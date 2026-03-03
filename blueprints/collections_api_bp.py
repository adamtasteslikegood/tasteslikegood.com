"""
Collections API Blueprint — RESTful endpoints for cookbook management.

Provides database-backed cookbook CRUD operations:
- GET    /api/collections                         List user's cookbooks
- POST   /api/collections                         Create a new cookbook
- GET    /api/collections/<id>                    Get a specific cookbook
- DELETE /api/collections/<id>                    Delete a cookbook
- POST   /api/collections/<id>/recipes            Add a recipe to a cookbook
- DELETE /api/collections/<id>/recipes/<rid>      Remove a recipe from a cookbook
"""

import logging
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request, session

from extensions import db
from models import Cookbook

logger = logging.getLogger(__name__)

collections_api_bp = Blueprint(
    "collections_api", __name__, url_prefix="/api/collections"
)


def _current_user_id():
    return session.get("user_id")


def _require_auth_or_guest(f):
    """Inject user_id (None for guests) into the route function."""
    def wrapper(*args, **kwargs):
        return f(user_id=_current_user_id(), *args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


# ── Routes ────────────────────────────────────────────────────────────────────

@collections_api_bp.route("", methods=["GET"])
@_require_auth_or_guest
def list_collections(user_id):
    """List all cookbooks owned by the current user (or anonymous)."""
    try:
        cookbooks = (
            Cookbook.query.filter_by(user_id=user_id)
            .order_by(Cookbook.created_at.desc())
            .all()
        )
        return jsonify({"collections": [cb.to_dict() for cb in cookbooks]}), 200
    except Exception as e:
        logger.error(f"Error listing collections: {e}")
        return jsonify({"error": "Failed to fetch collections"}), 500


@collections_api_bp.route("", methods=["POST"])
@_require_auth_or_guest
def create_collection(user_id):
    """
    Create a new cookbook.

    Request body: {"name": "...", "description": "...", "id": "<optional-uuid>"}
    """
    try:
        data = request.get_json()
        if not data or not data.get("name"):
            return jsonify({"error": "name is required"}), 400

        cookbook = Cookbook(
            id=data.get("id") or str(uuid.uuid4()),
            user_id=user_id,
            name=data["name"],
            description=data.get("description", ""),
            cover_image=data.get("coverImage"),
            recipe_ids=data.get("recipeIds", []),
        )
        db.session.add(cookbook)
        db.session.commit()
        return jsonify(cookbook.to_dict()), 201
    except Exception as e:
        logger.error(f"Error creating collection: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to create collection"}), 500


@collections_api_bp.route("/<collection_id>", methods=["GET"])
@_require_auth_or_guest
def get_collection(user_id, collection_id):
    """Get a specific cookbook by ID."""
    try:
        cookbook = Cookbook.query.filter_by(id=collection_id, user_id=user_id).first()
        if not cookbook:
            return jsonify({"error": "Collection not found"}), 404
        return jsonify(cookbook.to_dict()), 200
    except Exception as e:
        logger.error(f"Error fetching collection {collection_id}: {e}")
        return jsonify({"error": "Failed to fetch collection"}), 500


@collections_api_bp.route("/<collection_id>", methods=["DELETE"])
@_require_auth_or_guest
def delete_collection(user_id, collection_id):
    """Delete a cookbook."""
    try:
        cookbook = Cookbook.query.filter_by(id=collection_id, user_id=user_id).first()
        if not cookbook:
            return jsonify({"error": "Collection not found"}), 404
        db.session.delete(cookbook)
        db.session.commit()
        return jsonify({"message": "Collection deleted"}), 200
    except Exception as e:
        logger.error(f"Error deleting collection {collection_id}: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to delete collection"}), 500


@collections_api_bp.route("/<collection_id>/recipes", methods=["POST"])
@_require_auth_or_guest
def add_recipe_to_collection(user_id, collection_id):
    """
    Add a recipe ID to a cookbook.

    Request body: {"recipe_id": "<uuid>"}
    """
    try:
        data = request.get_json()
        recipe_id = data.get("recipe_id") if data else None
        if not recipe_id:
            return jsonify({"error": "recipe_id is required"}), 400

        cookbook = Cookbook.query.filter_by(id=collection_id, user_id=user_id).first()
        if not cookbook:
            return jsonify({"error": "Collection not found"}), 404

        ids = list(cookbook.recipe_ids or [])
        if recipe_id not in ids:
            ids.append(recipe_id)
            cookbook.recipe_ids = ids
            cookbook.updated_at = datetime.utcnow()
            db.session.commit()

        return jsonify(cookbook.to_dict()), 200
    except Exception as e:
        logger.error(f"Error adding recipe to collection {collection_id}: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to update collection"}), 500


@collections_api_bp.route("/<collection_id>/recipes/<recipe_id>", methods=["DELETE"])
@_require_auth_or_guest
def remove_recipe_from_collection(user_id, collection_id, recipe_id):
    """Remove a recipe ID from a cookbook."""
    try:
        cookbook = Cookbook.query.filter_by(id=collection_id, user_id=user_id).first()
        if not cookbook:
            return jsonify({"error": "Collection not found"}), 404

        ids = [r for r in (cookbook.recipe_ids or []) if r != recipe_id]
        cookbook.recipe_ids = ids
        cookbook.updated_at = datetime.utcnow()
        db.session.commit()

        return jsonify(cookbook.to_dict()), 200
    except Exception as e:
        logger.error(f"Error removing recipe from collection {collection_id}: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to update collection"}), 500
