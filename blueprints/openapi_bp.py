"""The OpenAPI description of the public read API (KAN-398).

- GET /api/public/openapi.json

It lists only what a caller with no account may use: the two public recipe
endpoints and the agent tools that read published recipes. The tool entries
come from ``services.agent_tools``, so a tool that is switched off or needs
sign-in is not described, the same as ``GET /api/agent/tools`` does not list
it. Responses are described loosely: the recipe shape has one definition, the
code that builds it.
"""

from typing import Any

from flask import Blueprint, current_app, jsonify
from flask.typing import ResponseReturnValue

from blueprints.public_bp import (
    NEWEST_RECIPES_DEFAULT,
    NEWEST_RECIPES_MAX,
    _public_base_url,
)
from services.agent_tools import ATTRIBUTION, enabled_tools

openapi_bp = Blueprint("openapi", __name__)

OPENAPI_VERSION = "3.1.0"
# The version of this description, not of the app: raise it when a path or a
# parameter here changes.
API_VERSION = "1.0.0"


def _json_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"type": "object"}}},
    }


_ERROR = _json_response("An error: an object with an `error` message.")

# get_recipe is the only read-only tool whose handler returns 404 (slug not
# found in blueprints/agent_api_bp.py); search_recipes answers 200 even for
# a no-match query.
_TOOLS_WITH_NOT_FOUND = {"get_recipe"}


def _tool_path(tool: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "200": _json_response("The tool's result, with an `attribution` line."),
        "400": _ERROR,
    }
    if tool.name in _TOOLS_WITH_NOT_FOUND:
        responses["404"] = _ERROR
    return {
        "post": {
            "operationId": tool.name,
            "summary": tool.description,
            "requestBody": {
                "required": bool(tool.input_schema.get("required")),
                "content": {"application/json": {"schema": tool.input_schema}},
            },
            "responses": responses,
        }
    }


@openapi_bp.route("/api/public/openapi.json", methods=["GET"])
def public_openapi() -> ResponseReturnValue:
    paths: dict[str, Any] = {
        "/api/public/newest-recipes": {
            "get": {
                "operationId": "newest_recipes",
                "summary": "The most recently published recipes: slug, name and date.",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": NEWEST_RECIPES_MAX,
                            "default": NEWEST_RECIPES_DEFAULT,
                        },
                    }
                ],
                "responses": {"200": _json_response("An object with a `recipes` list.")},
            }
        },
        "/api/recipes/public/{slug}": {
            "get": {
                "operationId": "public_recipe",
                "summary": "One published recipe: ingredients, instructions, times and tags.",
                "parameters": [
                    {
                        "name": "slug",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "The last part of the recipe's /r/<slug> address.",
                    }
                ],
                "responses": {
                    "200": _json_response("The recipe."),
                    "404": _ERROR,
                    "410": _json_response("The recipe was removed."),
                },
            }
        },
    }

    tools = [tool for tool in enabled_tools(current_app.config) if not tool.requires_sign_in]
    if tools:
        paths["/api/agent/tools"] = {
            "get": {
                "operationId": "list_agent_tools",
                "summary": "The tools an AI agent may call, with their input schemas.",
                "responses": {"200": _json_response("An object with a `tools` list.")},
            }
        }
        for tool in tools:
            paths[f"/api/agent/tools/{tool.name}"] = _tool_path(tool)

    return jsonify(
        {
            "openapi": OPENAPI_VERSION,
            "info": {
                "title": "Tasteslikegood.org public recipe API",
                "version": API_VERSION,
                "description": (
                    "Read access to the published vegan recipes on Tasteslikegood.org. "
                    "No account or key is needed. " + ATTRIBUTION
                ),
            },
            "servers": [{"url": _public_base_url()}],
            "paths": paths,
        }
    )
