"""
Async Pub/Sub recipe/image generation queueing, image byte loading, derived
variant serving, and admin image audit/migration orchestration (KAN-462).

Extracted from ``blueprints.generation_api_bp`` to keep route handlers thin
while preserving backwards-compatible symbols for existing callers and tests.
"""

import base64
import binascii
from collections.abc import Callable
import hashlib
import logging
import sys
from typing import Any, TypeVar, cast
import uuid

from ddtrace import tracer
from flask import Response, jsonify, session
from flask.typing import ResponseReturnValue

from config import DEFAULT_MODEL, GCS_BUCKET_NAME
from repositories import db_recipe_repository
from services import gcs_service
from services.image_variants import make_pin_variant, make_webp_variant
from utils.cache_utils import (
    TTL_IMAGE,
    invalidate_recipe,
    recipe_image_pin_key,
    recipe_image_variant_key,
    safe_get,
    safe_set,
)
from utils.log_sanitizer import sanitize_log_value
from utils.session_utils import get_or_create_session_id

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _bp_attr(name: str, default: _T) -> _T:
    """Resolve ``name`` from ``blueprints.generation_api_bp`` so test monkeypatches apply."""
    bp_mod = sys.modules.get("blueprints.generation_api_bp")
    return cast(_T, getattr(bp_mod, name, default)) if bp_mod is not None else default


def _current_user_id():
    return session.get("user_id")


def _current_guest_session_id():
    session_fn = _bp_attr("get_or_create_session_id", get_or_create_session_id)
    return session_fn()


def queue_recipe_generation(
    prompt: str,
    selected_model: str,
    user_id: int | None,
    guest_session_id: str | None,
    with_image: bool = True,
) -> ResponseReturnValue:
    """Save a pending recipe for this owner and queue its text generation.

    Shared by the web app's route and the agent tools (KAN-401). The
    caller validates the prompt and decides who the owner is. The worker
    queues a photo after the text unless ``with_image`` is False.
    """
    # The Pub/Sub worker rebuilds the full schema prompt from the raw prompt
    # (worker_api_bp), so only the raw prompt is published here.
    repo = _bp_attr("db_recipe_repository", db_recipe_repository)
    invalidate_fn = _bp_attr("invalidate_recipe", invalidate_recipe)

    recipe_id = str(uuid.uuid4())
    pending_data = {"id": recipe_id, "name": "Generating...", "user_id": user_id}

    db_recipe = repo.create_recipe(
        pending_data,
        user_id,
        guest_session_id,
        status="generating",
    )

    if not db_recipe:
        return jsonify({"error": "Failed to save recipe to database"}), 500

    # KAN-151: this is the second path that adds a row to an owner's scope.
    # Since KAN-381 the placeholder is not counted or listed until the worker
    # sets it "ready" (and invalidates again), so this clears nothing that is
    # stale today; it stays so the owner's keys never depend on that filter.
    invalidate_fn(user_id, guest_session_id, recipe_id)

    from services.pubsub_service import publish_message

    message_data = {
        "recipe_id": recipe_id,
        "prompt": prompt,
        "model": selected_model,
        "user_id": user_id,
        "guest_session_id": guest_session_id,
        "with_image": with_image,
    }

    try:
        publish_message("recipe-generation", message_data)
        logger.info(
            "Queued recipe generation (id=%s)",
            sanitize_log_value(recipe_id),
        )
        return jsonify({"recipe_id": recipe_id, "status": "generating"}), 202
    except Exception as e:
        logger.error(
            "Failed to publish recipe generation: %s",
            sanitize_log_value(e),
        )
        repo.update_recipe_status(recipe_id, "error", user_id, guest_session_id)
        # Terminal state: no worker will run, so nothing downstream clears this.
        invalidate_fn(user_id, guest_session_id, recipe_id)
        return jsonify({"error": "Failed to queue generation"}), 500


def queue_recipe_image(
    recipe_id: str,
    force_regenerate: bool,
    user_id: int | None,
    guest_session_id: str | None,
) -> ResponseReturnValue:
    """Queue an AI image for a recipe this owner holds.

    Shared by the web app's route and the agent tools (KAN-401).
    """
    repo = _bp_attr("db_recipe_repository", db_recipe_repository)
    invalidate_fn = _bp_attr("invalidate_recipe", invalidate_recipe)

    db_recipe = repo.get_recipe_by_id(recipe_id, user_id, guest_session_id)
    if not db_recipe:
        return jsonify({"error": "Recipe not found"}), 404

    recipe_data = db_recipe.data or {}

    # Check if image already exists (verify actual image data, not just URL)
    has_real_image = bool(recipe_data.get("ai_image_data") or recipe_data.get("ai_image_gcs"))
    if not force_regenerate and has_real_image and recipe_data.get("ai_image_url"):
        return jsonify({"image_url": recipe_data["ai_image_url"]}), 200

    queued = repo.queue_image_generation(
        recipe_id,
        str(uuid.uuid4()),
        force_regenerate,
        user_id,
        guest_session_id,
    )
    if queued is None:
        db_recipe = repo.get_recipe_by_id(
            recipe_id,
            user_id,
            guest_session_id,
        )
        if db_recipe is None:
            return jsonify({"error": "Recipe not found"}), 404
        if db_recipe.status != "generating_image":
            return jsonify({"error": "Recipe is not ready for image generation"}), 409
        return jsonify({"status": "generating_image"}), 202
    # status flipped to "generating_image" and ai_metadata.image_request was
    # rewritten; both ride on to_dict(), so the cached recipe payload is stale.
    invalidate_fn(user_id, guest_session_id, recipe_id)

    if not queued.should_publish:
        return jsonify({"status": "generating_image"}), 202

    from services.pubsub_service import publish_message

    message_data = {
        "recipe_id": recipe_id,
        "user_id": user_id,
        "guest_session_id": guest_session_id,
        "force_regenerate": queued.force_regenerate,
        "image_request_id": queued.request_id,
    }

    try:
        publish_message("image-generation", message_data)
        logger.info(
            "Queued image generation for recipe (id=%s)",
            sanitize_log_value(recipe_id),
        )
        return jsonify({"status": "generating_image"}), 202
    except Exception as e:
        logger.error(
            "Failed to queue image generation for recipe %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        if not repo.release_image_generation_queue(
            recipe_id,
            queued.request_id,
            user_id,
            guest_session_id,
        ):
            logger.info(
                "Image queue state changed for recipe %s after publish returned an error",
                sanitize_log_value(recipe_id),
            )
        # The release reverted status/ai_metadata again, and no worker will run
        # to clear the key later.
        invalidate_fn(user_id, guest_session_id, recipe_id)
        return jsonify({"error": "Failed to queue image generation"}), 500


def _image_mimetype(image_bytes: bytes) -> str:
    """Sniff the real image type from the bytes' magic numbers.

    Imagen has produced JPEG bytes for recipes while this endpoint hardcoded
    ``image/png`` — a mismatch that breaks strict crawlers/CDNs and the
    audit's content-type check (cookbook #3164). Defaults to PNG for
    unrecognized payloads (the endpoint's historical behavior).
    """
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "image/png"


def _load_stored_image_bytes(recipe, *, allow_legacy_fallback: bool = True) -> bytes | None:
    """The recipe's image bytes from storage.

    GCS is authoritative when configured. Legacy base64 fallback remains for the
    original endpoint, but callers that bind cache identity to the GCS URI can
    disable it so different bytes are never stored under that identity.
    """
    recipe_data = recipe.data or {}
    image_bytes = None
    stored_uri = recipe_data.get("ai_image_gcs")
    if not isinstance(stored_uri, str):
        stored_uri = ""

    bucket_name = _bp_attr("GCS_BUCKET_NAME", GCS_BUCKET_NAME)
    gcs_mod = _bp_attr("gcs_service", gcs_service)
    if bucket_name and stored_uri:
        # download_image traces itself (image.gcs_fetch, KAN-268).
        image_bytes = gcs_mod.download_image(
            bucket_name,
            recipe.id,
            stored_uri,
        )

    if image_bytes is None and allow_legacy_fallback:
        image_b64 = recipe_data.get("ai_image_data")
        # Writable through the generic recipe PUT, so it can be any JSON value:
        # a non-string or malformed payload is "no image" (404), not a 500.
        if isinstance(image_b64, str) and image_b64:
            with tracer.trace("image.legacy_decode"):
                try:
                    image_bytes = base64.b64decode(image_b64) or None
                except (binascii.Error, ValueError):
                    image_bytes = None

    return image_bytes


def _traced_cache_get(key: str) -> bytes | None:
    """Read image bytes from Valkey and record a consistent cache-hit tag."""
    safe_get_fn = _bp_attr("safe_get", safe_get)
    with tracer.trace("image.cache_read") as span:
        cached = safe_get_fn(key)
        span.set_tag("image.cache_hit", str(cached is not None).lower())
    return cached  # type: ignore[no-any-return]


def _image_cache_version(recipe) -> str:
    """Cache identity of every byte source the image endpoint can serve.

    The shared source identity includes both the preferred GCS URI and the
    legacy fallback payload when both exist. Hash the raw identity to retain
    the established 16-character internal cache-key strength.
    """
    from blueprints.public_bp import _image_version_source

    source = _image_version_source(recipe) or ""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _serve_derived_image(
    recipe,
    key: str,
    build: Callable[[bytes], bytes | None],
    mimetype: str,
    http_cache_control: str,
) -> ResponseReturnValue | None:
    """A derived image (sized WebP, pin JPEG) of the recipe's photo, or ``None``.

    ``key`` must be keyed on the image's version (``_image_cache_version``), and
    the bytes are built from storage, never from the full-size Valkey entry, so
    a derived image only ever reflects the bytes its version names.

    A configured GCS URI is authoritative for derived images. If its read
    fails, do not fall back to legacy base64 under a cache key derived from the
    GCS identity; return ``None`` and let the bounded-cache original path decide
    whether legacy fallback is appropriate.
    """
    cache_get_fn = _bp_attr("_traced_cache_get", _traced_cache_get)
    load_bytes_fn = _bp_attr("_load_stored_image_bytes", _load_stored_image_bytes)
    safe_set_fn = _bp_attr("safe_set", safe_set)
    ttl_image = _bp_attr("TTL_IMAGE", TTL_IMAGE)

    stored_uri = (recipe.data or {}).get("ai_image_gcs")
    if not isinstance(stored_uri, str):
        stored_uri = ""

    derived = cache_get_fn(key)
    if derived is None:
        source = load_bytes_fn(recipe, allow_legacy_fallback=not bool(stored_uri))
        if source is None:
            return None
        # Decode + resize + re-encode (PIL): CPU work, previously unspanned.
        with tracer.trace("image.variant_build") as span:
            derived = build(source)
            span.set_metric("image.source_bytes", len(source))
            span.set_metric("image.output_bytes", len(derived or b""))
            span.set_tag("image.variant_success", str(derived is not None).lower())
        if derived is None:
            return None
        with tracer.trace("image.cache_write") as span:
            span.set_metric("image.bytes", len(derived))
            safe_set_fn(key, derived, timeout=ttl_image)

    return Response(
        derived,
        mimetype=mimetype,
        headers={"Cache-Control": http_cache_control},
    )


def _serve_image_variant(recipe, width: int, http_cache_control: str) -> ResponseReturnValue | None:
    """A WebP of the recipe's image at ``width`` px (KAN-271), or ``None`` to fall back."""
    webp_fn = _bp_attr("make_webp_variant", make_webp_variant)
    serve_derived_fn = _bp_attr("_serve_derived_image", _serve_derived_image)
    variant_key_fn = _bp_attr("recipe_image_variant_key", recipe_image_variant_key)
    version_fn = _bp_attr("_image_cache_version", _image_cache_version)

    def build_webp_variant(source: bytes) -> bytes | None:
        return webp_fn(source, width)

    return serve_derived_fn(
        recipe,
        variant_key_fn(recipe.id, width, version_fn(recipe)),
        build_webp_variant,
        "image/webp",
        http_cache_control,
    )


def _serve_pin_image(recipe, http_cache_control: str) -> ResponseReturnValue | None:
    """The 2:3 Pinterest pin JPEG of the recipe's image (KAN-284), or ``None``."""
    serve_derived_fn = _bp_attr("_serve_derived_image", _serve_derived_image)
    pin_key_fn = _bp_attr("recipe_image_pin_key", recipe_image_pin_key)
    version_fn = _bp_attr("_image_cache_version", _image_cache_version)
    pin_variant_fn = _bp_attr("make_pin_variant", make_pin_variant)

    return serve_derived_fn(
        recipe,
        pin_key_fn(recipe.id, version_fn(recipe)),
        pin_variant_fn,
        "image/jpeg",
        http_cache_control,
    )


def _collect_image_audit_summary() -> dict[str, Any]:
    """Summarize which recipes in the database have or lack stored image data."""
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
    return {
        "total": len(results),
        "with_image_data": len(have_image),
        "without_image_data": len(missing),
        "missing": missing,
    }


def _run_image_url_migration() -> ResponseReturnValue:
    """Migrate base64 recipe images in the database to GCS in batches."""
    from extensions import db
    from models import Recipe

    bucket_name = _bp_attr("GCS_BUCKET_NAME", GCS_BUCKET_NAME)
    if not bucket_name:
        return jsonify({"error": "GCS_BUCKET_NAME not configured"}), 500

    from services.gcs_service import upload_image

    batch_size = 100
    migrated = []
    errors = []
    skipped_data_urls = 0

    try:
        offset = 0
        while True:
            recipes = Recipe.query.limit(batch_size).offset(offset).all()
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
                        if image_b64.startswith("data:image/"):
                            parts = image_b64.split(",", 1)
                            image_b64 = parts[1] if len(parts) == 2 else image_b64

                        image_bytes = base64.b64decode(image_b64)
                        gcs_uri = upload_image(bucket_name, recipe.id, image_bytes)
                        if gcs_uri:
                            data["ai_image_gcs"] = gcs_uri
                            data["ai_image_url"] = api_url
                            del data["ai_image_data"]
                            changed = True
                        else:
                            errors.append({"id": recipe.id, "error": "GCS upload failed"})
                    except Exception as e:
                        logger.error(
                            "Image migration failed for recipe %s: %s",
                            sanitize_log_value(recipe.id),
                            sanitize_log_value(e),
                        )
                        errors.append({"id": recipe.id, "error": "Image upload failed"})

                # KAN-329: a data: URL in ai_image_url is client-written and is
                # never promoted into GCS as the recipe's image. It is left as
                # is; only the image worker may populate media on a public row.
                elif url and url.startswith("data:"):
                    skipped_data_urls += 1

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
            offset += batch_size

        logger.info(
            "Image migration complete: %d migrated, %d data URLs skipped, %d errors",
            len(migrated),
            skipped_data_urls,
            len(errors),
        )
        return (
            jsonify(
                {
                    "migrated": len(migrated),
                    "skipped_data_urls": skipped_data_urls,
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


def _find_recipes_missing_images(
    user_id: int | None, guest_session_id: str | None
) -> list[dict[str, Any]]:
    """Return owned recipes that have neither GCS nor base64 image data."""
    from models import Recipe

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
    return missing


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
    "queue_recipe_generation",
    "queue_recipe_image",
]
