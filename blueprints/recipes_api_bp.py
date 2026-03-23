"""
Recipe API Blueprint - RESTful endpoints for recipe management.

Provides database-backed recipe CRUD operations:
- GET /api/recipes - List user's recipes
- POST /api/recipes - Create new recipe
- GET /api/recipes/:id - Get specific recipe
- PUT /api/recipes/:id - Update recipe
- DELETE /api/recipes/:id - Delete recipe
"""

import logging

from extensions import db
from flask import Blueprint, jsonify, request, session
from repositories import db_recipe_repository
from utils.session_utils import get_or_create_session_id
from utils.cache_utils import (
    recipe_key, recipe_stats_key, invalidate_recipe,
    safe_get, safe_set, TTL_MEDIUM, TTL_SHORT,
)

logger = logging.getLogger(__name__)

recipes_api_bp = Blueprint("recipes_api", __name__, url_prefix="/api/recipes")


def _strip_image_data(data, recipe_id=None):
    """Remove bulky image storage fields from API responses.
    Images are served separately via GET /api/recipes/<id>/image.
    Also ensures ai_image_url points to the API endpoint when image data exists.

    Args:
        data: The recipe's JSON data dict.
        recipe_id: Explicit recipe ID (use when data may not contain 'id').
    """
    if not data:
        return data
    has_image = bool(data.get("ai_image_data") or data.get("ai_image_gcs"))
    stripped_keys = {"ai_image_data", "ai_image_gcs"}
    if not stripped_keys.intersection(data) and not has_image:
        return data
    result = {k: v for k, v in data.items() if k not in stripped_keys}
    # Ensure ai_image_url points to the API endpoint for any recipe with image data
    rid = recipe_id or result.get("id")
    if has_image and rid:
        result["ai_image_url"] = f"/api/recipes/{rid}/image"
    return result


def _recipe_response(recipe):
    """Build a consistent recipe response dict with image data stripped."""
    d = recipe.to_dict()
    d["data"] = _strip_image_data(d.get("data"), recipe_id=recipe.id)
    return d


def get_current_user_id():
    """
    Get the current authenticated user's ID from session.

    Returns:
        User ID (int) or None if not authenticated
    """
    return session.get("user_id")


def require_auth_or_guest(f):
    """
    Decorator that allows both authenticated and guest users.
    Sets user_id to None for guests.
    """

    def decorated_function(*args, **kwargs):
        user_id = get_current_user_id()
        guest_session_id = get_or_create_session_id()
        # Pass both scopes; route/repository chooses the correct one.
        return f(user_id=user_id, guest_session_id=guest_session_id, *args, **kwargs)

    decorated_function.__name__ = f.__name__
    return decorated_function


@recipes_api_bp.route("", methods=["GET"])
@require_auth_or_guest
def list_recipes(user_id, guest_session_id):
    """
    List all recipes for the current user.

    Returns:
        JSON array of recipes with metadata (id, name, created_at, etc.)
    """
    try:
        recipes = db_recipe_repository.get_user_recipes(user_id, guest_session_id)

        return (
            jsonify(
                {
                    "recipes": [
                        {
                            "id": recipe.id,
                            "name": recipe.name,
                            "data": _strip_image_data(recipe.data, recipe_id=recipe.id),
                            "created_at": (
                                recipe.created_at.isoformat() if recipe.created_at else None
                            ),
                            "updated_at": (
                                recipe.updated_at.isoformat() if recipe.updated_at else None
                            ),
                        }
                        for recipe in recipes
                    ],
                    "count": len(recipes),
                    "user_id": user_id,
                    "guest_session_id": guest_session_id,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error listing recipes: {e}")
        return jsonify({"error": "Failed to fetch recipes"}), 500


@recipes_api_bp.route("", methods=["POST"])
@require_auth_or_guest
def create_recipe(user_id, guest_session_id):
    """
    Create a new recipe.

    Request body:
        {
            "name": "Recipe Name",
            "ingredients": [...],
            "instructions": [...],
            ... (full recipe data)
        }

    Returns:
        Created recipe with ID
    """
    try:
        recipe_data = request.get_json()

        if not recipe_data:
            return jsonify({"error": "No recipe data provided"}), 400

        if "name" not in recipe_data:
            return jsonify({"error": "Recipe name is required"}), 400

        recipe = db_recipe_repository.create_recipe(recipe_data, user_id, guest_session_id)

        if not recipe:
            return jsonify({"error": "Failed to create recipe"}), 500

        invalidate_recipe(user_id, guest_session_id, recipe.id)
        return jsonify(_recipe_response(recipe)), 201

    except Exception as e:
        logger.error(f"Error creating recipe: {e}")
        return jsonify({"error": "Failed to create recipe"}), 500


@recipes_api_bp.route("/<recipe_id>", methods=["GET"])
@require_auth_or_guest
def get_recipe(user_id, guest_session_id, recipe_id):
    """
    Get a specific recipe by ID.

    Only returns recipe if it belongs to the current user (or is anonymous for guests).
    """
    try:
        # Check cache first
        ck = recipe_key(user_id, guest_session_id, recipe_id)
        cached = safe_get(ck)
        if cached is not None:
            return jsonify(cached), 200

        recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, guest_session_id)

        if not recipe:
            return jsonify({"error": "Recipe not found"}), 404

        result = _recipe_response(recipe)
        safe_set(ck, result, timeout=TTL_MEDIUM)
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error fetching recipe {recipe_id}: {e}")
        return jsonify({"error": "Failed to fetch recipe"}), 500


@recipes_api_bp.route("/<recipe_id>", methods=["PUT"])
@require_auth_or_guest
def update_recipe(user_id, guest_session_id, recipe_id):
    """
    Update an existing recipe.

    Request body:
        {
            "name": "Updated Name",
            "ingredients": [...],
            ... (full recipe data)
        }
    """
    try:
        recipe_data = request.get_json()

        if not recipe_data:
            return jsonify({"error": "No recipe data provided"}), 400

        recipe = db_recipe_repository.update_recipe(
            recipe_id, recipe_data, user_id, guest_session_id
        )

        if not recipe:
            return jsonify({"error": "Recipe not found or update failed"}), 404

        invalidate_recipe(user_id, guest_session_id, recipe_id)
        return jsonify(_recipe_response(recipe)), 200

    except Exception as e:
        logger.error(f"Error updating recipe {recipe_id}: {e}")
        return jsonify({"error": "Failed to update recipe"}), 500


@recipes_api_bp.route("/<recipe_id>", methods=["DELETE"])
@require_auth_or_guest
def delete_recipe(user_id, guest_session_id, recipe_id):
    """
    Delete a recipe.

    Only allows deletion if recipe belongs to current user (or is anonymous for guests).
    """
    try:
        success = db_recipe_repository.delete_recipe(recipe_id, user_id, guest_session_id)

        if not success:
            return jsonify({"error": "Recipe not found or delete failed"}), 404

        # Clean up GCS image if configured
        from config import GCS_BUCKET_NAME
        if GCS_BUCKET_NAME:
            from services.gcs_service import delete_image
            delete_image(GCS_BUCKET_NAME, recipe_id)

        invalidate_recipe(user_id, guest_session_id, recipe_id)
        return jsonify({"message": "Recipe deleted successfully"}), 200

    except Exception as e:
        logger.error(f"Error deleting recipe {recipe_id}: {e}")
        return jsonify({"error": "Failed to delete recipe"}), 500


@recipes_api_bp.route("/stats", methods=["GET"])
@require_auth_or_guest
def get_recipe_stats(user_id, guest_session_id):
    """
    Get statistics about user's recipes.

    Returns:
        {
            "total_recipes": 42,
            "user_id": 123 or null
        }
    """
    try:
        ck = recipe_stats_key(user_id, guest_session_id)
        cached = safe_get(ck)
        if cached is not None:
            return jsonify(cached), 200

        count = db_recipe_repository.count_user_recipes(user_id, guest_session_id)

        result = {
            "total_recipes": count,
            "user_id": user_id,
            "guest_session_id": guest_session_id,
        }
        safe_set(ck, result, timeout=TTL_SHORT)

        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error fetching recipe stats: {e}")
        return jsonify({"error": "Failed to fetch stats"}), 500
