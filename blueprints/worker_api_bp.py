import json
import logging
import datetime
import base64
import os
from copy import deepcopy
from functools import wraps
from flask import Blueprint, request, jsonify, abort

from google.auth.transport import requests as g_requests
from google.genai.errors import APIError, ServerError
from google.oauth2 import id_token
from httpx import TransportError

from config import GCS_BUCKET_NAME, WORKER_CLAIM_STALE_SECONDS
from repositories import db_recipe_repository
from blueprints.generation_bp import attempt_recipe_generation, build_generation_prompt
from utils.cache_utils import invalidate_recipe, invalidate_recipe_image
from utils.log_sanitizer import sanitize_log_value
from services.gemini_service import get_genai_client

logger = logging.getLogger(__name__)

worker_api_bp = Blueprint("worker_api_bp", __name__, url_prefix="/api/worker")

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
        logger.warning(
            "Invalid GENERATION_MAX_ATTEMPTS %s; using %d",
            sanitize_log_value(repr(raw)),
            default,
        )
        return default
    return max(1, value)


GENERATION_MAX_ATTEMPTS = _parse_max_attempts(os.environ.get("GENERATION_MAX_ATTEMPTS", "3"))


class RetryableImageError(RuntimeError):
    """Image generation failed for a reason Pub/Sub should retry."""


def _current_recipe_scope(recipe_id):
    """Return the row and its current owner after any guest-to-user migration."""
    recipe = db_recipe_repository.get_recipe_for_worker(recipe_id)
    if recipe is None:
        return None, None, None
    return recipe, recipe.user_id, recipe.guest_session_id


def _release_worker_claim(recipe_id, status, claim_token, expected_status, worker_kind):
    """Release a lease after an unexpected failure if it is still ours."""
    if claim_token is None:
        return
    if not db_recipe_repository.set_recipe_status_for_worker(
        recipe_id,
        status,
        claim_token,
        expected_status=expected_status,
        release_claim=True,
    ):
        logger.info(
            "%s worker lease for %s was already superseded",
            worker_kind,
            sanitize_log_value(recipe_id),
        )


def _claim_image_worker(recipe_id, status):
    """Claim queued/current image work while still accepting legacy ready rows."""
    if status not in {"ready", "generating_image"}:
        return None
    return db_recipe_repository.claim_recipe_for_worker(
        recipe_id,
        expected_status=status,
        processing_status="generating_image",
        stale_after_seconds=WORKER_CLAIM_STALE_SECONDS,
    )


def _image_request_status(recipe_data, image_request_id):
    if image_request_id is None:
        return None
    metadata = (recipe_data or {}).get("ai_metadata")
    image_request = metadata.get("image_request") if isinstance(metadata, dict) else None
    if not isinstance(image_request, dict) or image_request.get("id") != image_request_id:
        return "superseded"
    return image_request.get("status")


def _completed_image_request(recipe_data, image_request_id, force_regenerate):
    if image_request_id is not None:
        return image_request_id, force_regenerate
    metadata = (recipe_data or {}).get("ai_metadata")
    image_request = metadata.get("image_request") if isinstance(metadata, dict) else None
    if (
        isinstance(image_request, dict)
        and isinstance(image_request.get("id"), str)
        and image_request.get("status") == "pending"
    ):
        return image_request["id"], image_request.get("force_regenerate") is True
    return None, force_regenerate


def _record_image_failure(
    recipe_id,
    error_message,
    claim_token,
    image_request_id=None,
    force_regenerate=False,
    retryable=False,
):
    """Persist image failure metadata without replacing user-owned recipe data."""
    timestamp = datetime.datetime.now().isoformat()
    metadata = {
        "image_generation": {
            "success": False,
            "error": error_message,
            "timestamp": timestamp,
        },
        "image_enqueue": {
            "status": "complete",
            "timestamp": timestamp,
        },
    }
    if image_request_id is not None:
        metadata["image_request"] = {
            "id": image_request_id,
            "status": "pending" if retryable else "complete",
            "force_regenerate": force_regenerate,
            "timestamp": timestamp,
        }
    return (
        db_recipe_repository.patch_recipe_for_worker(
            recipe_id,
            {"ai_metadata": metadata},
            claim_token,
            status="ready",
            expected_status="generating_image",
        )
        is not None
    )


def _image_enqueue_pending(recipe_data):
    metadata = (recipe_data or {}).get("ai_metadata")
    if not isinstance(metadata, dict):
        return False
    enqueue = metadata.get("image_enqueue")
    return isinstance(enqueue, dict) and enqueue.get("status") == "pending"


def _publish_image_generation(recipe_id, user_id, guest_session_id):
    from services.pubsub_service import publish_message

    publish_message(
        "image-generation",
        {
            "recipe_id": recipe_id,
            "user_id": user_id,
            "guest_session_id": guest_session_id,
            "force_regenerate": False,
        },
    )
    logger.info(
        "Queued image generation for recipe %s",
        sanitize_log_value(recipe_id),
    )


def _existing_recipe_delivery_response(recipe_id, recipe, user_id, guest_session_id):
    """Handle recipe-message redelivery after generation has already finished."""
    should_retry_enqueue = _image_enqueue_pending(recipe.data) and (
        recipe.status == "ready"
        or (recipe.status == "generating_image" and recipe.worker_claim_token is None)
    )
    if should_retry_enqueue:
        try:
            _publish_image_generation(recipe_id, user_id, guest_session_id)
        except Exception as e:
            logger.error(
                "Failed to requeue image generation for %s: %s",
                sanitize_log_value(recipe_id),
                sanitize_log_value(e),
            )
            return jsonify({"status": "retry"}), 500
        return jsonify({"status": "ok"}), 200
    if recipe.status == "ready":
        return jsonify({"status": "ok"}), 200
    if recipe.status in {"error", "generating_image"}:
        logger.info(
            "Skipping recipe generation for %s with terminal status %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(recipe.status),
        )
        return jsonify({"status": "ok"}), 200
    return None


def _image_generation_metadata(
    user_id,
    guest_session_id,
    image_prompt,
    image_request_id=None,
    force_regenerate=False,
):
    timestamp = datetime.datetime.now().isoformat()
    metadata = {
        "image_generation": {
            "model": "imagen-4.0-generate-001",
            "user_id": user_id,
            "user_display_name": "Background Worker",
            "is_authenticated": user_id is not None,
            "session_id": guest_session_id,
            "prompt": image_prompt,
            "timestamp": timestamp,
            "success": True,
        },
        "image_enqueue": {
            "status": "complete",
            "timestamp": timestamp,
        },
    }
    if image_request_id is not None:
        metadata["image_request"] = {
            "id": image_request_id,
            "status": "complete",
            "force_regenerate": force_regenerate,
            "timestamp": timestamp,
        }
    return metadata


def _is_retryable_image_error(error):
    if isinstance(
        error,
        (
            RetryableImageError,
            ServerError,
            TransportError,
            RuntimeError,
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    ):
        return True
    return isinstance(error, APIError) and error.code in {408, 409, 429}


def _delete_replaced_gcs_image(recipe_id, previous_gcs_uri, current_gcs_uri):
    if (
        not GCS_BUCKET_NAME
        or not previous_gcs_uri
        or not current_gcs_uri
        or previous_gcs_uri == current_gcs_uri
    ):
        return
    from services.gcs_service import delete_image

    delete_image(GCS_BUCKET_NAME, recipe_id, previous_gcs_uri)


def _delete_unpersisted_gcs_image(recipe_id, uploaded_gcs_uri, image_persisted):
    if not uploaded_gcs_uri or image_persisted:
        return
    from services.gcs_service import delete_image

    delete_image(GCS_BUCKET_NAME, recipe_id, uploaded_gcs_uri)


def _prepare_image_delivery(recipe_id, force_regenerate, image_request_id):
    recipe, user_id, guest_session_id = _current_recipe_scope(recipe_id)
    if recipe is None:
        logger.error("Recipe not found: %s", sanitize_log_value(recipe_id))
        return None, (jsonify({"status": "ok"}), 200)

    recipe_data = deepcopy(recipe.data or {})
    if (
        image_request_id is not None
        and _image_request_status(recipe_data, image_request_id) != "pending"
    ):
        logger.info(
            "Skipping completed or superseded image request %s for %s",
            sanitize_log_value(image_request_id),
            sanitize_log_value(recipe_id),
        )
        return None, (jsonify({"status": "ok"}), 200)

    has_real_image = bool(recipe_data.get("ai_image_data") or recipe_data.get("ai_image_gcs"))
    if not force_regenerate and has_real_image and recipe_data.get("ai_image_url"):
        logger.info("Image already exists for %s", sanitize_log_value(recipe_id))
        return None, (jsonify({"status": "ok"}), 200)

    claim_token = _claim_image_worker(recipe_id, recipe.status)
    if claim_token is None:
        return None, (
            jsonify({"status": "retry", "message": "Image is already processing"}),
            500,
        )

    recipe, user_id, guest_session_id = _current_recipe_scope(recipe_id)
    if recipe is None:
        return None, (jsonify({"status": "ok"}), 200)
    if (
        image_request_id is not None
        and _image_request_status(recipe.data or {}, image_request_id) != "pending"
    ):
        _release_worker_claim(
            recipe_id,
            "ready",
            claim_token,
            "generating_image",
            "Image",
        )
        return None, (jsonify({"status": "ok"}), 200)
    return (recipe, user_id, guest_session_id, claim_token), None


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
        claims = None
        try:
            claims = id_token.verify_oauth2_token(
                token,
                g_requests.Request(),
                audience=request.base_url,
            )
        except ValueError as e:
            logger.warning(
                "Pub/Sub OIDC verification failed: %s",
                sanitize_log_value(e),
            )
            abort(401)
        if (
            not claims
            or claims.get("email") != PUBSUB_INVOKER_SA
            or not claims.get("email_verified")
        ):
            logger.warning(
                "Pub/Sub OIDC email mismatch: %s",
                sanitize_log_value(claims.get("email") if claims else None),
            )
            abort(403)
        return fn(*args, **kwargs)

    return wrapper


@worker_api_bp.route("/recipe", methods=["POST"])
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
        logger.error("Failed to decode push message: %s", sanitize_log_value(e))
        return jsonify({"status": "ok"}), 200

    recipe_id = data.get("recipe_id")
    if not recipe_id:
        return jsonify({"status": "ok"}), 200

    prompt = data.get("prompt")
    selected_model = data.get("model")
    logger.info("Processing recipe generation for %s", sanitize_log_value(recipe_id))

    claim_token = None
    try:
        recipe, user_id, guest_session_id = _current_recipe_scope(recipe_id)
        if recipe is None:
            return jsonify({"status": "ok"}), 200
        existing_response = _existing_recipe_delivery_response(
            recipe_id,
            recipe,
            user_id,
            guest_session_id,
        )
        if existing_response is not None:
            return existing_response
        claim_token = db_recipe_repository.claim_recipe_for_worker(
            recipe_id,
            expected_status="generating",
            processing_status="processing",
            stale_after_seconds=WORKER_CLAIM_STALE_SECONDS,
        )
        if claim_token is None:
            return jsonify({"status": "retry", "message": "Recipe is already processing"}), 500

        full_prompt = build_generation_prompt(prompt)

        # The model intermittently returns malformed/truncated JSON (a single
        # bad sample at temperature 0.7); a fresh attempt usually succeeds, so
        # retry in-process rather than surfacing an error status the user has
        # to retry by hand.
        recipe_data = None
        recipe_json_str = None
        last_error = None
        for attempt in range(1, GENERATION_MAX_ATTEMPTS + 1):
            if not db_recipe_repository.set_recipe_status_for_worker(
                recipe_id,
                "processing",
                claim_token,
                expected_status="processing",
            ):
                raise RuntimeError("Recipe worker claim was lost")
            recipe_data, recipe_json_str, last_error = attempt_recipe_generation(
                full_prompt, selected_model
            )
            if recipe_data:
                break
            logger.warning(
                "Recipe generation attempt %d/%d failed for %s: %s",
                attempt,
                GENERATION_MAX_ATTEMPTS,
                sanitize_log_value(recipe_id),
                sanitize_log_value(last_error),
            )

        if not recipe_data:
            logger.error(
                "Recipe generation failed for %s: %s",
                sanitize_log_value(recipe_id),
                sanitize_log_value(last_error),
            )
            if not db_recipe_repository.set_recipe_status_for_worker(
                recipe_id,
                "error",
                claim_token,
                expected_status="processing",
                release_claim=True,
            ):
                raise RuntimeError("Recipe failure status could not be persisted")
            return jsonify({"status": "ok"}), 200

        recipe, user_id, guest_session_id = _current_recipe_scope(recipe_id)
        if recipe is None:
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
            "image_enqueue": {
                "status": "pending",
                "timestamp": datetime.datetime.now().isoformat(),
            },
            "stock_image_generation": None,
        }

        updated = db_recipe_repository.update_recipe_for_worker(
            recipe_id,
            recipe_data,
            claim_token,
            status="ready",
            expected_status="processing",
        )
        if updated is None:
            raise RuntimeError("Generated recipe could not be persisted")

        user_id = updated.user_id
        guest_session_id = updated.guest_session_id
        invalidate_recipe(user_id, guest_session_id, recipe_id)
        logger.info("Successfully generated recipe %s", sanitize_log_value(recipe_id))

        try:
            _publish_image_generation(recipe_id, user_id, guest_session_id)
        except Exception as e:
            logger.error(
                "Failed to queue image generation for %s: %s",
                sanitize_log_value(recipe_id),
                sanitize_log_value(e),
            )
            return jsonify({"status": "retry"}), 500

    except Exception as e:
        logger.error("Error processing recipe message: %s", sanitize_log_value(e))
        try:
            _release_worker_claim(
                recipe_id,
                "generating",
                claim_token,
                "processing",
                "Recipe",
            )
        except Exception as status_error:
            logger.error(
                "Failed to release recipe worker claim: %s",
                sanitize_log_value(status_error),
            )
        return jsonify({"status": "retry"}), 500

    return jsonify({"status": "ok"}), 200


@worker_api_bp.route("/image", methods=["POST"])
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
        logger.error("Failed to decode push message: %s", sanitize_log_value(e))
        return jsonify({"status": "ok"}), 200

    recipe_id = data.get("recipe_id")
    if not recipe_id:
        return jsonify({"status": "ok"}), 200

    force_regenerate = data.get("force_regenerate") is True
    raw_image_request_id = data.get("image_request_id")
    image_request_id = (
        raw_image_request_id
        if isinstance(raw_image_request_id, str) and raw_image_request_id
        else None
    )

    logger.info("Processing image generation for %s", sanitize_log_value(recipe_id))

    claim_token = None
    try:
        prepared, early_response = _prepare_image_delivery(
            recipe_id,
            force_regenerate,
            image_request_id,
        )
        if early_response is not None:
            return early_response
        recipe, user_id, guest_session_id, claim_token = prepared
        recipe_data = deepcopy(recipe.data or {})
        previous_gcs_uri = recipe_data.get("ai_image_gcs")

        client = get_genai_client(None)

        if not client:
            logger.error("No AI credentials available for worker")
            if not _record_image_failure(
                recipe_id,
                "No AI credentials available",
                claim_token,
                image_request_id,
                force_regenerate,
                retryable=True,
            ):
                raise RuntimeError("Image credential failure could not be persisted")
            return jsonify({"status": "retry"}), 500

        recipe_name = recipe_data.get("name", "vegan dish")
        image_keywords = recipe_data.get("image_keywords")
        keyword_str = (
            ", ".join(keyword for keyword in image_keywords if isinstance(keyword, str))
            if isinstance(image_keywords, list)
            else ""
        )
        image_prompt = (
            f"Professional food photography of {recipe_name}. "
            f"{keyword_str}. "
            f"High resolution, photorealistic, natural lighting, overhead shot, "
            f"delicious plating."
        )

        uploaded_gcs_uri = None
        image_persisted = False
        try:
            response = client.models.generate_images(
                model="imagen-4.0-generate-001",
                prompt=image_prompt,
                config={"number_of_images": 1},
            )

            if not response.generated_images:
                raise RetryableImageError("No images generated")

            image_bytes = response.generated_images[0].image.image_bytes
            image_url = f"/api/recipes/{recipe_id}/image"
            image_patch = {"ai_image_url": image_url}
            remove_data_fields: tuple[str, ...] = ()

            if GCS_BUCKET_NAME:
                from services.gcs_service import upload_image

                uploaded_gcs_uri = upload_image(
                    GCS_BUCKET_NAME,
                    recipe_id,
                    image_bytes,
                    version=claim_token,
                )
                if not uploaded_gcs_uri:
                    raise RetryableImageError("Failed to upload image to storage")
                image_patch["ai_image_gcs"] = uploaded_gcs_uri
                remove_data_fields = ("ai_image_data",)
            else:
                image_patch["ai_image_data"] = base64.b64encode(image_bytes).decode("ascii")
                remove_data_fields = ("ai_image_gcs",)

            recipe, user_id, guest_session_id = _current_recipe_scope(recipe_id)
            if recipe is None:
                return jsonify({"status": "ok"}), 200
            completed_request_id, completed_force_regenerate = _completed_image_request(
                recipe.data or {},
                image_request_id,
                force_regenerate,
            )
            image_patch["ai_metadata"] = _image_generation_metadata(
                user_id,
                guest_session_id,
                image_prompt,
                completed_request_id,
                completed_force_regenerate,
            )
            updated = db_recipe_repository.patch_recipe_for_worker(
                recipe_id,
                image_patch,
                claim_token,
                status="ready",
                expected_status="generating_image",
                remove_data_fields=remove_data_fields,
            )
            if updated is None:
                raise RuntimeError("Generated image could not be persisted")
            image_persisted = True
            user_id = updated.user_id
            guest_session_id = updated.guest_session_id
            invalidate_recipe_image(recipe_id)
            invalidate_recipe(user_id, guest_session_id, recipe_id)
            _delete_replaced_gcs_image(recipe_id, previous_gcs_uri, uploaded_gcs_uri)

            logger.info(
                "Successfully generated image for recipe %s",
                sanitize_log_value(recipe_id),
            )

        except Exception as e:
            _delete_unpersisted_gcs_image(recipe_id, uploaded_gcs_uri, image_persisted)
            retryable = _is_retryable_image_error(e)
            logger.error(
                "Image generation failed for %s: %s",
                sanitize_log_value(recipe_id),
                sanitize_log_value(e),
            )
            if not _record_image_failure(
                recipe_id,
                "Image generation failed",
                claim_token,
                image_request_id,
                force_regenerate,
                retryable=retryable,
            ):
                raise RuntimeError("Image generation failure could not be persisted")
            if retryable:
                return jsonify({"status": "retry"}), 500

    except Exception as e:
        logger.error("Error processing image message: %s", sanitize_log_value(e))
        try:
            _release_worker_claim(
                recipe_id,
                "ready",
                claim_token,
                "generating_image",
                "Image",
            )
        except Exception as status_error:
            logger.error(
                "Failed to release image worker claim: %s",
                sanitize_log_value(status_error),
            )
        return jsonify({"status": "retry"}), 500

    return jsonify({"status": "ok"}), 200
