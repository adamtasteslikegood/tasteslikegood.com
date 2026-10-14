"""The MCP endpoint over the published recipes (KAN-399)."""

import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402

GENERATION_ON = {"AGENT_TOOLS_GENERATE_TEXT": True, "AGENT_TOOLS_GENERATE_IMAGE": True}


@pytest.fixture
def make_client():
    contexts = []

    def _make(*, signed_in=False, **switches):
        app = create_app(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", **switches)
        context = app.app_context()
        context.push()
        contexts.append(context)
        db.create_all()
        db.session.add(
            Recipe(
                id=str(uuid.uuid4()),
                name="Smoky Tofu Tacos",
                slug="smoky-tofu-tacos",
                is_public=True,
                data={"name": "Smoky Tofu Tacos", "description": "Tacos.", "ingredients": []},
            )
        )
        db.session.commit()
        client = app.test_client()
        if signed_in:
            user = User(email="cook@example.com", name="Cook")
            db.session.add(user)
            db.session.commit()
            with client.session_transaction() as session:
                session["user_id"] = user.id
        return client

    yield _make
    for context in contexts:
        db.session.remove()
        db.drop_all()
        context.pop()


def _rpc(client, method, params=None, message_id=1, **kwargs):
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return client.post("/mcp", json=message, **kwargs)


def test_initialize_echoes_a_supported_version_and_names_the_server(make_client):
    response = _rpc(make_client(), "initialize", {"protocolVersion": "2025-06-18"})

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    body = response.get_json()
    assert body["id"] == 1
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["capabilities"] == {"tools": {}}
    assert body["result"]["serverInfo"]["name"]
    assert "Mcp-Session-Id" not in response.headers


@pytest.mark.parametrize("requested", ["2025-03-26", "1999-01-01", None])
def test_initialize_version_negotiation(make_client, requested):
    params = {} if requested is None else {"protocolVersion": requested}
    result = _rpc(make_client(), "initialize", params).get_json()["result"]

    expected = requested if requested == "2025-03-26" else "2025-06-18"
    assert result["protocolVersion"] == expected


def test_a_notification_is_accepted_with_no_body(make_client):
    response = make_client().post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )

    assert response.status_code == 202
    assert response.data == b""


def test_a_client_response_is_accepted(make_client):
    response = make_client().post("/mcp", json={"jsonrpc": "2.0", "id": 7, "result": {}})

    assert response.status_code == 202


def test_ping(make_client):
    assert _rpc(make_client(), "ping").get_json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tools_list_is_the_two_read_tools_with_the_registry_schemas(make_client):
    from services.agent_tools import TOOLS

    tools = _rpc(make_client(), "tools/list").get_json()["result"]["tools"]

    assert [tool["name"] for tool in tools] == ["search_recipes", "get_recipe"]
    by_name = {tool.name: tool for tool in TOOLS}
    for tool in tools:
        assert tool["inputSchema"] == by_name[tool["name"]].input_schema
        assert tool["description"]


def test_generation_is_never_offered_here_even_switched_on_and_signed_in(make_client):
    client = make_client(signed_in=True, **GENERATION_ON)

    names = [tool["name"] for tool in _rpc(client, "tools/list").get_json()["result"]["tools"]]
    assert names == ["search_recipes", "get_recipe"]

    for name in ("generate_recipe", "generate_recipe_image", "get_generation_status"):
        call = _rpc(client, "tools/call", {"name": name, "arguments": {"prompt": "a vegan stew"}})
        assert call.get_json()["error"]["code"] == -32602
    assert Recipe.query.count() == 1


def test_no_tools_with_the_read_switch_off(make_client):
    client = make_client(AGENT_TOOLS_READ=False)

    assert _rpc(client, "tools/list").get_json()["result"]["tools"] == []
    call = _rpc(client, "tools/call", {"name": "search_recipes", "arguments": {}})
    assert call.get_json()["error"]["code"] == -32602


def test_search_then_read_a_recipe(make_client):
    client = make_client()

    found = _rpc(client, "tools/call", {"name": "search_recipes", "arguments": {"query": "tofu"}})
    result = found.get_json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["recipes"][0]["slug"] == "smoky-tofu-tacos"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]

    read = _rpc(
        client, "tools/call", {"name": "get_recipe", "arguments": {"slug": "smoky-tofu-tacos"}}
    )
    recipe = read.get_json()["result"]["structuredContent"]
    assert recipe["url"].endswith("/r/smoky-tofu-tacos")
    assert recipe["attribution"]


def test_a_tool_failure_is_a_result_marked_as_an_error(make_client):
    client = make_client()

    missing = _rpc(client, "tools/call", {"name": "get_recipe", "arguments": {"slug": "nope"}})
    result = missing.get_json()["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "Recipe not found"

    nul = _rpc(client, "tools/call", {"name": "get_recipe", "arguments": {"slug": "a\x00b"}})
    assert nul.get_json()["result"]["isError"] is True


@pytest.mark.parametrize(
    "params",
    [{}, {"name": "get_recipe", "arguments": []}, {"name": 3}, {"name": "unknown_tool"}],
)
def test_bad_tool_call_params(make_client, params):
    body = _rpc(make_client(), "tools/call", params).get_json()

    assert body["error"]["code"] == -32602
    assert body["id"] == 1


def test_unknown_method(make_client):
    body = _rpc(make_client(), "resources/list").get_json()

    assert body["error"]["code"] == -32601


@pytest.mark.parametrize(
    "body, status",
    [
        (b"{not json", 400),
        (b"[]", 400),
        (b"", 400),
        (b'{"id": 1, "method": "ping"}', 400),
        (b'{"jsonrpc": "2.0", "id": 1}', 400),
        (b'{"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"}', 400),
        (b'{"jsonrpc": "2.0", "id": true, "method": "ping"}', 400),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "' + b"x" * 11000 + b'"}', 413),
    ],
)
def test_malformed_messages_are_refused(make_client, body, status):
    response = make_client().post("/mcp", data=body, content_type="application/json")

    assert response.status_code == status
    assert response.get_json()["error"]["code"] in (-32700, -32600)


def test_a_body_that_is_not_json_media_type_is_refused(make_client):
    response = make_client().post(
        "/mcp", data=b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}', content_type="text/plain"
    )

    assert response.status_code == 415


def test_params_must_be_an_object(make_client):
    body = _rpc(make_client(), "tools/list", params=[1]).get_json()

    assert body["error"]["code"] == -32602


@pytest.mark.parametrize("method", ["get", "delete", "put"])
def test_other_http_methods_are_not_allowed(make_client, method):
    assert getattr(make_client(), method)("/mcp").status_code == 405


def test_an_unsupported_protocol_version_header_is_refused(make_client):
    client = make_client()

    bad = _rpc(client, "ping", headers={"MCP-Protocol-Version": "1999-01-01"})
    assert bad.status_code == 400
    good = _rpc(client, "ping", headers={"MCP-Protocol-Version": "2025-06-18"})
    assert good.status_code == 200


def test_a_page_on_another_site_is_refused_and_the_site_itself_is_not(make_client):
    client = make_client()

    foreign = _rpc(client, "tools/list", headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403
    own = _rpc(client, "tools/list", headers={"Origin": "http://localhost"})
    assert own.status_code == 200
