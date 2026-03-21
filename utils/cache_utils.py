"""
Cache utilities for user-scoped Valkey caching.

Provides helper functions for building per-user cache keys and
invalidating related caches on mutation operations.
All cache operations are fault-tolerant — failures log warnings
and fall through to the database, never crash the request.
"""

import logging

from extensions import cache

logger = logging.getLogger(__name__)

# TTLs in seconds
TTL_SHORT = 300       # 5 minutes — recipe stats, collections list
TTL_MEDIUM = 600      # 10 minutes — individual recipes, collections
TTL_IMAGE = 86400     # 24 hours — recipe images (rarely change)


# ── Safe cache operations (never raise) ───────────────────────────────────────


def safe_get(key):
    """Get from cache. Returns None on any failure."""
    try:
        return cache.get(key)
    except Exception as e:
        logger.warning(f"Cache GET failed for {key}: {e}")
        return None


def safe_set(key, value, timeout=None):
    """Set in cache. Silently ignores failures."""
    try:
        cache.set(key, value, timeout=timeout)
    except Exception as e:
        logger.warning(f"Cache SET failed for {key}: {e}")


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


def _delete_keys(keys):
    """Delete cache keys, logging failures without raising."""
    for key in keys:
        try:
            cache.delete(key)
        except Exception as e:
            logger.warning(f"Cache delete failed for {key}: {e}")
