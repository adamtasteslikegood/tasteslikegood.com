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
from utils.session_utils import get_or_create_session_id
from utils.cache_utils import (
    collections_list_key,
    collection_key,
    invalidate_collection,
    safe_get,
    safe_set,
    TTL_SHORT,
    TTL_MEDIUM,
)

logger = logging.getLogger(__name__)

collections_api_bp = Blueprint("collections_api", __name__, url_prefix="/api/collections")


def _current_user_id():
    return session.get("user_id")


def _current_guest_session_id():
    return get_or_create_session_id()


def _scope_collections_query(user_id, guest_session_id):
    if user_id is not None:
        return Cookbook.query.filter_by(user_id=user_id)
    return Cookbook.query.filter_by(user_id=None, guest_session_id=guest_session_id)


def _require_auth_or_guest(f):
    """Inject user_id (None for guests) into the route function."""

    def wrapper(*args, **kwargs):
        return f(
            user_id=_current_user_id(),
            guest_session_id=_current_guest_session_id(),
            *args,
            **kwargs,
        )

    wrapper.__name__ = f.__name__
    return wrapper


# ── Routes ────────────────────────────────────────────────────────────────────


@collections_api_bp.route("", methods=["GET"])
@_require_auth_or_guest
def list_collections(user_id, guest_session_id):
    """List all cookbooks owned by the current user (or anonymous)."""
    try:
        ck = collections_list_key(user_id, guest_session_id)
        cached = safe_get(ck)
        if cached is not None:
            return jsonify(cached), 200

        cookbooks = (
            _scope_collections_query(user_id, guest_session_id)
            .order_by(Cookbook.created_at.desc())
            .all()
        )
        result = {
            "collections": [cb.to_dict() for cb in cookbooks],
            "user_id": user_id,
            "guest_session_id": guest_session_id,
        }
        safe_set(ck, result, timeout=TTL_SHORT)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Error listing collections: {e}")
        return jsonify({"error": "Failed to fetch collections"}), 500


@collections_api_bp.route("", methods=["POST"])
@_require_auth_or_guest
def create_collection(user_id, guest_session_id):
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
            guest_session_id=None if user_id is not None else guest_session_id,
            name=data["name"],
            description=data.get("description", ""),
            cover_image=data.get("coverImage"),
            recipe_ids=data.get("recipeIds", []),
        )
        db.session.add(cookbook)
        db.session.commit()
        invalidate_collection(user_id, guest_session_id)
        return jsonify(cookbook.to_dict()), 201
    except Exception as e:
        logger.error(f"Error creating collection: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to create collection"}), 500


@collections_api_bp.route("/<collection_id>", methods=["GET"])
@_require_auth_or_guest
def get_collection(user_id, guest_session_id, collection_id):
    """Get a specific cookbook by ID."""
    try:
        ck = collection_key(user_id, guest_session_id, collection_id)
        cached = safe_get(ck)
        if cached is not None:
            return jsonify(cached), 200

        cookbook = (
            _scope_collections_query(user_id, guest_session_id).filter_by(id=collection_id).first()
        )
        if not cookbook:
            return jsonify({"error": "Collection not found"}), 404
        result = cookbook.to_dict()
        safe_set(ck, result, timeout=TTL_MEDIUM)
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Error fetching collection {collection_id}: {e}")
        return jsonify({"error": "Failed to fetch collection"}), 500


@collections_api_bp.route("/<collection_id>", methods=["DELETE"])
@_require_auth_or_guest
def delete_collection(user_id, guest_session_id, collection_id):
    """Delete a cookbook."""
    try:
        cookbook = (
            _scope_collections_query(user_id, guest_session_id).filter_by(id=collection_id).first()
        )
        if not cookbook:
            return jsonify({"error": "Collection not found"}), 404
        db.session.delete(cookbook)
        db.session.commit()
        invalidate_collection(user_id, guest_session_id, collection_id)
        return jsonify({"message": "Collection deleted"}), 200
    except Exception as e:
        logger.error(f"Error deleting collection {collection_id}: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to delete collection"}), 500


@collections_api_bp.route("/<collection_id>/recipes", methods=["POST"])
@_require_auth_or_guest
def add_recipe_to_collection(user_id, guest_session_id, collection_id):
    """
    Add a recipe ID to a cookbook.

    Request body: {"recipe_id": "<uuid>"}
    """
    try:
        data = request.get_json()
        recipe_id = data.get("recipe_id") if data else None
        if not recipe_id:
            return jsonify({"error": "recipe_id is required"}), 400

        cookbook = (
            _scope_collections_query(user_id, guest_session_id).filter_by(id=collection_id).first()
        )
        if not cookbook:
            return jsonify({"error": "Collection not found"}), 404

        ids = list(cookbook.recipe_ids or [])
        if recipe_id not in ids:
            ids.append(recipe_id)
            cookbook.recipe_ids = ids
            cookbook.updated_at = datetime.utcnow()
            db.session.commit()

        invalidate_collection(user_id, guest_session_id, collection_id)
        return jsonify(cookbook.to_dict()), 200
    except Exception as e:
        logger.error(f"Error adding recipe to collection {collection_id}: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to update collection"}), 500


@collections_api_bp.route("/<collection_id>/recipes/<recipe_id>", methods=["DELETE"])
@_require_auth_or_guest
def remove_recipe_from_collection(user_id, guest_session_id, collection_id, recipe_id):
    """Remove a recipe ID from a cookbook."""
    try:
        cookbook = (
            _scope_collections_query(user_id, guest_session_id).filter_by(id=collection_id).first()
        )
        if not cookbook:
            return jsonify({"error": "Collection not found"}), 404

        ids = [r for r in (cookbook.recipe_ids or []) if r != recipe_id]
        cookbook.recipe_ids = ids
        cookbook.updated_at = datetime.utcnow()
        db.session.commit()

        invalidate_collection(user_id, guest_session_id, collection_id)
        return jsonify(cookbook.to_dict()), 200
    except Exception as e:
        logger.error(f"Error removing recipe from collection {collection_id}: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to update collection"}), 500
