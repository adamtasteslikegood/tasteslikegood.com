"""Agent tools: the switches, the access check and the tool calls (KAN-401)."""

import io
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from werkzeug.test import EnvironBuilder

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402

READ_TOOLS = ["search_recipes", "get_recipe"]


def _make_app(**switches):
    return create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        **switches,
    )


@pytest.fixture
def make_client():
    """Build a client for an app with the given switches; signed in when asked."""
    contexts = []

    def _make(*, signed_in=False, **switches):
        app = _make_app(**switches)
        context = app.app_context()
        context.push()
        contexts.append(context)
        db.create_all()
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


def _tool_names(client):
    return [tool["name"] for tool in client.get("/api/agent/tools").get_json()["tools"]]


def _call(client, name, **arguments):
    return client.post(f"/api/agent/tools/{name}", json=arguments)


def _published_recipe(name="Smoky Chili", slug="smoky-chili"):
    recipe = Recipe(
        id=str(uuid.uuid4()),
        name=name,
        slug=slug,
        is_public=True,
        status="ready",
        data={"name": name, "description": "A pot of chili.", "tags": ["dinner"]},
    )
    db.session.add(recipe)
    db.session.commit()
    return recipe


def _owned_recipe(user_id, status="ready"):
    recipe = Recipe(
        id=str(uuid.uuid4()), user_id=user_id, name="Mine", status=status, data={"name": "Mine"}
    )
    db.session.add(recipe)
    db.session.commit()
    return recipe


# --- the switches -----------------------------------------------------------


def test_by_default_only_the_read_tools_are_offered(make_client):
    client = make_client(signed_in=True)
    assert _tool_names(client) == READ_TOOLS
    for name in ("generate_recipe", "generate_recipe_image", "get_generation_status"):
        assert _call(client, name, prompt="A bright summer pasta").status_code == 404


def test_text_switch_offers_text_generation_and_not_images(make_client):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_TEXT=True)
    assert _tool_names(client) == [*READ_TOOLS, "generate_recipe", "get_generation_status"]
    assert _call(client, "generate_recipe_image", recipe_id="x").status_code == 404


def test_image_switch_offers_image_generation_and_not_text(make_client):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_IMAGE=True)
    assert _tool_names(client) == [*READ_TOOLS, "generate_recipe_image", "get_generation_status"]
    assert _call(client, "generate_recipe", prompt="A bright summer pasta").status_code == 404


def test_read_switch_off_removes_the_read_tools(make_client):
    client = make_client(AGENT_TOOLS_READ=False)
    _published_recipe()
    assert _tool_names(client) == []
    assert _call(client, "search_recipes").status_code == 404
    assert _call(client, "get_recipe", slug="smoky-chili").status_code == 404


def test_a_switched_off_tool_answers_like_an_unknown_one(make_client):
    client = make_client()
    off = _call(client, "generate_recipe", prompt="A bright summer pasta")
    unknown = _call(client, "no_such_tool")
    assert (off.status_code, off.get_json()) == (unknown.status_code, unknown.get_json())


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_switch_reads_on_values_from_the_environment(monkeypatch, value):
    from config import _env_switch

    monkeypatch.setenv("AGENT_TOOLS_TEST_SWITCH", value)
    assert _env_switch("AGENT_TOOLS_TEST_SWITCH", False) is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "enabled?"])
def test_switch_reads_anything_else_as_off(monkeypatch, value):
    from config import _env_switch

    monkeypatch.setenv("AGENT_TOOLS_TEST_SWITCH", value)
    assert _env_switch("AGENT_TOOLS_TEST_SWITCH", True) is False


def test_unset_switch_keeps_its_default(monkeypatch):
    from config import _env_switch

    monkeypatch.delenv("AGENT_TOOLS_TEST_SWITCH", raising=False)
    assert _env_switch("AGENT_TOOLS_TEST_SWITCH", True) is True
    assert _env_switch("AGENT_TOOLS_TEST_SWITCH", False) is False


# --- who may generate -------------------------------------------------------


def test_signed_out_caller_cannot_generate_even_with_a_guest_session(make_client):
    client = make_client(AGENT_TOOLS_GENERATE_TEXT=True, AGENT_TOOLS_GENERATE_IMAGE=True)
    with client.session_transaction() as session:
        session["session_id"] = "guest-1"
    with patch("services.pubsub_service.publish_message") as publish:
        text = _call(client, "generate_recipe", prompt="A bright summer pasta")
        image = _call(client, "generate_recipe_image", recipe_id="x")
        status = _call(client, "get_generation_status", recipe_id="x")
    assert [text.status_code, image.status_code, status.status_code] == [401, 401, 401]
    publish.assert_not_called()
    assert Recipe.query.count() == 0


def test_signed_in_caller_queues_a_recipe_owned_by_the_account(make_client):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_TEXT=True)
    with patch("services.pubsub_service.publish_message") as publish:
        response = _call(client, "generate_recipe", prompt="A bright summer pasta")

    assert response.status_code == 202
    body = response.get_json()
    recipe = db.session.get(Recipe, body["recipe_id"])
    assert recipe.status == "generating"
    assert recipe.user_id == User.query.one().id
    assert recipe.guest_session_id is None
    assert body["url"].endswith(f"/recipe/{recipe.id}")
    assert "Tasteslikegood.org" in body["attribution"]
    (topic, message), _ = publish.call_args
    assert topic == "recipe-generation"
    assert message["user_id"] == recipe.user_id
    assert message["guest_session_id"] is None
    assert message["with_image"] is False


def test_text_generation_asks_for_no_image_even_with_the_image_switch_on(make_client):
    client = make_client(
        signed_in=True, AGENT_TOOLS_GENERATE_TEXT=True, AGENT_TOOLS_GENERATE_IMAGE=True
    )
    with patch("services.pubsub_service.publish_message") as publish:
        _call(client, "generate_recipe", prompt="A bright summer pasta")
    assert publish.call_args.args[1]["with_image"] is False


def test_agent_does_not_choose_the_model(make_client):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_TEXT=True)
    with patch("services.pubsub_service.publish_message") as publish:
        _call(client, "generate_recipe", prompt="A bright summer pasta", model="models/other")
    from config import DEFAULT_MODEL

    assert publish.call_args.args[1]["model"] == DEFAULT_MODEL


def test_generate_recipe_rejects_a_bad_prompt(make_client):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_TEXT=True)
    assert _call(client, "generate_recipe").status_code == 400
    assert _call(client, "generate_recipe", prompt=7).status_code == 400
    assert Recipe.query.count() == 0


def test_image_tool_queues_for_a_recipe_the_account_owns(make_client):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_IMAGE=True)
    recipe = _owned_recipe(User.query.one().id)
    with patch("services.pubsub_service.publish_message") as publish:
        response = _call(client, "generate_recipe_image", recipe_id=recipe.id)
    assert response.status_code == 202
    assert publish.call_args.args[0] == "image-generation"
    body = response.get_json()
    assert body["status"] == "generating_image"
    assert body["recipe_id"] == recipe.id
    assert body["url"].endswith(f"/recipe/{recipe.id}")
    assert "Tasteslikegood.org" in body["attribution"]


def test_image_tool_does_not_reach_another_owners_recipe(make_client):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_IMAGE=True)
    other = User(email="other@example.com", name="Other")
    db.session.add(other)
    db.session.commit()
    recipe = _owned_recipe(other.id)
    with patch("services.pubsub_service.publish_message") as publish:
        response = _call(client, "generate_recipe_image", recipe_id=recipe.id)
    assert response.status_code == 404
    publish.assert_not_called()


def test_status_tool_reads_only_the_accounts_own_generation(make_client):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_TEXT=True)
    mine = _owned_recipe(User.query.one().id, status="generating")
    other = User(email="other@example.com", name="Other")
    db.session.add(other)
    db.session.commit()
    theirs = _owned_recipe(other.id)

    own = _call(client, "get_generation_status", recipe_id=mine.id)
    assert own.status_code == 200
    assert own.get_json()["status"] == "generating"
    assert _call(client, "get_generation_status", recipe_id=theirs.id).status_code == 404


# --- the web app's own routes are untouched ---------------------------------


@pytest.mark.parametrize("switch", [False, True])
def test_web_generation_ignores_the_agent_switches(make_client, switch):
    client = make_client(AGENT_TOOLS_GENERATE_TEXT=switch, AGENT_TOOLS_READ=switch)
    with client.session_transaction() as session:
        session["session_id"] = "guest-1"
    with patch("services.pubsub_service.publish_message") as publish:
        response = client.post("/api/generate", json={"prompt": "A bright summer pasta"})
    assert response.status_code == 202
    assert db.session.get(Recipe, response.get_json()["recipe_id"]).guest_session_id == "guest-1"
    assert publish.call_args.args[1]["with_image"] is True


# --- reading ----------------------------------------------------------------


def test_search_returns_published_recipes_only(make_client):
    client = make_client()
    _published_recipe()
    db.session.add(
        Recipe(
            id=str(uuid.uuid4()),
            name="Secret Chili",
            slug="secret-chili",
            is_public=False,
            data={"name": "Secret Chili"},
        )
    )
    db.session.commit()

    body = _call(client, "search_recipes", query="chili").get_json()
    assert [recipe["slug"] for recipe in body["recipes"]] == ["smoky-chili"]
    assert body["recipes"][0]["url"].endswith("/r/smoky-chili")
    assert "Tasteslikegood.org" in body["attribution"]


def test_search_treats_wildcards_as_text_and_caps_the_limit(make_client):
    client = make_client()
    for index in range(25):
        _published_recipe(name=f"Chili {index}", slug=f"chili-{index}")
    assert _call(client, "search_recipes", query="%").get_json()["recipes"] == []
    assert len(_call(client, "search_recipes", limit=500).get_json()["recipes"]) == 20
    assert _call(client, "search_recipes", limit="many").status_code == 400


def test_search_with_no_body_lists_recipes(make_client):
    client = make_client()
    _published_recipe()
    response = client.post("/api/agent/tools/search_recipes")
    assert len(response.get_json()["recipes"]) == 1


def test_get_recipe_returns_a_published_recipe_and_hides_a_private_one(make_client):
    client = make_client()
    _published_recipe()
    db.session.add(
        Recipe(
            id=str(uuid.uuid4()),
            name="Secret",
            slug="secret",
            is_public=False,
            data={"name": "Secret"},
        )
    )
    db.session.commit()

    body = _call(client, "get_recipe", slug="smoky-chili").get_json()
    assert body["name"] == "Smoky Chili"
    assert body["url"].endswith("/r/smoky-chili")
    assert _call(client, "get_recipe", slug="secret").status_code == 404
    assert _call(client, "get_recipe").status_code == 400


def test_tool_call_rejects_a_body_that_is_not_an_object(make_client):
    client = make_client()
    assert client.post("/api/agent/tools/search_recipes", json=["chili"]).status_code == 400


def test_every_generating_tool_is_named_generate():
    """Express applies the generation limit and the write pause by this prefix."""
    from services.agent_tools import TOOLS

    needs_sign_in = {tool.name for tool in TOOLS if tool.requires_sign_in}
    assert needs_sign_in - {"get_generation_status"} == {
        tool.name for tool in TOOLS if tool.name.startswith("generate_")
    }


def test_tool_call_refuses_a_body_over_the_size_limit(make_client):
    from blueprints.agent_api_bp import MAX_BODY_BYTES

    client = make_client()
    padding = "x" * MAX_BODY_BYTES
    sized = client.post("/api/agent/tools/search_recipes", json={"query": "chili", "pad": padding})
    assert sized.status_code == 413

    # No Content-Length, as a chunked upload arrives: a valid object, then
    # whitespace past the limit. It must be refused, not cut short and run.
    body = b'{"query": "chili"}' + b" " * MAX_BODY_BYTES
    builder = EnvironBuilder(
        path="/api/agent/tools/search_recipes",
        method="POST",
        input_stream=io.BytesIO(body),
        content_type="application/json",
    )
    environ = builder.get_environ()
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input_terminated"] = True
    streamed = client.open(environ)
    assert streamed.status_code == 413


def test_search_refuses_a_query_over_the_length_limit(make_client):
    from blueprints.agent_api_bp import SEARCH_QUERY_MAX_CHARS

    client = make_client()
    assert _call(client, "search_recipes", query="a" * SEARCH_QUERY_MAX_CHARS).status_code == 200
    assert (
        _call(client, "search_recipes", query="a" * (SEARCH_QUERY_MAX_CHARS + 1)).status_code == 400
    )


def test_search_returns_the_description_and_copes_without_one(make_client):
    client = make_client()
    _published_recipe()
    db.session.add(
        Recipe(
            id=str(uuid.uuid4()),
            name="Plain Chili",
            slug="plain-chili",
            is_public=True,
            data={"name": "Plain Chili"},
        )
    )
    db.session.commit()
    found = {r["slug"]: r for r in _call(client, "search_recipes").get_json()["recipes"]}
    assert found["smoky-chili"]["description"] == "A pot of chili."
    assert found["plain-chili"]["description"] == ""


@pytest.mark.parametrize("body", ['{"query":"chili"', "null", "[]", '"chili"', "7"])
def test_tool_call_rejects_malformed_json_instead_of_running_with_defaults(make_client, body):
    client = make_client()
    _published_recipe()
    response = client.post(
        "/api/agent/tools/search_recipes", data=body, content_type="application/json"
    )
    assert response.status_code == 400


@pytest.mark.parametrize("query", [0, False, None, ["chili"]])
def test_search_rejects_a_query_that_is_not_a_string(make_client, query):
    client = make_client()
    assert _call(client, "search_recipes", query=query).status_code == 400


def test_image_tool_reports_a_photo_that_already_exists(make_client):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_IMAGE=True)
    recipe = _owned_recipe(User.query.one().id)
    recipe.data = {
        **recipe.data,
        "ai_image_url": "/api/recipes/x/image",
        "ai_image_gcs": "recipes/x.png",
    }
    db.session.commit()
    with patch("services.pubsub_service.publish_message") as publish:
        response = _call(client, "generate_recipe_image", recipe_id=recipe.id)
    assert response.status_code == 200
    publish.assert_not_called()
    body = response.get_json()
    assert body["image_url"] == "/api/recipes/x/image"
    assert body["recipe_id"] == recipe.id
    assert body["url"].endswith(f"/recipe/{recipe.id}")


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", None])
def test_tool_call_refuses_a_body_that_is_not_sent_as_json(make_client, content_type):
    client = make_client(signed_in=True, AGENT_TOOLS_GENERATE_TEXT=True)
    with patch("services.pubsub_service.publish_message") as publish:
        response = client.post(
            "/api/agent/tools/generate_recipe",
            data='{"prompt": "A bright summer pasta"}',
            content_type=content_type,
        )
    assert response.status_code == 415
    publish.assert_not_called()
    assert Recipe.query.count() == 0


@pytest.mark.parametrize(
    "name, body",
    [
        ("search_recipes", '{"query": "\\ud800"}'),
        ("get_recipe", '{"slug": "\\ud800"}'),
        ("search_recipes", '{"query": "chili\\u0000"}'),
        ("get_recipe", '{"slug": "\\u0000"}'),
    ],
)
def test_tool_call_refuses_text_the_database_cannot_take(make_client, name, body):
    client = make_client()
    response = client.post(f"/api/agent/tools/{name}", data=body, content_type="application/json")
    assert response.status_code == 400


@pytest.mark.parametrize("depth", [600, 4000])
def test_tool_call_refuses_or_ignores_deeply_nested_json(make_client, depth):
    """Nesting must never be a 500: it is refused, or ignored as an unused argument."""
    client = make_client()
    body = '{"query": "chili", "extra": ' + "[" * depth + "]" * depth + "}"
    response = client.post(
        "/api/agent/tools/search_recipes", data=body, content_type="application/json"
    )
    assert response.status_code in (200, 400)
