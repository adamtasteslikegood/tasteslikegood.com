"""Detect recipes with no image bytes and enqueue regeneration (KAN-141).

Runs as the scheduled Cloud Run Job ``flask-backend-image-repair``: scans for
recipes whose data blob has neither ``ai_image_data`` (base64) nor
``ai_image_gcs`` (bucket URI) — the same "imageless" semantics as the
/api/admin/image-audit endpoint — and enqueues each through the exact
Pub/Sub path the SPA's regenerate button uses. The flask-backend worker does
the actual Imagen generation; this script only detects and enqueues.

Priority order (most-visible pages repaired first): canonical recipes, then
published recipes, then everything else oldest-first. Each run enqueues at
most ``IMAGE_REPAIR_LIMIT`` recipes (default 10) so a large backlog drains
over successive runs instead of burning Imagen quota in one burst.

Usage:
    python scripts/repair_missing_images.py [--dry-run]

Exit codes: 0 = success (including "nothing to repair"), 1 = fatal error.
Skipped rows (queue contention, publish failure) are logged and do not fail
the run — the next scheduled run retries them.
"""

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("image-repair")

DEFAULT_LIMIT = 10


def _is_imageless(data: dict) -> bool:
    """True when the blob holds no actual image bytes/URI.

    ``ai_image_url`` alone does not count: rows exist whose URL points at an
    object that was never written (blank hero/OG images on the public page).
    """
    return not data.get("ai_image_data") and not data.get("ai_image_gcs")


def select_candidates(limit: int):
    """Imageless, generation-idle recipes, most publicly visible first."""
    from models import Recipe

    rows = (
        Recipe.query.filter(Recipe.status == "ready")
        .order_by(
            Recipe.is_canonical.desc(),
            Recipe.is_public.desc(),
            Recipe.created_at.asc(),
        )
        .all()
    )
    return [r for r in rows if _is_imageless(r.data or {})][:limit]


def enqueue(recipe) -> bool:
    """Queue one recipe through the same path as POST /api/generate_image."""
    from repositories import db_recipe_repository
    from services.pubsub_service import publish_message

    queued = db_recipe_repository.queue_image_generation(
        recipe.id,
        str(uuid.uuid4()),
        False,
        recipe.user_id,
        recipe.guest_session_id,
    )
    if queued is None:
        logger.warning("skip %s: not queueable (state changed under us)", recipe.id)
        return False
    if not queued.should_publish:
        logger.info("skip %s: request already pending", recipe.id)
        return False

    try:
        publish_message(
            "image-generation",
            {
                "recipe_id": recipe.id,
                "user_id": recipe.user_id,
                "guest_session_id": recipe.guest_session_id,
                "force_regenerate": queued.force_regenerate,
                "image_request_id": queued.request_id,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 - one bad publish must not kill the run
        logger.error("publish failed for %s: %s", recipe.id, exc)
        db_recipe_repository.release_image_generation_queue(
            recipe.id, queued.request_id, recipe.user_id, recipe.guest_session_id
        )
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report candidates without enqueueing"
    )
    args = parser.parse_args()

    limit = int(os.environ.get("IMAGE_REPAIR_LIMIT", DEFAULT_LIMIT))

    from app import create_app

    app = create_app()
    with app.app_context():
        candidates = select_candidates(limit)
        if not candidates:
            logger.info("No imageless recipes found — nothing to repair.")
            return 0

        logger.info("%d imageless recipe(s), limit %d this run:", len(candidates), limit)
        for r in candidates:
            logger.info(
                "  %s %r (canonical=%s public=%s slug=%s)",
                r.id,
                r.name,
                r.is_canonical,
                r.is_public,
                r.slug,
            )

        if args.dry_run:
            logger.info("Dry run — nothing enqueued.")
            return 0

        enqueued = sum(1 for r in candidates if enqueue(r))
        logger.info("Enqueued %d/%d image regeneration(s).", enqueued, len(candidates))
        return 0


if __name__ == "__main__":
    sys.exit(main())
