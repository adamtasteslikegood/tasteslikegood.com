"""Agent tools API: the tools an AI agent may call (KAN-401).

- GET  /api/agent/tools          The tools that are switched on, with their input schemas
- POST /api/agent/tools/<name>   Call one; the JSON body holds its arguments

Which tools exist and who may call them is decided in ``services.agent_tools``.
Generation here never falls back to a guest session, unlike /api/generate: an
agent generates for a signed-in account or not at all.
"""

from typing import Any

from flask import Blueprint, current_app, jsonify, request, session, url_for
from flask.typing import ResponseReturnValue

from blueprints.generation_api_bp import queue_recipe_generation, queue_recipe_image
from blueprints.generation_bp import validate_generation_input
from blueprints.public_bp import _public_base_url, _recipe_image_url, _save_recipe_payload
from config import DEFAULT_MODEL
from models.recipe import Recipe
from repositories import db_recipe_repository
from services.agent_tools import ATTRIBUTION, Refusal, check_access, enabled_tools

agent_api_bp = Blueprint("agent_api", __name__, url_prefix="/api/agent")

SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 20


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
    query = arguments.get("query") or ""
    limit = arguments.get("limit", SEARCH_LIMIT_DEFAULT)
    if not isinstance(query, str) or type(limit) is not int:
        return _error(400, "query must be a string and limit an integer")
    limit = max(1, min(limit, SEARCH_LIMIT_MAX))

    rows = _published()
    words = query.strip()
    if words:
        rows = rows.filter(Recipe.name.icontains(words, autoescape=True))
    found = rows.order_by(Recipe.created_at.desc(), Recipe.id.desc()).limit(limit).all()
    return jsonify(
        {
            "recipes": [
                {
                    "name": recipe.name,
                    "slug": recipe.slug,
                    "url": _public_recipe_url(recipe.slug),
                    "description": (recipe.data or {}).get("description", ""),
                }
                for recipe in found
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
    # is the configured one; an agent does not choose it.
    response = current_app.make_response(
        queue_recipe_generation(prompt, DEFAULT_MODEL, user_id, None)
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
    return queue_recipe_image(recipe_id, False, user_id, None)


def _get_generation_status(arguments: dict[str, Any], user_id: int | None) -> ResponseReturnValue:
    recipe_id = _recipe_id_argument(arguments)
    if recipe_id is None:
        return _error(400, "recipe_id is required")
    recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, None)
    if recipe is None:
        return _error(404, "Recipe not found")
    return jsonify(
        {
            "recipe_id": recipe.id,
            "status": recipe.status,
            "recipe": recipe.data,
            "url": _account_recipe_url(recipe.id),
            "attribution": ATTRIBUTION,
        }
    )


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


@agent_api_bp.route("/tools/<name>", methods=["POST"])
def call_agent_tool(name: str) -> ResponseReturnValue:
    """Call one tool. The JSON body is the tool's arguments object."""
    user_id = session.get("user_id")
    tool = check_access(name, current_app.config, user_id)
    if isinstance(tool, Refusal):
        return _error(tool.status, tool.error)

    arguments = request.get_json(silent=True)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(400, "The request body must be a JSON object")
    return _HANDLERS[tool.name](arguments, user_id)
