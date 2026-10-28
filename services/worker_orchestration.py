"""
Pub/Sub push worker orchestration, OIDC verification, lease management, and
recipe/image generation execution helpers (KAN-462).

Extracted from ``blueprints.worker_api_bp`` to keep route handlers thin while
preserving backwards-compatible symbols for existing tests.
"""

import base64
from copy import deepcopy
import datetime
from functools import wraps
import json
import logging
import os
import sys
from typing import Any, TypeVar, cast

from flask import abort, jsonify, request
from flask.typing import ResponseReturnValue
from google.auth.transport import requests as g_requests
from google.genai import types as genai_types
from google.genai.errors import APIError, ServerError
from google.oauth2 import id_token
from httpx import TransportError

from blueprints.generation_bp import attempt_recipe_generation
from config import (
    GCS_BUCKET_NAME,
    GENAI_HTTP_TIMEOUT_MS,
    IMAGE_MODEL,
    WORKER_CLAIM_STALE_SECONDS,
)
from repositories import db_recipe_repository
from services.gemini_service import get_genai_client
from services.image_service import extract_image_bytes
from utils.cache_utils import invalidate_recipe, invalidate_recipe_image
from utils.log_sanitizer import sanitize_log_value

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _bp_attr(name: str, default: _T) -> _T:
    """Resolve ``name`` from ``blueprints.worker_api_bp`` so test monkeypatches apply."""
    bp_mod = sys.modules.get("blueprints.worker_api_bp")
    return cast(_T, getattr(bp_mod, name, default)) if bp_mod is not None else default


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
def _parse_max_attempts(raw: Any, default: int = 3) -> int:
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
_RECIPE_GENERATION_LEASE_MS = WORKER_CLAIM_STALE_SECONDS * 1_000
_RECIPE_GENERATION_OVERHEAD_MS = min(
    30_000,
    max(0, _RECIPE_GENERATION_LEASE_MS - GENERATION_MAX_ATTEMPTS),
)
RECIPE_GENERATION_ATTEMPT_TIMEOUT_MS = min(
    GENAI_HTTP_TIMEOUT_MS,
    max(
        1,
        (_RECIPE_GENERATION_LEASE_MS - _RECIPE_GENERATION_OVERHEAD_MS) // GENERATION_MAX_ATTEMPTS,
    ),
)


class RetryableImageError(RuntimeError):
    """Image generation failed for a reason Pub/Sub should retry."""


def _decode_pubsub_push(
    *, silent: bool = False
) -> tuple[dict[str, Any] | None, ResponseReturnValue | None]:
    """Decode a Pub/Sub push envelope into ``(data, early_response)``."""
    envelope = (request.get_json(silent=True) or {}) if silent else request.get_json()
    if not silent and not envelope:
        return None, (
            jsonify({"status": "error", "message": "Bad Request: no JSON payload"}),
            400,
        )

    message = envelope.get("message") if isinstance(envelope, dict) else None
    if not message:
        return None, (
            jsonify({"status": "error", "message": "Bad Request: missing 'message'"}),
            400,
        )

    try:
        data_str = base64.b64decode(message["data"]).decode("utf-8")
        data = json.loads(data_str)
    except Exception as e:
        logger.error("Failed to decode push message: %s", sanitize_log_value(e))
        return None, (jsonify({"status": "ok"}), 200)

    if not isinstance(data, dict):
        return None, (jsonify({"status": "ok"}), 200)
    return data, None


def _current_recipe_scope(recipe_id):
    """Return the row and its current owner after any guest-to-user migration."""
    repo = _bp_attr("db_recipe_repository", db_recipe_repository)
    recipe = repo.get_recipe_for_worker(recipe_id)
    if recipe is None:
        return None, None, None
    return recipe, recipe.user_id, recipe.guest_session_id


def _release_worker_claim(recipe_id, status, claim_token, expected_status, worker_kind):
    """Release a lease after an unexpected failure if it is still ours."""
    if claim_token is None:
        return
    repo = _bp_attr("db_recipe_repository", db_recipe_repository)
    if not repo.set_recipe_status_for_worker(
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
    repo = _bp_attr("db_recipe_repository", db_recipe_repository)
    stale_seconds = _bp_attr("WORKER_CLAIM_STALE_SECONDS", WORKER_CLAIM_STALE_SECONDS)
    return repo.claim_recipe_for_worker(
        recipe_id,
        expected_status=status,
        processing_status="generating_image",
        stale_after_seconds=stale_seconds,
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
    metadata: dict[str, Any] = {
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
    repo = _bp_attr("db_recipe_repository", db_recipe_repository)
    updated = repo.patch_recipe_for_worker(
        recipe_id,
        {"ai_metadata": metadata},
        claim_token,
        status="ready",
        expected_status="generating_image",
    )
    if updated is None:
        return False

    # KAN-151: this is a terminal transition (status back to "ready" carrying
    # image_generation.success=False), so nothing downstream clears the cached
    # recipe payload the way the success path at _process_image_message does.
    # Invalidating in the helper covers both call sites at once.
    invalidate_fn = _bp_attr("invalidate_recipe", invalidate_recipe)
    invalidate_fn(updated.user_id, updated.guest_session_id, recipe_id)
    return True


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
    pending_fn = _bp_attr("_image_enqueue_pending", _image_enqueue_pending)
    publish_fn = _bp_attr("_publish_image_generation", _publish_image_generation)
    should_retry_enqueue = pending_fn(recipe.data) and (
        recipe.status == "ready"
        or (recipe.status == "generating_image" and recipe.worker_claim_token is None)
    )
    if should_retry_enqueue:
        try:
            publish_fn(recipe_id, user_id, guest_session_id)
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
    image_model = _bp_attr("IMAGE_MODEL", IMAGE_MODEL)
    metadata: dict[str, Any] = {
        "image_generation": {
            "model": image_model,
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
    retryable_cls = _bp_attr("RetryableImageError", RetryableImageError)
    if isinstance(
        error,
        (
            retryable_cls,
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
    bucket_name = _bp_attr("GCS_BUCKET_NAME", GCS_BUCKET_NAME)
    if (
        not bucket_name
        or not previous_gcs_uri
        or not current_gcs_uri
        or previous_gcs_uri == current_gcs_uri
    ):
        return
    from services.gcs_service import delete_image

    delete_image(bucket_name, recipe_id, previous_gcs_uri)


def _delete_unpersisted_gcs_image(recipe_id, uploaded_gcs_uri, image_persisted):
    if not uploaded_gcs_uri or image_persisted:
        return
    bucket_name = _bp_attr("GCS_BUCKET_NAME", GCS_BUCKET_NAME)
    from services.gcs_service import delete_image

    delete_image(bucket_name, recipe_id, uploaded_gcs_uri)


def _prepare_image_delivery(recipe_id, force_regenerate, image_request_id):
    scope_fn = _bp_attr("_current_recipe_scope", _current_recipe_scope)
    status_fn = _bp_attr("_image_request_status", _image_request_status)
    claim_fn = _bp_attr("_claim_image_worker", _claim_image_worker)
    release_fn = _bp_attr("_release_worker_claim", _release_worker_claim)

    recipe, user_id, guest_session_id = scope_fn(recipe_id)
    if recipe is None:
        logger.error("Recipe not found: %s", sanitize_log_value(recipe_id))
        return None, (jsonify({"status": "ok"}), 200)

    recipe_data = deepcopy(recipe.data or {})
    if image_request_id is not None and status_fn(recipe_data, image_request_id) != "pending":
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

    claim_token = claim_fn(recipe_id, recipe.status)
    if claim_token is None:
        return None, (
            jsonify({"status": "retry", "message": "Image is already processing"}),
            500,
        )

    recipe, user_id, guest_session_id = scope_fn(recipe_id)
    if recipe is None:
        return None, (jsonify({"status": "ok"}), 200)
    if image_request_id is not None and status_fn(recipe.data or {}, image_request_id) != "pending":
        release_fn(
            recipe_id,
            "ready",
            claim_token,
            "generating_image",
            "Image",
        )
        return None, (jsonify({"status": "ok"}), 200)
    return (recipe, user_id, guest_session_id, claim_token), None


def _run_recipe_generation_attempts(recipe_id, claim_token, full_prompt, selected_model):
    """Run in-process model retries while refreshing the worker lease each attempt."""
    repo = _bp_attr("db_recipe_repository", db_recipe_repository)
    max_attempts = _bp_attr("GENERATION_MAX_ATTEMPTS", GENERATION_MAX_ATTEMPTS)
    timeout_ms = _bp_attr(
        "RECIPE_GENERATION_ATTEMPT_TIMEOUT_MS", RECIPE_GENERATION_ATTEMPT_TIMEOUT_MS
    )
    attempt_fn = _bp_attr("attempt_recipe_generation", attempt_recipe_generation)

    recipe_data = None
    recipe_json_str = None
    last_error = None
    for attempt in range(1, max_attempts + 1):
        if not repo.set_recipe_status_for_worker(
            recipe_id,
            "processing",
            claim_token,
            expected_status="processing",
        ):
            raise RuntimeError("Recipe worker claim was lost")
        recipe_data, recipe_json_str, last_error = attempt_fn(
            full_prompt,
            selected_model,
            timeout_ms,
        )
        if recipe_data:
            break
        logger.warning(
            "Recipe generation attempt %d/%d failed for %s: %s",
            attempt,
            max_attempts,
            sanitize_log_value(recipe_id),
            sanitize_log_value(last_error),
        )
    return recipe_data, recipe_json_str, last_error


def _build_recipe_generation_metadata(
    user_id, guest_session_id, selected_model, prompt, with_image
) -> dict[str, Any]:
    """Assemble ``ai_metadata`` for a newly generated recipe row."""
    return {
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
        # "skipped" is not "pending", so a redelivered message does not
        # queue the photo either (_existing_recipe_delivery_response).
        "image_enqueue": {
            "status": "pending" if with_image else "skipped",
            "timestamp": datetime.datetime.now().isoformat(),
        },
        "stock_image_generation": None,
    }


def _build_image_generation_prompt(recipe_data: dict[str, Any]) -> str:
    """Build the food-photography prompt from recipe name and keywords."""
    recipe_name = recipe_data.get("name", "vegan dish")
    image_keywords = recipe_data.get("image_keywords")
    keyword_str = (
        ", ".join(keyword for keyword in image_keywords if isinstance(keyword, str))
        if isinstance(image_keywords, list)
        else ""
    )
    return (
        f"Professional food photography of {recipe_name}. "
        f"{keyword_str}. "
        f"High resolution, photorealistic, natural lighting, overhead shot, "
        f"delicious plating."
    )


def _execute_image_generation(
    recipe_id,
    claim_token,
    image_request_id,
    force_regenerate,
    recipe_data,
) -> ResponseReturnValue | None:
    """Run Imagen generation, upload/encode bytes, and patch the recipe row."""
    get_client_fn = _bp_attr("get_genai_client", get_genai_client)
    record_failure_fn = _bp_attr("_record_image_failure", _record_image_failure)
    extract_fn = _bp_attr("extract_image_bytes", extract_image_bytes)
    bucket_name = _bp_attr("GCS_BUCKET_NAME", GCS_BUCKET_NAME)
    image_model = _bp_attr("IMAGE_MODEL", IMAGE_MODEL)
    scope_fn = _bp_attr("_current_recipe_scope", _current_recipe_scope)
    completed_fn = _bp_attr("_completed_image_request", _completed_image_request)
    metadata_fn = _bp_attr("_image_generation_metadata", _image_generation_metadata)
    repo = _bp_attr("db_recipe_repository", db_recipe_repository)
    invalidate_img_fn = _bp_attr("invalidate_recipe_image", invalidate_recipe_image)
    invalidate_fn = _bp_attr("invalidate_recipe", invalidate_recipe)
    delete_replaced_fn = _bp_attr("_delete_replaced_gcs_image", _delete_replaced_gcs_image)
    delete_unpersisted_fn = _bp_attr("_delete_unpersisted_gcs_image", _delete_unpersisted_gcs_image)
    is_retryable_fn = _bp_attr("_is_retryable_image_error", _is_retryable_image_error)
    retryable_err_cls = _bp_attr("RetryableImageError", RetryableImageError)

    previous_gcs_uri = recipe_data.get("ai_image_gcs")
    client = get_client_fn(None)

    if not client:
        logger.error("No AI credentials available for worker")
        if not record_failure_fn(
            recipe_id,
            "No AI credentials available",
            claim_token,
            image_request_id,
            force_regenerate,
            retryable=True,
        ):
            raise RuntimeError("Image credential failure could not be persisted")
        return jsonify({"status": "retry"}), 500

    image_prompt = _build_image_generation_prompt(recipe_data)
    uploaded_gcs_uri = None
    image_persisted = False
    try:
        response = client.models.generate_content(
            model=image_model,
            contents=image_prompt,
            config=genai_types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

        image_bytes = extract_fn(response)
        if not image_bytes:
            raise retryable_err_cls("No images generated")
        image_url = f"/api/recipes/{recipe_id}/image"
        image_patch: dict[str, Any] = {"ai_image_url": image_url}
        remove_data_fields: tuple[str, ...] = ()

        if bucket_name:
            from services.gcs_service import upload_image

            uploaded_gcs_uri = upload_image(
                bucket_name,
                recipe_id,
                image_bytes,
                version=claim_token,
            )
            if not uploaded_gcs_uri:
                raise retryable_err_cls("Failed to upload image to storage")
            image_patch["ai_image_gcs"] = uploaded_gcs_uri
            remove_data_fields = ("ai_image_data",)
        else:
            image_patch["ai_image_data"] = base64.b64encode(image_bytes).decode("ascii")
            remove_data_fields = ("ai_image_gcs",)

        recipe, user_id, guest_session_id = scope_fn(recipe_id)
        if recipe is None:
            return jsonify({"status": "ok"}), 200
        completed_request_id, completed_force_regenerate = completed_fn(
            recipe.data or {},
            image_request_id,
            force_regenerate,
        )
        image_patch["ai_metadata"] = metadata_fn(
            user_id,
            guest_session_id,
            image_prompt,
            completed_request_id,
            completed_force_regenerate,
        )
        updated = repo.patch_recipe_for_worker(
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
        invalidate_img_fn(recipe_id)
        invalidate_fn(user_id, guest_session_id, recipe_id)
        delete_replaced_fn(recipe_id, previous_gcs_uri, uploaded_gcs_uri)

        logger.info(
            "Successfully generated image for recipe %s",
            sanitize_log_value(recipe_id),
        )
    except Exception as e:
        delete_unpersisted_fn(recipe_id, uploaded_gcs_uri, image_persisted)
        retryable = is_retryable_fn(e)
        logger.error(
            "Image generation failed for %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        if not record_failure_fn(
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

    return None


def require_pubsub_oidc(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_optional = _bp_attr("PUBSUB_AUTH_OPTIONAL", PUBSUB_AUTH_OPTIONAL)
        if auth_optional:
            return fn(*args, **kwargs)
        invoker_sa = _bp_attr("PUBSUB_INVOKER_SA", PUBSUB_INVOKER_SA)
        if not invoker_sa:
            logger.error("PUBSUB_INVOKER_SA not set; refusing push request")
            abort(503)
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("Pub/Sub push missing Bearer token")
            abort(401)
        token = auth_header.split(None, 1)[1].strip()
        id_token_mod = _bp_attr("id_token", id_token)
        g_requests_mod = _bp_attr("g_requests", g_requests)
        claims = None
        try:
            claims = id_token_mod.verify_oauth2_token(
                token,
                g_requests_mod.Request(),
                audience=request.base_url,
            )
        except ValueError as e:
            logger.warning(
                "Pub/Sub OIDC verification failed: %s",
                sanitize_log_value(e),
            )
            abort(401)
        if not claims or claims.get("email") != invoker_sa or not claims.get("email_verified"):
            logger.warning(
                "Pub/Sub OIDC email mismatch: %s",
                sanitize_log_value(claims.get("email") if claims else None),
            )
            abort(403)
        return fn(*args, **kwargs)

    return wrapper
