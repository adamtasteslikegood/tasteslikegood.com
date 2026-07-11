"""
Generation API Blueprint — JSON endpoints for AI recipe and image generation.

Provides endpoints for the Angular frontend (via Express proxy):
- POST /api/generate         Generate a recipe (returns JSON, saves to DB)
- POST /api/generate_image   Generate an AI image for a DB recipe (saves to GCS/DB)
- GET  /api/recipes/<id>/image   Serve a recipe's AI image from GCS (or legacy base64)
"""

import base64
import datetime
import logging
import uuid

from flask import Blueprint, Response, jsonify, request, session

from config import DEFAULT_MODEL, GCS_BUCKET_NAME
from blueprints.generation_bp import (
    build_generation_prompt,
    attempt_recipe_generation,
    validate_generation_input,
)
from repositories import db_recipe_repository
from services.gemini_service import get_genai_client
from utils.cache_utils import (
    recipe_image_key,
    invalidate_recipe,
    invalidate_recipe_image,
    safe_get,
    safe_set,
    TTL_IMAGE,
)
from utils.admin_auth import require_admin
from utils.session_utils import get_or_create_session_id, get_user_metadata

logger = logging.getLogger(__name__)

generation_api_bp = Blueprint("generation_api", __name__, url_prefix="/api")


def _current_user_id():
    return session.get("user_id")


def _current_guest_session_id():
    return get_or_create_session_id()


@generation_api_bp.route("/generate", methods=["POST"])
def generate_recipe_json():
    """
    Generate a vegan recipe via AI and return it as JSON.

    Request body:
        {
            "prompt": "A hearty winter stew",
            "model": "gemini-2.5-flash"  (optional, defaults to config)
        }

    Returns:
        201: { "recipe": { ...recipe data... } }
        400: { "error": "..." }
        500: { "error": "..." }
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body is required"}), 400

    prompt = (data.get("prompt") or "").strip()
    is_valid, error_message = validate_generation_input(prompt)
    if not is_valid:
        return jsonify({"error": error_message}), 400

    selected_model = data.get("model", DEFAULT_MODEL)

    # Build prompt with schema
    full_prompt = build_generation_prompt(prompt)

    # Assign an ID
    recipe_id = str(uuid.uuid4())
    user_id = _current_user_id()
    guest_session_id = _current_guest_session_id()

    # Save a pending recipe to database
    pending_data = {
        "id": recipe_id,
        "name": "Generating...",
        "user_id": user_id
    }
    
    db_recipe = db_recipe_repository.create_recipe(pending_data, user_id, guest_session_id)

    if not db_recipe:
        return jsonify({"error": "Failed to save recipe to database"}), 500

    db_recipe_repository.update_recipe_status(recipe_id, "generating", user_id, guest_session_id)

    # Publish message to Pub/Sub
    from services.pubsub_service import publish_message
    
    message_data = {
        "recipe_id": recipe_id,
        "prompt": prompt,
        "model": selected_model,
        "user_id": user_id,
        "guest_session_id": guest_session_id
    }
    
    try:
        publish_message("recipe-generation", message_data)
        logger.info(f"Queued recipe generation (id={recipe_id})")
        return jsonify({"recipe_id": recipe_id, "status": "generating"}), 202
    except Exception as e:
        logger.error(f"Failed to publish recipe generation: {e}")
        db_recipe_repository.update_recipe_status(recipe_id, "error", user_id, guest_session_id)
        return jsonify({"error": "Failed to queue generation"}), 500


@generation_api_bp.route("/generate_image", methods=["POST"])
def generate_image_for_recipe():
    """
    Generate an AI image for a recipe stored in the database.
    When GCS_BUCKET_NAME is configured, image bytes are uploaded to GCS and only the
    GCS URI and API URL are stored in the recipe JSON. When GCS is not configured,
    image bytes are base64-encoded and stored directly in the recipe's data JSON.

    Request body:
        {
            "recipe_id": "uuid-string",
            "force_regenerate": false  (optional)
        }

    Returns:
        200: { "image_url": "/api/recipes/<id>/image" }
        400/404/500: { "error": "..." }
    """
    data = request.get_json()
    if not data or not data.get("recipe_id"):
        return jsonify({"error": "recipe_id is required"}), 400

    recipe_id = data["recipe_id"]
    force_regenerate = data.get("force_regenerate", False)

    user_id = _current_user_id()
    guest_session_id = _current_guest_session_id()

    # Load recipe from DB
    db_recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, guest_session_id)
    if not db_recipe:
        return jsonify({"error": "Recipe not found"}), 404

    recipe_data = db_recipe.data or {}

    # Check if image already exists (verify actual image data, not just URL)
    has_real_image = bool(recipe_data.get("ai_image_data") or recipe_data.get("ai_image_gcs"))
    if not force_regenerate and has_real_image and recipe_data.get("ai_image_url"):
        return jsonify({"image_url": recipe_data["ai_image_url"]}), 200

    from services.pubsub_service import publish_message
    
    message_data = {
        "recipe_id": recipe_id,
        "user_id": user_id,
        "guest_session_id": guest_session_id,
        "force_regenerate": force_regenerate
    }
    
    try:
        publish_message("image-generation", message_data)
        logger.info(f"Queued image generation for recipe (id={recipe_id})")
        return jsonify({"status": "generating_image"}), 202
    except Exception as e:
        logger.error(f"Failed to queue image generation for recipe {recipe_id}: {e}")
        return jsonify({"error": "Failed to queue image generation"}), 500


@generation_api_bp.route("/recipes/<recipe_id>/status", methods=["GET"])
def get_recipe_status(recipe_id):
    """
    Get the generation status of a recipe.
    """
    user_id = _current_user_id()
    guest_session_id = _current_guest_session_id()
    
    recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, guest_session_id)
    if not recipe:
        return jsonify({"error": "Recipe not found"}), 404
        
    return jsonify({
        "status": recipe.status,
        "recipe": recipe.data
    }), 200


@generation_api_bp.route("/recipes/<recipe_id>/image", methods=["GET"])
def serve_recipe_image(recipe_id):
    """
    Serve a recipe's AI-generated image.
    Tries in order: Valkey cache → GCS bucket → legacy base64 in DB.
    Cached in Valkey for 24 hours.

    Access rules:
        - If the recipe is public (``is_public=True``) anyone may fetch the image.
        - Otherwise the recipe must belong to the current user or guest session.
    """
    # Check Valkey cache first (stores raw bytes)
    ck = recipe_image_key(recipe_id)
    cached_bytes = safe_get(ck)
    if cached_bytes is not None:
        return Response(
            cached_bytes,
            mimetype="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # Public recipes bypass ownership scoping so unauthenticated SSR pages
    # and crawlers can still load the image.
    from models import Recipe

    recipe = Recipe.query.filter_by(id=recipe_id).first()
    if recipe is None:
        return jsonify({"error": "Recipe not found"}), 404

    if not recipe.is_public:
        user_id = _current_user_id()
        guest_session_id = _current_guest_session_id()
        recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, guest_session_id)
        if not recipe:
            return jsonify({"error": "Recipe not found"}), 404

    recipe_data = recipe.data or {}
    image_bytes = None

    # Try GCS first
    if GCS_BUCKET_NAME and recipe_data.get("ai_image_gcs"):
        from services.gcs_service import download_image

        image_bytes = download_image(GCS_BUCKET_NAME, recipe_id)

    # Fall back to legacy base64 in DB
    if image_bytes is None:
        image_b64 = recipe_data.get("ai_image_data")
        if image_b64:
            image_bytes = base64.b64decode(image_b64)

    if image_bytes is None:
        return jsonify({"error": "No image available"}), 404

    safe_set(ck, image_bytes, timeout=TTL_IMAGE)

    return Response(
        image_bytes,
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@generation_api_bp.route("/admin/image-audit", methods=["GET"])
def audit_recipe_images():
    """Diagnostic: show which recipes have/lack image data in the DB.
    Requires admin bearer token."""
    err = require_admin()
    if err:
        return err

    from models import Recipe

    recipes = Recipe.query.all()
    results = []
    for r in recipes:
        d = r.data or {}
        has_b64 = bool(d.get("ai_image_data"))
        has_gcs = bool(d.get("ai_image_gcs"))
        has_url = bool(d.get("ai_image_url"))
        has_id_in_data = "id" in d
        results.append(
            {
                "id": r.id,
                "name": r.name,
                "has_base64": has_b64,
                "has_gcs": has_gcs,
                "has_url_in_data": has_url,
                "url_in_data": d.get("ai_image_url", ""),
                "has_id_in_data": has_id_in_data,
                "has_any_image": has_b64 or has_gcs,
            }
        )

    missing = [r for r in results if not r["has_any_image"]]
    have_image = [r for r in results if r["has_any_image"]]
    return (
        jsonify(
            {
                "total": len(results),
                "with_image_data": len(have_image),
                "without_image_data": len(missing),
                "missing": missing,
            }
        ),
        200,
    )


@generation_api_bp.route("/admin/migrate-images", methods=["POST"])
def migrate_image_urls():
    """
    Migration endpoint: moves recipe images from base64-in-DB to GCS.
    Requires Authorization: Bearer <ADMIN_API_TOKEN>.

    For each recipe with ai_image_data (base64):
    1. Decode base64 to raw bytes
    2. Upload to GCS bucket
    3. Set ai_image_gcs URI and ai_image_url API path
    4. Remove ai_image_data from the JSON (frees DB space)

    Also fixes legacy URL patterns (data: URLs, /static/ paths).
    Returns summary of migrated recipes.
    """
    auth_error = require_admin()
    if auth_error:
        return auth_error
    from models import Recipe
    from extensions import db

    if not GCS_BUCKET_NAME:
        return jsonify({"error": "GCS_BUCKET_NAME not configured"}), 500

    from services.gcs_service import upload_image

    BATCH_SIZE = 100
    migrated = []
    errors = []

    try:
        offset = 0
        while True:
            recipes = Recipe.query.limit(BATCH_SIZE).offset(offset).all()
            if not recipes:
                break

            batch_changed = False
            for recipe in recipes:
                data = recipe.data or {}
                url = data.get("ai_image_url", "")
                api_url = f"/api/recipes/{recipe.id}/image"
                changed = False

                # Case 1: Has base64 data — migrate to GCS
                image_b64 = data.get("ai_image_data")
                if image_b64 and not data.get("ai_image_gcs"):
                    try:
                        # Handle data: URL prefix if present
                        if image_b64.startswith("data:image/"):
                            parts = image_b64.split(",", 1)
                            image_b64 = parts[1] if len(parts) == 2 else image_b64

                        image_bytes = base64.b64decode(image_b64)
                        gcs_uri = upload_image(GCS_BUCKET_NAME, recipe.id, image_bytes)
                        if gcs_uri:
                            data["ai_image_gcs"] = gcs_uri
                            data["ai_image_url"] = api_url
                            del data["ai_image_data"]
                            changed = True
                        else:
                            errors.append({"id": recipe.id, "error": "GCS upload failed"})
                    except Exception as e:
                        errors.append({"id": recipe.id, "error": str(e)})

                # Case 2: data: URL in ai_image_url — extract, upload, fix
                elif url and url.startswith("data:image/"):
                    try:
                        parts = url.split(",", 1)
                        if len(parts) == 2:
                            image_bytes = base64.b64decode(parts[1])
                            gcs_uri = upload_image(GCS_BUCKET_NAME, recipe.id, image_bytes)
                            if gcs_uri:
                                data["ai_image_gcs"] = gcs_uri
                                data["ai_image_url"] = api_url
                                data.pop("ai_image_data", None)
                                changed = True
                    except Exception as e:
                        errors.append({"id": recipe.id, "error": str(e)})

                # Case 3: /static/ path or missing URL but has GCS — fix URL
                elif (url and url.startswith("/static/")) or (not url and data.get("ai_image_gcs")):
                    data["ai_image_url"] = api_url
                    changed = True

                if changed:
                    recipe.data = data
                    migrated.append({"id": recipe.id, "name": recipe.name, "new_url": api_url})
                    batch_changed = True

            if batch_changed:
                db.session.commit()
            offset += BATCH_SIZE

        logger.info("Image migration complete: %d migrated, %d errors", len(migrated), len(errors))
        return (
            jsonify(
                {
                    "migrated": len(migrated),
                    "errors": len(errors),
                    "recipes": migrated,
                    "error_details": errors,
                }
            ),
            200,
        )

    except Exception as e:
        db.session.rollback()
        logger.error("Image migration failed: %s", e)
        return jsonify({"error": "Image migration failed"}), 500


@generation_api_bp.route("/recipes/missing-images", methods=["GET"])
def list_recipes_missing_images():
    """
    List recipes that have no valid AI image (no GCS or base64 image).
    Used by frontend to offer image regeneration.
    """
    from models import Recipe

    user_id = _current_user_id()
    guest_session_id = _current_guest_session_id()

    query = Recipe.query
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    elif guest_session_id:
        query = query.filter_by(user_id=None, guest_session_id=guest_session_id)

    recipes = query.all()
    missing = []
    for recipe in recipes:
        data = recipe.data or {}
        if not data.get("ai_image_gcs") and not data.get("ai_image_data"):
            missing.append(
                {
                    "id": recipe.id,
                    "name": recipe.name,
                    "ai_image_url": data.get("ai_image_url"),
                }
            )

    return jsonify({"recipes": missing, "count": len(missing)}), 200
