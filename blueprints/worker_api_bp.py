from copy import deepcopy
import logging

from flask import Blueprint, jsonify
from google.auth.transport import requests as g_requests
from google.genai import types as genai_types
from google.genai.errors import APIError, ClientError, ServerError
from google.oauth2 import id_token
from httpx import TransportError

from blueprints.generation_bp import attempt_recipe_generation, build_generation_prompt
from config import (
    GCS_BUCKET_NAME,
    GENAI_HTTP_TIMEOUT_MS,
    IMAGE_MODEL,
    WORKER_CLAIM_STALE_SECONDS,
)
from repositories import db_recipe_repository
from services.gemini_service import get_genai_client
from services.image_service import extract_image_bytes
from services.worker_orchestration import (
    GENERATION_MAX_ATTEMPTS,
    PUBSUB_AUTH_OPTIONAL,
    PUBSUB_INVOKER_SA,
    RECIPE_GENERATION_ATTEMPT_TIMEOUT_MS,
    _RECIPE_GENERATION_LEASE_MS,
    _RECIPE_GENERATION_OVERHEAD_MS,
    RetryableImageError,
    _build_image_generation_prompt,
    _build_recipe_generation_metadata,
    _claim_image_worker,
    _completed_image_request,
    _current_recipe_scope,
    _decode_pubsub_push,
    _delete_replaced_gcs_image,
    _delete_unpersisted_gcs_image,
    _execute_image_generation,
    _existing_recipe_delivery_response,
    _image_enqueue_pending,
    _image_generation_metadata,
    _image_request_status,
    _is_retryable_image_error,
    _parse_max_attempts,
    _prepare_image_delivery,
    _publish_image_generation,
    _record_image_failure,
    _release_worker_claim,
    _run_recipe_generation_attempts,
    require_pubsub_oidc,
)
from utils.cache_utils import invalidate_recipe, invalidate_recipe_image
from utils.log_sanitizer import sanitize_log_value

logger = logging.getLogger(__name__)

worker_api_bp = Blueprint("worker_api_bp", __name__, url_prefix="/api/worker")


@worker_api_bp.route("/recipe", methods=["POST"])
@require_pubsub_oidc
def process_recipe():
    data, early_response = _decode_pubsub_push()
    if early_response is not None:
        return early_response
    assert data is not None

    recipe_id = data.get("recipe_id")
    if not recipe_id:
        return jsonify({"status": "ok"}), 200

    prompt = data.get("prompt")
    selected_model = data.get("model")
    # A message without the field is from the web app, or was queued before
    # the field existed: both get a photo.
    with_image = data.get("with_image") is not False
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
        recipe_data, _recipe_json_str, last_error = _run_recipe_generation_attempts(
            recipe_id,
            claim_token,
            full_prompt,
            selected_model,
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
            # KAN-151: terminal failure. The success path below invalidates
            # after update_recipe_for_worker; without the same call here a
            # payload cached mid-generation keeps answering "processing" for
            # the full TTL after the generation has actually died.
            _, failed_user_id, failed_guest_id = _current_recipe_scope(recipe_id)
            invalidate_recipe(failed_user_id, failed_guest_id, recipe_id)
            return jsonify({"status": "ok"}), 200

        recipe, user_id, guest_session_id = _current_recipe_scope(recipe_id)
        if recipe is None:
            return jsonify({"status": "ok"}), 200

        # Add metadata
        recipe_data["id"] = recipe_id
        recipe_data["user_id"] = user_id
        recipe_data["ai_metadata"] = _build_recipe_generation_metadata(
            user_id,
            guest_session_id,
            selected_model,
            prompt,
            with_image,
        )

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

        if not with_image:
            return jsonify({"status": "ok"}), 200

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
    data, early_response = _decode_pubsub_push()
    if early_response is not None:
        return early_response
    assert data is not None

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
        prepared, prep_response = _prepare_image_delivery(
            recipe_id,
            force_regenerate,
            image_request_id,
        )
        if prep_response is not None:
            return prep_response
        recipe, _user_id, _guest_session_id, claim_token = prepared
        recipe_data = deepcopy(recipe.data or {})

        gen_response = _execute_image_generation(
            recipe_id,
            claim_token,
            image_request_id,
            force_regenerate,
            recipe_data,
        )
        if gen_response is not None:
            return gen_response

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


@worker_api_bp.route("/metadata", methods=["POST"])
@require_pubsub_oidc
def process_metadata():
    """Build search metadata for one published recipe (KAN-388).

    A 500 or 503 asks Pub/Sub to redeliver: the model call failed or another
    delivery holds it, and the rule version is already saved. Everything else
    is acknowledged.
    """
    from extensions import db
    from models.recipe import Recipe
    from services.recipe_metadata import MetadataBuildBusy, build_metadata

    data, early_response = _decode_pubsub_push(silent=True)
    if early_response is not None:
        return early_response
    assert data is not None
    recipe_id = data.get("recipe_id")

    recipe = db.session.get(Recipe, recipe_id) if isinstance(recipe_id, str) else None
    if recipe is None or not recipe.is_public:
        return jsonify({"status": "ok"}), 200
    try:
        build_metadata(recipe)
    except MetadataBuildBusy:
        # Another delivery holds the model call, or the cache could not say.
        # Not finished work: redelivery finds the model version once it is saved.
        db.session.rollback()
        return jsonify({"status": "busy"}), 503
    except ClientError as e:
        if e.code not in (408, 429):
            # The request itself was refused (bad model name, bad key): the same
            # message would fail the same way, and the rule version is saved.
            db.session.rollback()
            logger.error(
                "Metadata model call refused for recipe %s: %s",
                sanitize_log_value(recipe_id),
                sanitize_log_value(e),
            )
            return jsonify({"status": "ok"}), 200
        db.session.rollback()
        return jsonify({"status": "error"}), 500
    except Exception as e:
        db.session.rollback()
        logger.error(
            "Metadata build failed for recipe %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        return jsonify({"status": "error"}), 500
    return jsonify({"status": "ok"}), 200


__all__ = [
    "APIError",
    "ClientError",
    "GCS_BUCKET_NAME",
    "GENAI_HTTP_TIMEOUT_MS",
    "GENERATION_MAX_ATTEMPTS",
    "IMAGE_MODEL",
    "PUBSUB_AUTH_OPTIONAL",
    "PUBSUB_INVOKER_SA",
    "RECIPE_GENERATION_ATTEMPT_TIMEOUT_MS",
    "RetryableImageError",
    "ServerError",
    "TransportError",
    "WORKER_CLAIM_STALE_SECONDS",
    "_RECIPE_GENERATION_LEASE_MS",
    "_RECIPE_GENERATION_OVERHEAD_MS",
    "_build_image_generation_prompt",
    "_build_recipe_generation_metadata",
    "_claim_image_worker",
    "_completed_image_request",
    "_current_recipe_scope",
    "_decode_pubsub_push",
    "_delete_replaced_gcs_image",
    "_delete_unpersisted_gcs_image",
    "_execute_image_generation",
    "_existing_recipe_delivery_response",
    "_image_enqueue_pending",
    "_image_generation_metadata",
    "_image_request_status",
    "_is_retryable_image_error",
    "_parse_max_attempts",
    "_prepare_image_delivery",
    "_publish_image_generation",
    "_record_image_failure",
    "_release_worker_claim",
    "_run_recipe_generation_attempts",
    "attempt_recipe_generation",
    "build_generation_prompt",
    "db_recipe_repository",
    "extract_image_bytes",
    "g_requests",
    "genai_types",
    "get_genai_client",
    "id_token",
    "invalidate_recipe",
    "invalidate_recipe_image",
    "process_image",
    "process_metadata",
    "process_recipe",
    "require_pubsub_oidc",
    "worker_api_bp",
]
