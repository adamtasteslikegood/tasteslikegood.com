"""Agent tools API: the tools an AI agent may call (KAN-401).

- GET  /api/agent/tools          The tools that are switched on, with their input schemas
- POST /api/agent/tools/<name>   Call one; the JSON body holds its arguments

Which tools exist and who may call them is decided in ``services.agent_tools``.
Generation here never falls back to a guest session, unlike /api/generate: an
agent generates for a signed-in account or not at all.
"""

import json
from typing import Any

from flask import Blueprint, current_app, jsonify, request, session, url_for
from flask.typing import ResponseReturnValue

from blueprints.generation_api_bp import queue_recipe_generation, queue_recipe_image
from blueprints.generation_bp import validate_generation_input
from blueprints.public_bp import _public_base_url, _recipe_image_url, _save_recipe_payload
from config import DEFAULT_MODEL
from models.recipe import Recipe
from repositories import db_recipe_repository
from services.agent_tools import (
    ATTRIBUTION,
    GENERATE_TEXT,
    SWITCHES,
    Refusal,
    check_access,
    enabled_tools,
)

agent_api_bp = Blueprint("agent_api", __name__, url_prefix="/api/agent")

SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 20
SEARCH_QUERY_MAX_CHARS = 100
# Tool arguments are a prompt, a slug or an id. Express applies the same size
# to /api/generate (server/validation.ts); it does not parse these routes.
MAX_BODY_BYTES = 10 * 1024


def _error(status: int, message: str) -> ResponseReturnValue:
    return jsonify({"error": message}), status


def _public_recipe_url(slug: str) -> str:
    return _public_base_url() + url_for("public.show_public_recipe", slug=slug)


def _account_recipe_url(recipe_id: str) -> str:
    """Where the signed-in owner sees a recipe in the web app."""
    return f"{_public_base_url()}/recipe/{recipe_id}"


def _published() -> Any:
    return Recipe.query.filter(Recipe.is_public.is_(True), Recipe.slug.isnot(None))


def _search_recipes(arguments: dict[str, Any], user_id: int | None) -> ResponseReturnValue:
    query = arguments.get("query", "")
    limit = arguments.get("limit", SEARCH_LIMIT_DEFAULT)
    if not isinstance(query, str) or type(limit) is not int:
        return _error(400, "query must be a string and limit an integer")
    words = query.strip()
    if len(words) > SEARCH_QUERY_MAX_CHARS:
        return _error(400, f"query must be at most {SEARCH_QUERY_MAX_CHARS} characters")
    limit = max(1, min(limit, SEARCH_LIMIT_MAX))

    rows = _published()
    if words:
        rows = rows.filter(Recipe.name.icontains(words, autoescape=True))
    # Three values per recipe, not the row: `data` holds the whole recipe.
    found = (
        rows.with_entities(Recipe.name, Recipe.slug, Recipe.data["description"].as_string())
        .order_by(Recipe.created_at.desc(), Recipe.id.desc())
        .limit(limit)
        .all()
    )
    return jsonify(
        {
            "recipes": [
                {
                    "name": name,
                    "slug": slug,
                    "url": _public_recipe_url(slug),
                    "description": description or "",
                }
                for name, slug, description in found
            ],
            "attribution": ATTRIBUTION,
        }
    )


def _get_recipe(arguments: dict[str, Any], user_id: int | None) -> ResponseReturnValue:
    slug = arguments.get("slug")
    if not isinstance(slug, str) or not slug:
        return _error(400, "slug is required")
    recipe = _published().filter(Recipe.slug == slug).first()
    if recipe is None:
        return _error(404, "Recipe not found")
    payload = _save_recipe_payload(recipe, _recipe_image_url(recipe))
    payload["url"] = _public_recipe_url(recipe.slug)
    payload["attribution"] = ATTRIBUTION
    return jsonify(payload)


def _recipe_id_argument(arguments: dict[str, Any]) -> str | None:
    recipe_id = arguments.get("recipe_id")
    return recipe_id if isinstance(recipe_id, str) and recipe_id else None


def _generate_recipe(arguments: dict[str, Any], user_id: int | None) -> ResponseReturnValue:
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str):
        return _error(400, "prompt is required")
    prompt = prompt.strip()
    is_valid, error_message = validate_generation_input(prompt)
    if not is_valid:
        return _error(400, error_message)

    # No guest session: the recipe belongs to the signed-in account. The model
    # is the configured one; an agent does not choose it. Text only: the web
    # app's flow queues a photo after the text, and here that would generate
    # an image with the image switch off. A photo is generate_recipe_image.
    response = current_app.make_response(
        queue_recipe_generation(prompt, DEFAULT_MODEL, user_id, None, with_image=False)
    )
    if response.status_code != 202:
        return response
    body = response.get_json()
    body["url"] = _account_recipe_url(body["recipe_id"])
    body["attribution"] = ATTRIBUTION
    return jsonify(body), 202


def _generate_recipe_image(arguments: dict[str, Any], user_id: int | None) -> ResponseReturnValue:
    recipe_id = _recipe_id_argument(arguments)
    if recipe_id is None:
        return _error(400, "recipe_id is required")
    response = current_app.make_response(queue_recipe_image(recipe_id, False, user_id, None))
    if response.status_code not in (200, 202):
        return response
    # 202: queued. 200: the recipe already has a photo (image_url).
    body = response.get_json()
    body["recipe_id"] = recipe_id
    body["url"] = _account_recipe_url(recipe_id)
    body["attribution"] = ATTRIBUTION
    return jsonify(body), response.status_code


def _get_generation_status(arguments: dict[str, Any], user_id: int | None) -> ResponseReturnValue:
    recipe_id = _recipe_id_argument(arguments)
    if recipe_id is None:
        return _error(400, "recipe_id is required")
    recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, None)
    if recipe is None:
        return _error(404, "Recipe not found")
    # The status tool is on when either generation switch is on, so it gives
    # no more than that switch's offering. Always: the status and the photo.
    # The recipe text only with the text switch, and then only the fields a
    # published recipe shows, never the stored row (notes the owner wrote,
    # generation metadata).
    image_url = _recipe_image_url(recipe)
    body: dict[str, Any] = {
        "recipe_id": recipe.id,
        "status": recipe.status,
        "image_url": image_url,
        "url": _account_recipe_url(recipe.id),
        "attribution": ATTRIBUTION,
    }
    if current_app.config.get(SWITCHES[GENERATE_TEXT]) is True:
        body["recipe"] = _save_recipe_payload(recipe, image_url)
    return jsonify(body)


_HANDLERS = {
    "search_recipes": _search_recipes,
    "get_recipe": _get_recipe,
    "generate_recipe": _generate_recipe,
    "generate_recipe_image": _generate_recipe_image,
    "get_generation_status": _get_generation_status,
}


@agent_api_bp.route("/tools", methods=["GET"])
def list_agent_tools() -> ResponseReturnValue:
    """The tools that are switched on. A switched-off tool is not listed."""
    return jsonify(
        {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    "requiresSignIn": tool.requires_sign_in,
                }
                for tool in enabled_tools(current_app.config)
            ],
            "attribution": ATTRIBUTION,
        }
    )


def read_arguments() -> tuple[dict[str, Any], ResponseReturnValue | None]:
    """The request body as a JSON object, or the refusal to send back.

    Shared with the MCP endpoint (``blueprints/mcp_bp.py``), which reads its
    JSON-RPC message through the same checks.
    """
    # Read one byte past the limit and count, so a body sent without a
    # Content-Length (chunked) is refused too, not cut short and parsed.
    if (request.content_length or 0) > MAX_BODY_BYTES:
        return {}, _error(413, "The request body is too large")
    body = request.stream.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        return {}, _error(413, "The request body is too large")
    # No body means no arguments. A body that is present must be a JSON
    # object: a malformed one must not run the tool with defaults.
    arguments: Any = {}
    if body.strip():
        # A JSON media type, as /api/generate requires: a browser cannot send
        # one across origins without a preflight.
        if not request.is_json:
            return {}, _error(415, "The request body must be sent as application/json")
        try:
            arguments = json.loads(body)
            # A lone surrogate (an escape such as \ud800) parses, then fails
            # in the database driver. Refuse it here.
            json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        except (ValueError, RecursionError):
            # RecursionError: a body nested a few hundred levels deep.
            return {}, _error(400, "The request body must be a JSON object")
    if not isinstance(arguments, dict):
        return {}, _error(400, "The request body must be a JSON object")
    return arguments, None


def run_tool(name: str, arguments: dict[str, Any], user_id: int | None) -> ResponseReturnValue:
    """Check access, then call the tool. Every caller of a tool comes through here."""
    tool = check_access(name, current_app.config, user_id)
    if isinstance(tool, Refusal):
        return _error(tool.status, tool.error)
    # PostgreSQL refuses a NUL in a string parameter (also a 500). Every tool
    # argument is a top-level value, so nothing nested reaches a query.
    if any(isinstance(value, str) and "\x00" in value for value in arguments.values()):
        return _error(400, "Arguments must not contain NUL characters")
    return _HANDLERS[tool.name](arguments, user_id)


@agent_api_bp.route("/tools/<name>", methods=["POST"])
def call_agent_tool(name: str) -> ResponseReturnValue:
    """Call one tool. The JSON body is the tool's arguments object."""
    user_id = session.get("user_id")
    # Access first: a caller who may not use the tool learns nothing about
    # its body.
    tool = check_access(name, current_app.config, user_id)
    if isinstance(tool, Refusal):
        return _error(tool.status, tool.error)
    arguments, refusal = read_arguments()
    if refusal is not None:
        return refusal
    return run_tool(name, arguments, user_id)
