"""
Cache utilities for user-scoped Valkey caching.

Provides helper functions for building per-user cache keys and
invalidating related caches on mutation operations.
All cache operations are fault-tolerant — failures log warnings
and fall through to the database, never crash the request.
"""

import json
import logging

from extensions import cache
from utils.log_sanitizer import sanitize_log_value

logger = logging.getLogger(__name__)

# TTLs in seconds
TTL_SHORT = 300  # 5 minutes — recipe stats, collections list
TTL_MEDIUM = 600  # 10 minutes — individual recipes, collections
TTL_IMAGE = 86400  # 24 hours — recipe images (rarely change)

# Ceiling for cached JSON response payloads. Recipe rows written before the
# GCS migration can still carry a base64 image under data["ai_image_data"]
# (generation_api_bp still reads that fallback), so an unguarded set would put
# multi-MB blobs in Valkey once per owner. Oversized payloads are simply not
# cached — a miss returns exactly what a hit would have, so skipping is
# always safe. Image bytes are cached through safe_set without this guard,
# since being large is the whole point there.
MAX_JSON_CACHE_BYTES = 256 * 1024


# ── Safe cache operations (never raise) ───────────────────────────────────────


def safe_get(key):
    """Get from cache. Returns None on any failure."""
    try:
        return cache.get(key)
    except Exception as e:
        logger.warning(
            "Cache GET failed for %s: %s",
            sanitize_log_value(key),
            sanitize_log_value(e),
        )
        return None


def safe_set(key, value, timeout=None, max_bytes=None):
    """Set in cache. Silently ignores failures.

    When ``max_bytes`` is given, a payload whose JSON encoding exceeds it is
    skipped rather than stored. Skipping is not a correctness concern: the
    handler returns the freshly built response either way.
    """
    try:
        if max_bytes is not None:
            try:
                size = len(json.dumps(value, default=str).encode("utf-8"))
            except (TypeError, ValueError):
                # Not JSON-encodable — do not guess at its size, just skip.
                return
            if size > max_bytes:
                logger.info(
                    "Cache SET skipped for %s: payload %d bytes exceeds %d",
                    sanitize_log_value(key),
                    size,
                    max_bytes,
                )
                return
        cache.set(key, value, timeout=timeout)
    except Exception as e:
        logger.warning(
            "Cache SET failed for %s: %s",
            sanitize_log_value(key),
            sanitize_log_value(e),
        )


# ── Key builders ──────────────────────────────────────────────────────────────


def _owner_prefix(user_id, guest_session_id):
    """Build a cache key prefix scoped to the current user or guest."""
    if user_id is not None:
        return f"u:{user_id}"
    return f"g:{guest_session_id}"


def recipe_key(user_id, guest_session_id, recipe_id):
    return f"vgc:{_owner_prefix(user_id, guest_session_id)}:r:{recipe_id}"


def recipe_stats_key(user_id, guest_session_id):
    return f"vgc:{_owner_prefix(user_id, guest_session_id)}:rstats"


def collections_list_key(user_id, guest_session_id):
    return f"vgc:{_owner_prefix(user_id, guest_session_id)}:colls"


def collection_key(user_id, guest_session_id, collection_id):
    return f"vgc:{_owner_prefix(user_id, guest_session_id)}:c:{collection_id}"


def recipe_image_key(recipe_id):
    """Image cache is NOT user-scoped — same image for everyone."""
    return f"vgc:img:{recipe_id}"


# ── Invalidation helpers ──────────────────────────────────────────────────────


def invalidate_recipe(user_id, guest_session_id, recipe_id):
    """Invalidate caches after a recipe is created, updated, or deleted."""
    keys = [
        recipe_key(user_id, guest_session_id, recipe_id),
        recipe_stats_key(user_id, guest_session_id),
    ]
    _delete_keys(keys)


def invalidate_recipe_image(recipe_id):
    """Invalidate cached image after regeneration."""
    _delete_keys([recipe_image_key(recipe_id)])


def invalidate_collection(user_id, guest_session_id, collection_id=None):
    """Invalidate collection caches after mutation."""
    keys = [collections_list_key(user_id, guest_session_id)]
    if collection_id:
        keys.append(collection_key(user_id, guest_session_id, collection_id))
    _delete_keys(keys)


def invalidate_identity(user_id, guest_session_id, recipe_ids=(), collection_ids=()):
    """Invalidate every cache entry owned by one identity.

    Used by the guest-to-account merge, which reassigns recipe and cookbook
    rows between two owner scopes at once. Both the source (guest) and target
    (user) scopes must be cleared: the target gains rows it has already cached
    a stats/list answer for, and the source keys would otherwise survive until
    TTL against a session that no longer owns anything.

    Key builders are per-owner, and Valkey key scanning is deliberately avoided
    here (KEYS/SCAN across a shared instance is a foot-gun), so callers pass
    the specific ids they touched.
    """
    keys = [
        recipe_stats_key(user_id, guest_session_id),
        collections_list_key(user_id, guest_session_id),
    ]
    keys.extend(recipe_key(user_id, guest_session_id, rid) for rid in recipe_ids)
    keys.extend(collection_key(user_id, guest_session_id, cid) for cid in collection_ids)
    _delete_keys(keys)


def _delete_keys(keys):
    """Delete cache keys, logging failures without raising."""
    for key in keys:
        try:
            cache.delete(key)
        except Exception as e:
            logger.warning(
                "Cache delete failed for %s: %s",
                sanitize_log_value(key),
                sanitize_log_value(e),
            )
