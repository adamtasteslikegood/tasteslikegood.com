import json
import logging
import datetime
import base64
import os
from functools import wraps
from flask import Blueprint, request, jsonify, abort

from google.auth.transport import requests as g_requests
from google.oauth2 import id_token

from config import GCS_BUCKET_NAME
from repositories import db_recipe_repository
from blueprints.generation_bp import attempt_recipe_generation, build_generation_prompt
from utils.cache_utils import invalidate_recipe, invalidate_recipe_image
from services.gemini_service import get_genai_client

logger = logging.getLogger(__name__)

worker_api_bp = Blueprint('worker_api_bp', __name__, url_prefix='/api/worker')

# OIDC token verification for Pub/Sub push messages.
# Pub/Sub signs each push with a JWT issued for the configured push service
# account. We verify the signature and the email claim so that only Google
# Pub/Sub (acting as that SA) can invoke these endpoints. Without this,
# /api/worker/* would be open to any unauthenticated POST on the public Cloud
# Run URL.
PUBSUB_INVOKER_SA = os.environ.get("PUBSUB_INVOKER_SA")
PUBSUB_AUTH_OPTIONAL = os.environ.get("PUBSUB_AUTH_OPTIONAL", "0") == "1"

# In-process retries for one push message. Each attempt is a fresh model call
# (~10-25 s); 3 attempts stays well inside the Cloud Run request timeout and
# the push subscription's ack deadline.
def _parse_max_attempts(raw, default=3):
    """Parse the retry budget defensively: a misconfigured env var must not
    break blueprint import, and anything below 1 would skip generation
    entirely."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"Invalid GENERATION_MAX_ATTEMPTS {raw!r}; using {default}")
        return default
    return max(1, value)

GENERATION_MAX_ATTEMPTS = _parse_max_attempts(os.environ.get("GENERATION_MAX_ATTEMPTS", "3"))

def require_pubsub_oidc(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if PUBSUB_AUTH_OPTIONAL:
            return fn(*args, **kwargs)
        if not PUBSUB_INVOKER_SA:
            logger.error("PUBSUB_INVOKER_SA not set; refusing push request")
            abort(503)
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("Pub/Sub push missing Bearer token")
            abort(401)
        token = auth_header.split(None, 1)[1].strip()
        try:
            claims = id_token.verify_oauth2_token(token, g_requests.Request())
        except ValueError as e:
            logger.warning(f"Pub/Sub OIDC verification failed: {e}")
            abort(401)
        if claims.get("email") != PUBSUB_INVOKER_SA or not claims.get("email_verified"):
            logger.warning(f"Pub/Sub OIDC email mismatch: {claims.get('email')}")
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

@worker_api_bp.route('/recipe', methods=['POST'])
@require_pubsub_oidc
def process_recipe():
    envelope = request.get_json()
    if not envelope:
        return jsonify({"status": "error", "message": "Bad Request: no JSON payload"}), 400

    message = envelope.get("message")
    if not message:
        return jsonify({"status": "error", "message": "Bad Request: missing 'message'"}), 400

    try:
        data_str = base64.b64decode(message["data"]).decode("utf-8")
        data = json.loads(data_str)
    except Exception as e:
        logger.error(f"Failed to decode push message: {e}")
        return jsonify({"status": "ok"}), 200

    recipe_id = data.get("recipe_id")
    if not recipe_id:
        return jsonify({"status": "ok"}), 200

    prompt = data.get("prompt")
    selected_model = data.get("model")
    user_id = data.get("user_id")
    guest_session_id = data.get("guest_session_id")

    logger.info(f"Processing recipe generation for {recipe_id}")

    try:
        full_prompt = build_generation_prompt(prompt)

        # The model intermittently returns malformed/truncated JSON (a single
        # bad sample at temperature 0.7); a fresh attempt usually succeeds, so
        # retry in-process rather than surfacing an error status the user has
        # to retry by hand.
        recipe_data = None
        recipe_json_str = None
        last_error = None
        for attempt in range(1, GENERATION_MAX_ATTEMPTS + 1):
            recipe_data, recipe_json_str, last_error = attempt_recipe_generation(
                full_prompt, selected_model
            )
            if recipe_data:
                break
            logger.warning(
                f"Recipe generation attempt {attempt}/{GENERATION_MAX_ATTEMPTS} "
                f"failed for {recipe_id}: {last_error}"
            )

        if not recipe_data:
            logger.error(f"Recipe generation failed for {recipe_id}: {last_error}")
            db_recipe_repository.update_recipe_status(recipe_id, "error", user_id, guest_session_id)
            return jsonify({"status": "ok"}), 200

        # Add metadata
        recipe_data["id"] = recipe_id
        recipe_data["user_id"] = user_id
        recipe_data["ai_metadata"] = {
            "recipe_generation": {
                "model": selected_model,
                "user_id": user_id,
                "user_display_name": "Background Worker",
                "is_authenticated": user_id is not None,
                "session_id": guest_session_id,
                "prompt": prompt,
                "timestamp": datetime.datetime.now().isoformat(),
                "success": True,
            },
            "image_generation": None,
            "stock_image_generation": None,
        }

        db_recipe_repository.update_recipe(recipe_id, recipe_data, user_id, guest_session_id)
        db_recipe_repository.update_recipe_status(recipe_id, "ready", user_id, guest_session_id)
        
        invalidate_recipe(user_id, guest_session_id, recipe_id)
        logger.info(f"Successfully generated recipe {recipe_id}")
        
        # Trigger image generation
        from services.pubsub_service import publish_message
        try:
            publish_message("image-generation", {
                "recipe_id": recipe_id,
                "user_id": user_id,
                "guest_session_id": guest_session_id,
                "force_regenerate": False
            })
            logger.info(f"Queued image generation for recipe {recipe_id}")
        except Exception as e:
            logger.error(f"Failed to queue image generation for {recipe_id}: {e}")

    except Exception as e:
        logger.error(f"Error processing recipe message: {e}")
        return jsonify({"status": "ok"}), 200
        
    return jsonify({"status": "ok"}), 200

@worker_api_bp.route('/image', methods=['POST'])
@require_pubsub_oidc
def process_image():
    envelope = request.get_json()
    if not envelope:
        return jsonify({"status": "error", "message": "Bad Request: no JSON payload"}), 400

    message = envelope.get("message")
    if not message:
        return jsonify({"status": "error", "message": "Bad Request: missing 'message'"}), 400

    try:
        data_str = base64.b64decode(message["data"]).decode("utf-8")
        data = json.loads(data_str)
    except Exception as e:
        logger.error(f"Failed to decode push message: {e}")
        return jsonify({"status": "ok"}), 200

    recipe_id = data.get("recipe_id")
    if not recipe_id:
        return jsonify({"status": "ok"}), 200

    user_id = data.get("user_id")
    guest_session_id = data.get("guest_session_id")
    force_regenerate = data.get("force_regenerate", False)

    logger.info(f"Processing image generation for {recipe_id}")

    try:
        recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, guest_session_id)
        if not recipe:
            logger.error(f"Recipe not found: {recipe_id}")
            return jsonify({"status": "ok"}), 200

        recipe_data = recipe.data or {}
        
        # Check if image already exists
        has_real_image = bool(recipe_data.get("ai_image_data") or recipe_data.get("ai_image_gcs"))
        if not force_regenerate and has_real_image and recipe_data.get("ai_image_url"):
            logger.info(f"Image already exists for {recipe_id}")
            return jsonify({"status": "ok"}), 200

        client = get_genai_client(None)

        if not client:
            logger.error("No AI credentials available for worker")
            return jsonify({"status": "ok"}), 200

        recipe_name = recipe_data.get("name", "vegan dish")
        image_keywords = recipe_data.get("image_keywords", [])

        keyword_str = ", ".join(image_keywords) if image_keywords else ""
        image_prompt = (
            f"Professional food photography of {recipe_name}. "
            f"{keyword_str}. "
            f"High resolution, photorealistic, natural lighting, overhead shot, "
            f"delicious plating."
        )

        try:
            response = client.models.generate_images(
                model="imagen-4.0-generate-001",
                prompt=image_prompt,
                config={"number_of_images": 1},
            )
            
            if not response.generated_images:
                raise Exception("No images generated")
                
            image_bytes = response.generated_images[0].image.image_bytes
            image_url = f"/api/recipes/{recipe_id}/image"

            if GCS_BUCKET_NAME:
                from services.gcs_service import upload_image
                gcs_uri = upload_image(GCS_BUCKET_NAME, recipe_id, image_bytes)
                if not gcs_uri:
                    raise Exception("Failed to upload image to storage")
                recipe_data["ai_image_gcs"] = gcs_uri
                recipe_data.pop("ai_image_data", None)
            else:
                image_b64 = base64.b64encode(image_bytes).decode("ascii")
                recipe_data["ai_image_data"] = image_b64

            recipe_data["ai_image_url"] = image_url

            if "ai_metadata" not in recipe_data:
                recipe_data["ai_metadata"] = {}

            recipe_data["ai_metadata"]["image_generation"] = {
                "model": "imagen-4.0-generate-001",
                "user_id": user_id,
                "user_display_name": "Background Worker",
                "is_authenticated": user_id is not None,
                "session_id": guest_session_id,
                "prompt": image_prompt,
                "timestamp": datetime.datetime.now().isoformat(),
                "success": True,
            }

            db_recipe_repository.update_recipe(recipe_id, recipe_data, user_id, guest_session_id)
            invalidate_recipe_image(recipe_id)
            invalidate_recipe(user_id, guest_session_id, recipe_id)

            logger.info(f"Successfully generated image for recipe {recipe_id}")

        except Exception as e:
            logger.error(f"Image generation failed for {recipe_id}: {e}")
            if "ai_metadata" not in recipe_data:
                recipe_data["ai_metadata"] = {}
            recipe_data["ai_metadata"]["image_generation"] = {
                "success": False,
                "error": str(e),
                "timestamp": datetime.datetime.now().isoformat()
            }
            db_recipe_repository.update_recipe(recipe_id, recipe_data, user_id, guest_session_id)

    except Exception as e:
        logger.error(f"Error processing image message: {e}")

    return jsonify({"status": "ok"}), 200
