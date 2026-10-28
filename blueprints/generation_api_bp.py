"""
Generation API Blueprint — JSON endpoints for AI recipe and image generation.

Provides endpoints for the Angular frontend (via Express proxy):
- POST /api/generate         Generate a recipe (returns JSON, saves to DB)
- POST /api/generate_image   Generate an AI image for a DB recipe (saves to GCS/DB)
- GET  /api/recipes/<id>/image   Serve a recipe's AI image from GCS (or legacy base64)
"""

import logging

from ddtrace import tracer
from flask import Blueprint, Response, jsonify, request

from blueprints.generation_bp import validate_generation_input
from config import DEFAULT_MODEL, GCS_BUCKET_NAME
from repositories import db_recipe_repository

# Imported eagerly (not inside the image helper) so the google.cloud.storage
# import cost lands at worker boot, not on the first image request of every
# worker lifetime: ~1 s of unspanned time in pin trace 475f7f88 (KAN-268).
from services import gcs_service
from services.generation_orchestration import (
    _collect_image_audit_summary,
    _current_guest_session_id,
    _current_user_id,
    _find_recipes_missing_images,
    _image_cache_version,
    _image_mimetype,
    _load_stored_image_bytes,
    _run_image_url_migration,
    _serve_derived_image,
    _serve_image_variant,
    _serve_pin_image,
    _traced_cache_get,
    queue_recipe_generation,
    queue_recipe_image,
)
from services.image_variants import (
    make_pin_variant,
    make_webp_variant,
    parse_pin_flag,
    parse_variant_width,
)
from utils.admin_auth import require_admin
from utils.cache_utils import (
    TTL_IMAGE,
    invalidate_recipe,
    recipe_image_key,
    recipe_image_pin_key,
    recipe_image_variant_key,
    safe_get,
    safe_set,
)
from utils.session_utils import get_or_create_session_id

logger = logging.getLogger(__name__)

generation_api_bp = Blueprint("generation_api", __name__, url_prefix="/api")


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
        202: { "recipe_id": "uuid-string", "status": "generating" }
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

    return queue_recipe_generation(
        prompt,
        data.get("model", DEFAULT_MODEL),
        _current_user_id(),
        _current_guest_session_id(),
    )


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
        200: { "image_url": "/api/recipes/<id>/image" } when an image already exists
        202: { "status": "generating_image" } after queueing; poll the recipe status endpoint
        400/404/500: { "error": "..." }
    """
    data = request.get_json()
    if not data or not data.get("recipe_id"):
        return jsonify({"error": "recipe_id is required"}), 400

    return queue_recipe_image(
        data["recipe_id"],
        data.get("force_regenerate") is True,
        _current_user_id(),
        _current_guest_session_id(),
    )


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

    return jsonify({"status": recipe.status, "recipe": recipe.data}), 200


@generation_api_bp.route("/recipes/<recipe_id>/image", methods=["GET"])
def serve_recipe_image(recipe_id):
    """
    Serve a recipe's AI-generated image.
    Access is checked first (existence + visibility/ownership), then the
    bytes are resolved: Valkey cache → GCS bucket → legacy base64 in DB.
    Cached in Valkey for 24 hours.

    Access rules:
        - If the recipe is public (``is_public=True``) anyone may fetch the image.
        - Otherwise the recipe must belong to the current user or guest session.

    Response hygiene (cookbook #3164): public-recipe responses are meant to be
    CDN/browser cacheable, so this endpoint is exempted from the app-wide
    session touch (no ``Set-Cookie``/``Vary: Cookie`` — see
    ``ensure_session_id`` in app.py) and CORS headers are stripped in
    ``create_app`` (images are consumed same-origin through the Express
    proxy; ACAO on them defeated shared caching).

    ``?w=400|600|800|1200`` (KAN-271, KAN-371) returns a WebP resized to that exact width —
    see ``_serve_image_variant``. Any other ``w`` is a 400. ``?pin=1``
    (KAN-284) returns the 1000x1500 Pinterest pin JPEG; ``pin`` with ``w``, or
    any other ``pin`` value, is a 400.
    """
    # Public recipes bypass ownership scoping so unauthenticated SSR pages
    # and crawlers can still load the image.
    from models import Recipe

    try:
        variant_width = parse_variant_width(request.args.get("w"))
    except ValueError:
        return jsonify({"error": "Unsupported image width"}), 400
    pin_values = request.args.getlist("pin")
    if len(pin_values) > 1:
        return jsonify({"error": "Duplicate pin parameter"}), 400
    try:
        pin = parse_pin_flag(pin_values[0] if pin_values else None)
    except ValueError:
        return jsonify({"error": "Unsupported pin value"}), 400
    if pin and variant_width is not None:
        return jsonify({"error": "pin and w cannot be combined"}), 400

    recipe = Recipe.query.filter_by(id=recipe_id).first()
    if recipe is None:
        return jsonify({"error": "Recipe not found"}), 404

    # Private images must not be stored by any HTTP cache — only Valkey,
    # behind the access check, may hold them.
    http_cache_control = "public, max-age=86400" if recipe.is_public else "private, no-store"

    if not recipe.is_public:
        user_id = _current_user_id()
        guest_session_id = _current_guest_session_id()
        recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, guest_session_id)
        if not recipe:
            return jsonify({"error": "Recipe not found"}), 404

    # Cache lookup must stay below the access check: the key is global
    # (same image for everyone), so serving on a hit without the check
    # would expose private/deleted recipes' images to anyone with the UUID.
    if variant_width is not None or pin:
        derived_response = (
            _serve_pin_image(recipe, http_cache_control)
            if pin
            else _serve_image_variant(recipe, variant_width, http_cache_control)
        )
        if derived_response is not None:
            return derived_response
        # No derived image (undecodable bytes, or a GCS read failed): serve the
        # original below rather than fail the page's image, but never let an
        # HTTP cache pin full-size bytes under this ``?w=``/``?pin=`` URL.
        http_cache_control = "no-store"

    ck = recipe_image_key(recipe_id, _image_cache_version(recipe))
    cached_bytes = _traced_cache_get(ck)
    if cached_bytes is not None:
        return Response(
            cached_bytes,
            mimetype=_image_mimetype(cached_bytes),
            headers={"Cache-Control": http_cache_control},
        )

    image_bytes = _load_stored_image_bytes(recipe)
    if image_bytes is None:
        return jsonify({"error": "No image available"}), 404

    # Synchronous on purpose: a background write would run between requests on
    # Cloud Run's throttled CPU, the same starvation KAN-268 found in the token
    # refresh thread. Spanned so its cost is attributable.
    with tracer.trace("image.cache_write") as span:
        span.set_metric("image.bytes", len(image_bytes))
        safe_set(ck, image_bytes, timeout=TTL_IMAGE)

    return Response(
        image_bytes,
        mimetype=_image_mimetype(image_bytes),
        headers={"Cache-Control": http_cache_control},
    )


@generation_api_bp.route("/admin/image-audit", methods=["GET"])
def audit_recipe_images():
    """Diagnostic: show which recipes have/lack image data in the DB.
    Requires admin bearer token."""
    err = require_admin()
    if err:
        return err

    return jsonify(_collect_image_audit_summary()), 200


@generation_api_bp.route("/admin/migrate-images", methods=["POST"])
def migrate_image_urls():
    """
    Migration endpoint: moves recipe images from base64-in-DB to GCS.
    Requires Authorization: Bearer <ADMIN_API_TOKEN>.
    """
    auth_error = require_admin()
    if auth_error:
        return auth_error

    return _run_image_url_migration()


@generation_api_bp.route("/recipes/missing-images", methods=["GET"])
def list_recipes_missing_images():
    """
    List recipes that have no valid AI image (no GCS or base64 image).
    Used by frontend to offer image regeneration.
    """
    missing = _find_recipes_missing_images(
        _current_user_id(),
        _current_guest_session_id(),
    )
    return jsonify({"recipes": missing, "count": len(missing)}), 200


__all__ = [
    "DEFAULT_MODEL",
    "GCS_BUCKET_NAME",
    "TTL_IMAGE",
    "_collect_image_audit_summary",
    "_current_guest_session_id",
    "_current_user_id",
    "_find_recipes_missing_images",
    "_image_cache_version",
    "_image_mimetype",
    "_load_stored_image_bytes",
    "_run_image_url_migration",
    "_serve_derived_image",
    "_serve_image_variant",
    "_serve_pin_image",
    "_traced_cache_get",
    "audit_recipe_images",
    "db_recipe_repository",
    "gcs_service",
    "generate_image_for_recipe",
    "generate_recipe_json",
    "generation_api_bp",
    "get_or_create_session_id",
    "get_recipe_status",
    "invalidate_recipe",
    "list_recipes_missing_images",
    "make_pin_variant",
    "make_webp_variant",
    "migrate_image_urls",
    "parse_pin_flag",
    "parse_variant_width",
    "queue_recipe_generation",
    "queue_recipe_image",
    "recipe_image_key",
    "recipe_image_pin_key",
    "recipe_image_variant_key",
    "require_admin",
    "safe_get",
    "safe_set",
    "serve_recipe_image",
    "validate_generation_input",
]
