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

from blueprints.generation_bp import (
    build_generation_prompt,
    attempt_recipe_generation,
    validate_generation_input,
)
from config import DEFAULT_MODEL
from flask import Blueprint, Response, jsonify, request, session
from repositories import db_recipe_repository
from services.gemini_service import get_genai_client
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
    """
    from models import Recipe

    recipe = Recipe.query.filter_by(id=recipe_id).first()
    if not recipe:
        return jsonify({"error": "Recipe not found"}), 404

    recipe_data = recipe.data or {}
    image_b64 = recipe_data.get("ai_image_data")
    if not image_b64:
        return jsonify({"error": "No image available"}), 404

    image_bytes = base64.b64decode(image_b64)
    return Response(
        image_bytes,
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
