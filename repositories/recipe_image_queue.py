"""
Image-generation queue, inherited stock-image stripping, and file-to-DB
migration helpers for the database-backed recipe repository (KAN-460).

Extracted from ``repositories.db_recipe_repository`` so image request
queueing/release and legacy file migration live in a cohesive module while
remaining re-exported from ``repositories.db_recipe_repository``.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, cast

from extensions import db
from models import Recipe
from repositories.recipe_worker_ops import _apply_recipe_scope
from utils.log_sanitizer import sanitize_log_value

logger = logging.getLogger("repositories.db_recipe_repository")


@dataclass(frozen=True)
class ImageGenerationQueue:
    request_id: str
    force_regenerate: bool
    should_publish: bool


def _strip_inherited_stock_image(data: Dict[str, Any]) -> None:
    """Drop ``stock_image_url`` from a saved copy's blob, in place.

    KAN-215.  A saved copy must not persist a stock image pointer that came
    from its source.  Once persisted it is indistinguishable from an image the
    copy genuinely owns, so ``public_bp._recipe_image_url`` returns it at the
    own-image check and never resolves the source — pinning the copy to the
    stock image permanently, including after the source gains an AI image (AI
    generation preserves ``stock_image_url``, so a source carrying both is a
    normal state).

    Enforced at the **server boundary**, on every write path, because the
    inherited URL does not only arrive from a server-side copy: the SPA posts
    the recipe data it is displaying, which for a save *is* the source's blob,
    stock URL included.  Stripping it on create alone would leave any later
    PUT/POST that echoes the payload free to re-persist it — the same
    regression, one request later.  Same idiom as ``is_canonical``:
    a field the API is not allowed to write is pinned by the server, not
    trusted from the payload.

    The copy still renders the source's image; it is resolved live at render
    time instead of being frozen at save time.
    """
    data.pop("stock_image_url", None)


def queue_image_generation(
    recipe_id: str,
    request_id: str,
    force_regenerate: bool,
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
) -> Optional[ImageGenerationQueue]:
    """Persist or recover one owner-scoped image request before publication."""
    try:
        for _ in range(3):
            recipe = (
                _apply_recipe_scope(
                    Recipe.query.populate_existing().filter(Recipe.id == recipe_id),
                    user_id,
                    guest_session_id,
                )
                .filter(Recipe.status.in_(("ready", "generating_image")))
                .first()
            )
            if recipe is None:
                return None

            recipe_data = dict(recipe.data or {})
            raw_metadata = recipe_data.get("ai_metadata")
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            existing_request = metadata.get("image_request")
            if not isinstance(existing_request, dict):
                existing_request = {}

            existing_request_id = existing_request.get("id")
            existing_pending = (
                isinstance(existing_request_id, str) and existing_request.get("status") == "pending"
            )
            existing_force = existing_request.get("force_regenerate") is True
            reusable_request = existing_pending and (not force_regenerate or existing_force)
            queued_request_id = cast(str, existing_request_id) if reusable_request else request_id
            queued_force = existing_force if reusable_request else force_regenerate

            if recipe.status == "generating_image" and recipe.worker_claim_token is not None:
                return ImageGenerationQueue(
                    request_id=queued_request_id,
                    force_regenerate=queued_force,
                    should_publish=False,
                )

            enqueue = metadata.get("image_enqueue")
            if (
                recipe.status == "generating_image"
                and not reusable_request
                and not force_regenerate
                and isinstance(enqueue, dict)
                and enqueue.get("status") == "pending"
            ):
                return ImageGenerationQueue(
                    request_id=request_id,
                    force_regenerate=force_regenerate,
                    should_publish=False,
                )

            observed_updated_at = recipe.updated_at
            metadata["image_request"] = {
                "id": queued_request_id,
                "status": "pending",
                "force_regenerate": queued_force,
                "timestamp": datetime.now().isoformat(),
            }
            recipe_data["ai_metadata"] = metadata
            query = _apply_recipe_scope(
                Recipe.query.filter(
                    Recipe.id == recipe_id,
                    Recipe.status == recipe.status,
                    Recipe.worker_claim_token.is_(None),
                    Recipe.updated_at == observed_updated_at,
                ),
                user_id,
                guest_session_id,
            )
            updated = cast(
                int,
                query.update(
                    {
                        "data": recipe_data,
                        "status": "generating_image",
                        "worker_claim_token": None,
                        "updated_at": datetime.utcnow(),
                    },
                    synchronize_session=False,
                ),
            )
            db.session.commit()
            if updated == 1:
                return ImageGenerationQueue(
                    request_id=queued_request_id,
                    force_regenerate=queued_force,
                    should_publish=True,
                )
            db.session.expire_all()
        return None
    except Exception as e:
        logger.error(
            "Error queueing image generation for recipe %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        db.session.rollback()
        return None


def release_image_generation_queue(
    recipe_id: str,
    request_id: str,
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
) -> bool:
    """Return an unpublished request to ready while retaining its retry identity."""
    try:
        recipe = (
            _apply_recipe_scope(
                Recipe.query.populate_existing().filter(
                    Recipe.id == recipe_id,
                    Recipe.status == "generating_image",
                    Recipe.worker_claim_token.is_(None),
                ),
                user_id,
                guest_session_id,
            )
            .with_for_update()
            .first()
        )
        if recipe is None:
            return False
        metadata = (recipe.data or {}).get("ai_metadata")
        image_request = metadata.get("image_request") if isinstance(metadata, dict) else None
        if not isinstance(image_request, dict) or image_request.get("id") != request_id:
            return False

        recipe.status = "ready"
        recipe.updated_at = datetime.utcnow()
        db.session.commit()
        return True
    except Exception as e:
        logger.error(
            "Error releasing image generation queue for recipe %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        db.session.rollback()
        return False


def migrate_file_to_db(
    filename: str, recipe_data: Dict[str, Any], user_id: Optional[int] = None
) -> Optional[Recipe]:
    """
    Migrate a file-based recipe to the database.

    Uses the filename (without .json) as the recipe ID to maintain consistency.

    Args:
        filename: Original filename (e.g., "recipe-uuid.json")
        recipe_data: Recipe JSON data
        user_id: Optional user ID to assign ownership

    Returns:
        Created Recipe object, or None if creation failed
    """
    try:
        # Extract UUID from filename
        recipe_id = filename.replace(".json", "")
        recipe_name = recipe_data.get("name", "Unnamed Recipe")

        # Check if already exists
        existing = Recipe.query.filter_by(id=recipe_id).first()  # type: ignore[no-any-return]
        if existing:
            logger.warning(
                "Recipe %s already exists in database, skipping",
                sanitize_log_value(recipe_id),
            )
            return existing  # type: ignore[no-any-return]

        # KAN-329: a file is client content; it never arrives public.
        # File migrations are client-content imports. Do not preserve a
        # forged worker-only provenance label inside the JSON blob.
        migrated_data = {key: value for key, value in recipe_data.items() if key != "origin"}
        recipe = Recipe(
            id=recipe_id,
            user_id=user_id,
            name=recipe_name,
            slug=recipe_data.get("slug"),
            is_public=False,
            data={**migrated_data, "is_public": False},
        )

        db.session.add(recipe)
        db.session.commit()

        logger.info(
            "Migrated recipe %s from file %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(filename),
        )
        return recipe

    except Exception as e:
        logger.error(
            "Error migrating recipe %s: %s",
            sanitize_log_value(filename),
            sanitize_log_value(e),
        )
        db.session.rollback()
        return None


class RecipeImageQueueMixin:
    """Mixin exposing image generation queue and file migration methods."""

    strip_inherited_stock_image = staticmethod(_strip_inherited_stock_image)
    queue_image_generation = staticmethod(queue_image_generation)
    release_image_generation_queue = staticmethod(release_image_generation_queue)
    migrate_file_to_db = staticmethod(migrate_file_to_db)


__all__ = [
    "ImageGenerationQueue",
    "RecipeImageQueueMixin",
    "_strip_inherited_stock_image",
    "queue_image_generation",
    "release_image_generation_queue",
    "migrate_file_to_db",
]
