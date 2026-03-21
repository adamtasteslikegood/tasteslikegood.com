"""
Generation API Blueprint — JSON endpoints for AI recipe and image generation.

Provides endpoints for the Angular frontend (via Express proxy):
- POST /api/generate         Generate a recipe (returns JSON, saves to DB)
- POST /api/generate_image   Generate an AI image for a DB recipe (saves to DB)
- GET  /api/recipes/<id>/image   Serve a recipe's AI image from the DB
"""

import base64
import datetime
import logging
import uuid

from flask import Blueprint, Response, jsonify, request, session

from config import DEFAULT_MODEL
from blueprints.generation_bp import (
    build_generation_prompt,
    attempt_recipe_generation,
    validate_generation_input,
)
from extensions import cache
from repositories import db_recipe_repository
from services.gemini_service import get_genai_client
from utils.cache_utils import (
    recipe_image_key, invalidate_recipe, invalidate_recipe_image,
    TTL_IMAGE,
)
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

    # Generate recipe using dual auth strategy
    recipe_data, recipe_json_str, last_error = attempt_recipe_generation(
        full_prompt, selected_model
    )

    if not recipe_data:
        logger.error(f"Recipe generation failed: {last_error}")
        return jsonify({"error": f"Generation failed: {last_error}"}), 500

    # Assign an ID
    recipe_id = str(uuid.uuid4())
    recipe_data["id"] = recipe_id

    # Add metadata
    user_metadata = get_user_metadata()
    recipe_data["user_id"] = user_metadata["user_id"]
    recipe_data["ai_metadata"] = {
        "recipe_generation": {
            "model": selected_model,
            "user_id": user_metadata["user_id"],
            "user_display_name": user_metadata["display_name"],
            "is_authenticated": user_metadata["is_authenticated"],
            "session_id": user_metadata["session_id"],
            "prompt": prompt,
            "timestamp": datetime.datetime.now().isoformat(),
            "success": True,
        },
        "image_generation": None,
        "stock_image_generation": None,
    }

    # Save to database
    user_id = _current_user_id()
    guest_session_id = _current_guest_session_id()

    db_recipe = db_recipe_repository.create_recipe(recipe_data, user_id, guest_session_id)

    if not db_recipe:
        return jsonify({"error": "Failed to save recipe to database"}), 500

    invalidate_recipe(user_id, guest_session_id, recipe_id)

    logger.info(f"Generated and saved recipe '{recipe_data.get('name')}' (id={recipe_id})")

    return jsonify({"recipe": recipe_data}), 201


@generation_api_bp.route("/generate_image", methods=["POST"])
def generate_image_for_recipe():
    """
    Generate an AI image for a recipe stored in the database.
    Image bytes are stored as base64 in the recipe's data JSON (Cloud Run safe).

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

    # Check if image already exists
    if not force_regenerate and recipe_data.get("ai_image_url"):
        return jsonify({"image_url": recipe_data["ai_image_url"]}), 200

    # Get authenticated client
    session_credentials = session.get("credentials") if session else None
    client = get_genai_client(session_credentials)

    if not client:
        return jsonify({"error": "No AI credentials available"}), 500

    try:
        recipe_name = recipe_data.get("name", "vegan dish")
        image_keywords = recipe_data.get("image_keywords", [])

        # Build image prompt from keywords if available
        keyword_str = ", ".join(image_keywords) if image_keywords else ""
        image_prompt = (
            f"Professional food photography of {recipe_name}. "
            f"{keyword_str}. "
            f"High resolution, photorealistic, natural lighting, overhead shot, "
            f"delicious plating."
        )

        logger.info(f"Generating image for recipe '{recipe_name}' (id={recipe_id})")

        # Generate image via Imagen
        response = client.models.generate_images(
            model="imagen-4.0-generate-001",
            prompt=image_prompt,
            config={
                "number_of_images": 1,
            },
        )

        if not response.generated_images:
            return jsonify({"error": "No images generated"}), 500

        # Store image bytes as base64 in the recipe's data JSON.
        # This persists in Cloud SQL and survives Cloud Run restarts.
        image_bytes = response.generated_images[0].image.image_bytes
        image_b64 = base64.b64encode(image_bytes).decode("ascii")

        image_url = f"/api/recipes/{recipe_id}/image"

        # Update recipe in DB
        user_metadata = get_user_metadata()
        recipe_data["ai_image_url"] = image_url
        recipe_data["ai_image_data"] = image_b64

        if "ai_metadata" not in recipe_data:
            recipe_data["ai_metadata"] = {}

        recipe_data["ai_metadata"]["image_generation"] = {
            "model": "imagen-4.0-generate-001",
            "user_id": user_metadata["user_id"],
            "user_display_name": user_metadata["display_name"],
            "is_authenticated": user_metadata["is_authenticated"],
            "session_id": user_metadata["session_id"],
            "prompt": image_prompt,
            "timestamp": datetime.datetime.now().isoformat(),
            "success": True,
        }

        db_recipe_repository.update_recipe(recipe_id, recipe_data, user_id, guest_session_id)

        invalidate_recipe_image(recipe_id)
        invalidate_recipe(user_id, guest_session_id, recipe_id)

        logger.info(f"Generated image for recipe '{recipe_name}': {image_url}")
        return jsonify({"image_url": image_url}), 200

    except Exception as e:
        logger.error(f"Image generation error for recipe {recipe_id}: {e}")
        return jsonify({"error": str(e)}), 500


@generation_api_bp.route("/recipes/<recipe_id>/image", methods=["GET"])
def serve_recipe_image(recipe_id):
    """
    Serve a recipe's AI-generated image from the database.
    Returns the raw image bytes with appropriate Content-Type.
    Cached in Valkey for 24 hours to avoid repeated base64 decoding.
    """
    from models import Recipe

    # Check cache first (stores raw bytes)
    ck = recipe_image_key(recipe_id)
    cached_bytes = cache.get(ck)
    if cached_bytes is not None:
        return Response(
            cached_bytes,
            mimetype="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    recipe = Recipe.query.filter_by(id=recipe_id).first()
    if not recipe:
        return jsonify({"error": "Recipe not found"}), 404

    recipe_data = recipe.data or {}
    image_b64 = recipe_data.get("ai_image_data")
    if not image_b64:
        return jsonify({"error": "No image available"}), 404

    image_bytes = base64.b64decode(image_b64)
    cache.set(ck, image_bytes, timeout=TTL_IMAGE)

    return Response(
        image_bytes,
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@generation_api_bp.route("/admin/migrate-images", methods=["POST"])
def migrate_image_urls():
    """
    One-time migration: fix old recipes that have data: URLs or /static/ paths
    instead of the /api/recipes/<id>/image pattern.

    - data:image/...;base64,... → extract base64 into ai_image_data, set URL to API path
    - /static/images/... → set URL to API path (if ai_image_data already exists)
    - Missing ai_image_url but has ai_image_data → set URL to API path

    Returns summary of migrated recipes.
    """
    from models import Recipe
    from extensions import db

    try:
        recipes = Recipe.query.all()
        migrated = []

        for recipe in recipes:
            data = recipe.data or {}
            url = data.get("ai_image_url", "")
            changed = False
            api_url = f"/api/recipes/{recipe.id}/image"

            # Case 1: data: URL — extract base64 and fix URL
            if url and url.startswith("data:image/"):
                # Extract base64 from data URL (format: data:image/png;base64,AAAA...)
                parts = url.split(",", 1)
                if len(parts) == 2:
                    data["ai_image_data"] = parts[1]
                data["ai_image_url"] = api_url
                changed = True

            # Case 2: /static/ file path — fix URL (image data should already be in DB)
            elif url and url.startswith("/static/"):
                data["ai_image_url"] = api_url
                changed = True

            # Case 3: No URL but has image data — set the URL
            elif not url and data.get("ai_image_data"):
                data["ai_image_url"] = api_url
                changed = True

            if changed:
                recipe.data = data
                migrated.append({"id": recipe.id, "name": recipe.name, "new_url": api_url})

        if migrated:
            db.session.commit()

        logger.info("Image URL migration complete: %d recipes updated", len(migrated))
        return jsonify({
            "migrated": len(migrated),
            "recipes": migrated,
        }), 200

    except Exception as e:
        db.session.rollback()
        logger.error("Image URL migration failed: %s", e)
        return jsonify({"error": str(e)}), 500


@generation_api_bp.route("/recipes/missing-images", methods=["GET"])
def list_recipes_missing_images():
    """
    List recipes that have no valid AI image (no ai_image_data in DB).
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
        if not data.get("ai_image_data"):
            missing.append({
                "id": recipe.id,
                "name": recipe.name,
                "ai_image_url": data.get("ai_image_url"),
            })

    return jsonify({"recipes": missing, "count": len(missing)}), 200
