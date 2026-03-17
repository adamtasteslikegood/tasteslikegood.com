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

logger = logging.getLogger(__name__)

recipes_api_bp = Blueprint("recipes_api", __name__, url_prefix="/api/recipes")


def _strip_image_data(data):
    """Remove bulky ai_image_data from API responses to keep payloads small.
    Images are served separately via GET /api/recipes/<id>/image."""
    if not data or "ai_image_data" not in data:
        return data
    return {k: v for k, v in data.items() if k != "ai_image_data"}


def _recipe_response(recipe):
    """Build a consistent recipe response dict with image data stripped."""
    d = recipe.to_dict()
    d["data"] = _strip_image_data(d.get("data"))
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
                            "data": _strip_image_data(recipe.data),
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
        recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, guest_session_id)

        if not recipe:
            return jsonify({"error": "Recipe not found"}), 404

        return jsonify(_recipe_response(recipe)), 200

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
        count = db_recipe_repository.count_user_recipes(user_id, guest_session_id)

        return (
            jsonify(
                {
                    "total_recipes": count,
                    "user_id": user_id,
                    "guest_session_id": guest_session_id,
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Error fetching recipe stats: {e}")
        return jsonify({"error": "Failed to fetch stats"}), 500
