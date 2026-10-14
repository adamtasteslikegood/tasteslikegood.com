"""The OpenAPI description of the public read API (KAN-398)."""

import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402

READ_TOOL_PATHS = ["/api/agent/tools/search_recipes", "/api/agent/tools/get_recipe"]
GENERATION_ON = {"AGENT_TOOLS_GENERATE_TEXT": True, "AGENT_TOOLS_GENERATE_IMAGE": True}


@pytest.fixture
def document():
    """Fetch the document from an app with the given switches."""
    contexts = []

    def _fetch(**switches):
        app = create_app(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:", **switches)
        context = app.app_context()
        context.push()
        contexts.append(context)
        db.create_all()
        response = app.test_client().get("/api/public/openapi.json")
        assert response.status_code == 200
        assert response.mimetype == "application/json"
        return response.get_json()

    yield _fetch
    for context in contexts:
        db.session.remove()
        db.drop_all()
        context.pop()


def test_describes_the_public_endpoints_and_the_read_tools(document):
    doc = document()

    assert doc["openapi"].startswith("3.1")
    assert doc["info"]["title"]
    assert doc["servers"][0]["url"].startswith("http")
    assert set(doc["paths"]) == {
        "/api/public/newest-recipes",
        "/api/recipes/public/{slug}",
        "/api/agent/tools",
        *READ_TOOL_PATHS,
    }


def test_every_described_path_is_a_route_the_app_serves(document):
    doc = document()
    client_app = create_app(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    rules = {
        rule.rule.replace("<", "{").replace(">", "}") for rule in client_app.url_map.iter_rules()
    }

    for path in doc["paths"]:
        if path.startswith("/api/agent/tools/"):
            assert "/api/agent/tools/{name}" in rules
        else:
            assert path in rules


def test_a_tool_schema_is_the_registry_schema(document):
    from services.agent_tools import TOOLS

    doc = document()
    by_name = {tool.name: tool for tool in TOOLS}

    for path in READ_TOOL_PATHS:
        operation = doc["paths"][path]["post"]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        assert schema == by_name[operation["operationId"]].input_schema
    assert doc["paths"]["/api/agent/tools/get_recipe"]["post"]["requestBody"]["required"] is True
    assert (
        doc["paths"]["/api/agent/tools/search_recipes"]["post"]["requestBody"]["required"] is False
    )


def test_tools_that_need_sign_in_are_not_described_even_when_switched_on(document):
    doc = document(**GENERATION_ON)

    assert [path for path in doc["paths"] if "generat" in path] == []
    assert "generat" not in str(doc["paths"])


def test_no_tool_paths_when_the_read_switch_is_off(document):
    doc = document(AGENT_TOOLS_READ=False)

    assert set(doc["paths"]) == {"/api/public/newest-recipes", "/api/recipes/public/{slug}"}


def test_limit_bounds_match_the_endpoint(document):
    from blueprints.public_bp import NEWEST_RECIPES_DEFAULT, NEWEST_RECIPES_MAX

    schema = document()["paths"]["/api/public/newest-recipes"]["get"]["parameters"][0]["schema"]

    assert schema["maximum"] == NEWEST_RECIPES_MAX
    assert schema["default"] == NEWEST_RECIPES_DEFAULT


def test_each_tool_describes_only_the_errors_it_returns(document):
    paths = document()["paths"]

    assert "404" in paths["/api/agent/tools/get_recipe"]["post"]["responses"]
    assert "404" not in paths["/api/agent/tools/search_recipes"]["post"]["responses"]


def test_the_recipe_endpoint_describes_the_redirect_for_a_renamed_slug(document):
    responses = document()["paths"]["/api/recipes/public/{slug}"]["get"]["responses"]

    assert set(responses) == {"200", "302", "404", "410"}
    assert "Location" in responses["302"]["headers"]
