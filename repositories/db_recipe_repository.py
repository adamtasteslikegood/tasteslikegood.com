"""
Database-backed recipe repository for Phase 3.

Handles recipe CRUD operations with SQLAlchemy ORM:
- Create, read, update, delete recipes
- User-scoped queries (recipes belong to users)
- Support for anonymous recipes (user_id = NULL)
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, cast

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Recipe, User  # noqa: F401
from utils.log_sanitizer import sanitize_log_value
from utils.slug_utils import normalize_slug

logger = logging.getLogger(__name__)

# Matches models/recipe.py: slug = db.Column(db.String(255), ...)
_SLUG_MAX_LENGTH = 255
# Commit retries when a concurrent publication wins the slug race.
_SLUG_COMMIT_RETRIES = 3
_WORKER_METADATA_KEYS = ("image_enqueue", "image_request")
_ACTIVE_RECIPE_STATUSES = frozenset({"generating", "processing", "generating_image"})

# Also returned verbatim by the API routes (a fixed string, so no exception
# internals can leak into responses — CodeQL py/stack-trace-exposure).
PUBLIC_SLUG_REQUIRED_ERROR = (
    "A public recipe needs a slug, and neither the provided slug nor "
    "the recipe name yields a usable one."
)


class RecipeSlugError(ValueError):
    """A recipe cannot be published without a usable /r/<slug> address."""


@dataclass(frozen=True)
class WorkerRecipeUpdate:
    user_id: Optional[int]
    guest_session_id: Optional[str]


@dataclass(frozen=True)
class ImageGenerationQueue:
    request_id: str
    force_regenerate: bool
    should_publish: bool


def _slugify(text: str) -> str:
    """Normalize text to a route-safe slug.

    Same normalization as scripts/backfill_slugs.py:generate_slug, except
    that this returns "" for unusable input (the publish gate rejects it
    with RecipeSlugError) where the backfill falls back to "recipe".
    """
    return normalize_slug(text)


def _preserve_worker_metadata(current_data: Dict[str, Any], merged: Dict[str, Any]) -> None:
    current_metadata = current_data.get("ai_metadata")
    if not isinstance(current_metadata, dict):
        return
    incoming_metadata = merged.get("ai_metadata")
    preserved_metadata = dict(incoming_metadata) if isinstance(incoming_metadata, dict) else {}
    for key in _WORKER_METADATA_KEYS:
        if key in current_metadata:
            preserved_metadata[key] = current_metadata[key]
    if preserved_metadata:
        merged["ai_metadata"] = preserved_metadata


def _resolve_public_slug(
    recipe_data: Dict[str, Any],
    recipe_id: str,
    current_slug: Optional[str] = None,
    skip: frozenset = frozenset(),
) -> str:
    """Pick a non-empty, route-safe, unique slug for a recipe being published.

    Public rows with slug=NULL would appear in /browse (the sitemap already
    filters them out) yet no /r/<slug> URL could resolve them, so
    publication requires a usable slug:
    the payload's slug if salvageable, else the row's existing slug, else one
    derived from the name. Uniqueness collisions get a numeric suffix; the DB
    unique constraint on Recipe.slug remains the final arbiter under races.

    Every result fits the 255-char slug column: the base is truncated and
    each collision suffix reserves its own room. ``skip`` holds slugs that
    already lost a commit race and must not be offered again.

    Slugs occupying the base are fetched in one LIKE query and the suffix is
    chosen in Python, so publishing the nth "Chili" costs one round trip, not
    n. The prefix over-matches (chili% also hits chili-con-carne); harmless,
    since candidates are tested by exact membership.
    """
    provided_slug = recipe_data.get("slug")
    if current_slug is not None and (provided_slug is None or str(provided_slug) == current_slug):
        return current_slug

    for source in (recipe_data.get("slug"), current_slug, recipe_data.get("name")):
        candidate = _slugify(str(source)) if source else ""
        if candidate:
            break
    else:
        raise RecipeSlugError(PUBLIC_SLUG_REQUIRED_ERROR)

    base = candidate[:_SLUG_MAX_LENGTH].rstrip("-")
    # Short enough that every truncated-for-suffix variant still matches.
    # _slugify output has no LIKE metacharacters (%, _).
    prefix = base[: _SLUG_MAX_LENGTH - 12].rstrip("-")
    occupied = set(skip) | {
        slug
        for (slug,) in db.session.query(Recipe.slug).filter(
            Recipe.id != recipe_id, Recipe.slug.like(f"{prefix}%")
        )
    }
    candidate, suffix = base, 1
    while candidate in occupied:
        suffix += 1
        tail = f"-{suffix}"
        candidate = f"{base[: _SLUG_MAX_LENGTH - len(tail)].rstrip('-')}{tail}"
    return candidate


def _commit_publish_retrying(
    stage: Callable[[Dict[str, Any]], Recipe],
    recipe_data: Dict[str, Any],
    recipe_id: str,
    current_slug: Optional[str] = None,
) -> Recipe:
    """Stage and commit a recipe write, retrying slug collisions lost to races.

    The uniqueness probe in _resolve_public_slug is check-then-write: two
    concurrent publications can both observe a slug as free, and the loser's
    commit then violates the unique index. Rather than letting that expected
    IntegrityError surface as a failed create/update, roll back, exclude the
    losing slug, and re-stage with the next suffix. ``stage(data)`` applies
    ``data`` to the ORM (building or mutating the row) and returns the row —
    it is re-invoked on every attempt because rollback discards staged state.

    Resolution always starts from the caller's original payload, not the
    previous attempt's result: resolving from a once-suffixed slug would
    compound the suffix (chili-2-2 after two races instead of chili-3).

    Only genuine slug races are retried: after rollback, a query must confirm
    another recipe now owns the attempted slug. Constraint names in the
    IntegrityError are backend-specific (SQLite vs PostgreSQL), so ownership
    of the slug is the portable signal. Every other integrity failure —
    PK/not-null violations, or errors from unrelated objects staged on the
    shared session — re-raises immediately rather than being retried into a
    commit that silently drops that other work.
    """
    resolver_input = dict(recipe_data)
    skip: set = set()
    attempts = 0
    while True:
        if recipe_data["is_public"]:
            recipe_data["slug"] = _resolve_public_slug(
                resolver_input, recipe_id, current_slug, skip=frozenset(skip)
            )
        recipe = stage(recipe_data)
        try:
            db.session.commit()
            return recipe
        except IntegrityError:
            db.session.rollback()
            attempts += 1
            attempted_slug = recipe_data.get("slug")
            lost_slug_race = (
                recipe_data["is_public"]
                and attempted_slug is not None
                and Recipe.query.filter(
                    Recipe.slug == attempted_slug, Recipe.id != recipe_id
                ).first()
                is not None
            )
            if not lost_slug_race or attempts > _SLUG_COMMIT_RETRIES:
                raise
            skip.add(attempted_slug)
            logger.warning(
                "Slug %r for recipe %s lost a publication race; retrying",
                sanitize_log_value(attempted_slug),
                sanitize_log_value(recipe_id),
            )


def _apply_recipe_scope(query, user_id: Optional[int], guest_session_id: Optional[str]):
    """Scope queries to either authenticated user or anonymous session."""
    if user_id is not None:
        return query.filter_by(user_id=user_id)
    if guest_session_id:
        return query.filter_by(user_id=None, guest_session_id=guest_session_id)
    return query.filter_by(user_id=None, guest_session_id=None)


def _gate_is_public(recipe_data: Dict[str, Any], user_id: Optional[int]) -> Dict[str, Any]:
    """Only authenticated users may publish: guests get is_public forced False.

    A guest_session_id is a throwaway browser token — there is no accountable
    owner to moderate or ban behind a guest-published /r/<slug> page. The flag
    is normalized in the data blob itself so the JSON payload and the
    is_public column can never disagree.
    """
    wants_public = recipe_data.get("is_public") is True
    if wants_public and user_id is None:
        logger.warning(
            "Guest attempted to publish recipe %s — forcing is_public=False",
            sanitize_log_value(recipe_data.get("id", "<no id>")),
        )
    return {**recipe_data, "is_public": wants_public if user_id is not None else False}


def get_user_recipes(
    user_id: Optional[int], guest_session_id: Optional[str] = None
) -> List[Recipe]:
    """
    Get all recipes for a specific user.

    Args:
        user_id: Database ID of the user (None for anonymous recipes)

    Returns:
        List of Recipe objects sorted by creation date (newest first)
    """
    try:
        query = _apply_recipe_scope(Recipe.query, user_id, guest_session_id).order_by(
            Recipe.created_at.desc()
        )
        return query.all()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            "Error fetching recipes for user %s: %s",
            sanitize_log_value(user_id),
            sanitize_log_value(e),
        )
        return []


def get_recipe_by_id(
    recipe_id: str,
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
) -> Optional[Recipe]:
    """
    Get a specific recipe by ID.

    Args:
        recipe_id: UUID of the recipe
        user_id: Optional user ID for ownership verification

    Returns:
        Recipe object if found and owned by user (or anonymous), None otherwise
    """
    try:
        query = _apply_recipe_scope(Recipe.query.filter_by(id=recipe_id), user_id, guest_session_id)

        return query.first()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            "Error fetching recipe %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        return None


def get_recipe_for_worker(recipe_id: str) -> Optional[Recipe]:
    """Fetch a recipe without owner scoping for an OIDC-authenticated worker."""
    return cast(
        Optional[Recipe],
        Recipe.query.populate_existing().filter_by(id=recipe_id).first(),
    )


def claim_recipe_for_worker(
    recipe_id: str,
    expected_status: str,
    processing_status: str,
    stale_after_seconds: int,
) -> Optional[str]:
    """Atomically claim one generation job and return its unique lease token."""
    now = datetime.utcnow()
    stale_before = now - timedelta(seconds=stale_after_seconds)
    claim_token = str(uuid.uuid4())
    unclaimed_status = Recipe.status == expected_status
    if expected_status == processing_status:
        unclaimed_status = and_(
            unclaimed_status,
            Recipe.worker_claim_token.is_(None),
        )
    claimed = cast(
        int,
        Recipe.query.filter(
            Recipe.id == recipe_id,
            or_(
                unclaimed_status,
                and_(
                    Recipe.status == processing_status,
                    Recipe.updated_at < stale_before,
                ),
            ),
        ).update(
            {
                "status": processing_status,
                "worker_claim_token": claim_token,
                "updated_at": now,
            },
            synchronize_session=False,
        ),
    )
    db.session.commit()
    return claim_token if claimed == 1 else None


def set_recipe_status_for_worker(
    recipe_id: str,
    status: str,
    claim_token: str,
    expected_status: Optional[str] = None,
    release_claim: bool = False,
) -> bool:
    """Heartbeat or release worker state only while the caller owns the lease."""
    query = Recipe.query.filter(
        Recipe.id == recipe_id,
        Recipe.worker_claim_token == claim_token,
    )
    if expected_status is not None:
        query = query.filter(Recipe.status == expected_status)
    updated = cast(
        int,
        query.update(
            {
                "status": status,
                "worker_claim_token": None if release_claim else claim_token,
                "updated_at": datetime.utcnow(),
            },
            synchronize_session=False,
        ),
    )
    db.session.commit()
    return updated == 1


def update_recipe_for_worker(
    recipe_id: str,
    recipe_data: Dict[str, Any],
    claim_token: str,
    status: str,
    expected_status: str,
) -> Optional[WorkerRecipeUpdate]:
    """Persist generated data only if the caller still owns the worker lease."""
    recipe = (
        Recipe.query.populate_existing()
        .filter(
            Recipe.id == recipe_id,
            Recipe.status == expected_status,
            Recipe.worker_claim_token == claim_token,
        )
        .first()
    )
    if recipe is None:
        return None

    result = WorkerRecipeUpdate(recipe.user_id, recipe.guest_session_id)
    observed_updated_at = recipe.updated_at
    merged = {**(recipe.data or {}), **recipe_data, "id": recipe_id}
    merged["is_public"] = recipe.is_public
    if recipe.slug is not None:
        merged["slug"] = recipe.slug
    else:
        merged.pop("slug", None)

    updated = cast(
        int,
        Recipe.query.filter(
            Recipe.id == recipe_id,
            Recipe.status == expected_status,
            Recipe.worker_claim_token == claim_token,
            Recipe.updated_at == observed_updated_at,
        ).update(
            {
                "name": recipe_data.get("name", recipe.name),
                "data": merged,
                "status": status,
                "worker_claim_token": None,
                "updated_at": datetime.utcnow(),
            },
            synchronize_session=False,
        ),
    )
    db.session.commit()
    if updated != 1:
        return None
    return result


def patch_recipe_for_worker(
    recipe_id: str,
    recipe_patch: Dict[str, Any],
    claim_token: str,
    status: str,
    expected_status: str,
    remove_data_fields: tuple[str, ...] = (),
) -> Optional[WorkerRecipeUpdate]:
    """Patch worker-owned fields without overwriting concurrent user edits."""
    for _ in range(3):
        recipe = (
            Recipe.query.populate_existing()
            .filter(
                Recipe.id == recipe_id,
                Recipe.status == expected_status,
                Recipe.worker_claim_token == claim_token,
            )
            .first()
        )
        if recipe is None:
            return None

        result = WorkerRecipeUpdate(recipe.user_id, recipe.guest_session_id)
        observed_updated_at = recipe.updated_at
        merged = dict(recipe.data or {})
        patch = dict(recipe_patch)
        metadata_patch = patch.pop("ai_metadata", None)
        merged.update(patch)
        if isinstance(metadata_patch, dict):
            metadata = dict(merged.get("ai_metadata") or {})
            metadata.update(metadata_patch)
            merged["ai_metadata"] = metadata
        for field in remove_data_fields:
            merged.pop(field, None)

        merged["id"] = recipe_id
        merged["is_public"] = recipe.is_public
        if recipe.slug is not None:
            merged["slug"] = recipe.slug
        else:
            merged.pop("slug", None)

        updated = cast(
            int,
            Recipe.query.filter(
                Recipe.id == recipe_id,
                Recipe.status == expected_status,
                Recipe.worker_claim_token == claim_token,
                Recipe.updated_at == observed_updated_at,
            ).update(
                {
                    "data": merged,
                    "status": status,
                    "worker_claim_token": None,
                    "updated_at": datetime.utcnow(),
                },
                synchronize_session=False,
            ),
        )
        db.session.commit()
        if updated == 1:
            return result
        db.session.expire_all()
    return None


def create_recipe(
    recipe_data: Dict[str, Any],
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
    status: str = "ready",
) -> Optional[Recipe]:
    """
    Create a new recipe in the database.

    Args:
        recipe_data: Full recipe JSON data
        user_id: Optional user ID (None for anonymous recipes)
        guest_session_id: Guest owner scope when user_id is None
        status: Initial generation state, persisted in the creation transaction

    Returns:
        Created Recipe object, or None if creation failed
    """
    try:
        # Use the id from recipe_data if present, otherwise generate a new UUID
        recipe_id = recipe_data.get("id", str(uuid.uuid4()))
        existing = cast(
            Optional[Recipe],
            Recipe.query.populate_existing().filter_by(id=recipe_id).with_for_update().first(),
        )
        if existing:
            same_owner = (user_id is not None and existing.user_id == user_id) or (
                user_id is None
                and existing.user_id is None
                and existing.guest_session_id == guest_session_id
            )
            if not same_owner:
                logger.warning(
                    "Recipe ID collision for id=%s (user_id=%s, guest_session_id=%s)",
                    sanitize_log_value(recipe_id),
                    sanitize_log_value(user_id),
                    sanitize_log_value(guest_session_id),
                )
                return None

            merged = {**(existing.data or {}), **recipe_data, "id": recipe_id}
            _preserve_worker_metadata(existing.data or {}, merged)
            if "is_public" not in recipe_data:
                merged["is_public"] = existing.is_public
            if "slug" not in recipe_data:
                if existing.slug is not None:
                    merged["slug"] = existing.slug
                else:
                    merged.pop("slug", None)
            recipe_data_with_id = _gate_is_public(merged, user_id)
            recipe_name = recipe_data.get("name", existing.name)
            next_status = existing.status if existing.status in _ACTIVE_RECIPE_STATUSES else status

            def stage_existing(data: Dict[str, Any]) -> Recipe:
                existing.name = recipe_name
                existing.slug = data.get("slug")
                existing.is_public = data.get("is_public", False)
                existing.data = data
                existing.status = next_status
                existing.updated_at = datetime.utcnow()
                return existing  # type: ignore[no-any-return]

            return _commit_publish_retrying(
                stage_existing, recipe_data_with_id, recipe_id, existing.slug
            )

        recipe_name = recipe_data.get("name", "Unnamed Recipe")
        recipe_data_with_id = _gate_is_public({**recipe_data, "id": recipe_id}, user_id)

        def stage_new(data: Dict[str, Any]) -> Recipe:
            recipe = Recipe(
                id=recipe_id,
                user_id=user_id,
                guest_session_id=None if user_id is not None else guest_session_id,
                name=recipe_name,
                slug=data.get("slug"),
                is_public=data.get("is_public", False),
                data=data,
                status=status,
            )
            db.session.add(recipe)
            return recipe

        recipe = _commit_publish_retrying(stage_new, recipe_data_with_id, recipe_id)

        logger.info(
            "Created recipe %s for user %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(user_id),
        )
        return recipe

    except RecipeSlugError:
        raise
    except Exception as e:
        logger.error("Error creating recipe: %s", sanitize_log_value(e))
        db.session.rollback()
        return None


def update_recipe(
    recipe_id: str,
    recipe_data: Dict[str, Any],
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
    status: Optional[str] = None,
) -> Optional[Recipe]:
    """
    Update an existing recipe.

    Args:
        recipe_id: UUID of the recipe to update
        recipe_data: New recipe JSON data
        user_id: Optional user ID for ownership verification

    Returns:
        Updated Recipe object, or None if not found or update failed
    """
    try:
        recipe = cast(
            Optional[Recipe],
            _apply_recipe_scope(
                Recipe.query.populate_existing().filter(Recipe.id == recipe_id),
                user_id,
                guest_session_id,
            )
            .with_for_update()
            .first(),
        )

        if not recipe:
            logger.warning(
                "Recipe %s not found for user %s",
                sanitize_log_value(recipe_id),
                sanitize_log_value(user_id),
            )
            return None

        # Merge the payload into the persisted blob (and pin the id): PUT
        # accepts partial payloads such as {"is_public": true}, and replacing
        # the blob wholesale would delete ingredients/instructions at the
        # moment of publishing. Keys the payload does supply always win.
        merged = {**(recipe.data or {}), **recipe_data, "id": recipe_id}
        _preserve_worker_metadata(recipe.data or {}, merged)

        if "is_public" not in recipe_data:
            # The is_public column is authoritative, like slug below: a
            # partial PUT must not publish or unpublish a recipe off a
            # stale blob value.
            merged["is_public"] = recipe.is_public

        recipe_data_with_id = _gate_is_public(merged, user_id)

        if "name" not in recipe_data_with_id and recipe.name:
            # Publish-only partial updates keep the persisted name (see
            # stage_update), so slug derivation must see it too — otherwise
            # {"is_public": true} on a slug-less row is wrongly rejected.
            # Writing it into the blob also keeps blob and column agreeing.
            # Key presence, not truthiness: an explicit falsy name is the
            # caller's value and goes to both blob and column unchanged.
            recipe_data_with_id["name"] = recipe.name

        if "slug" not in recipe_data:
            # The slug column is authoritative — scripts/backfill_slugs.py
            # rewrites it without touching the blob — so a partial PUT that
            # omits slug must not revert the column to a stale blob value.
            # Syncing the merged dict also realigns the blob with the column.
            if recipe.slug is not None:
                recipe_data_with_id["slug"] = recipe.slug
            else:
                recipe_data_with_id.pop("slug", None)

        def stage_update(data: Dict[str, Any]) -> Recipe:
            recipe.name = recipe_data.get("name", recipe.name)
            recipe.slug = data.get("slug")
            recipe.is_public = data["is_public"]
            recipe.data = data
            if status is not None:
                recipe.status = status
            recipe.updated_at = datetime.utcnow()
            return recipe

        updated = _commit_publish_retrying(
            stage_update, recipe_data_with_id, recipe_id, recipe.slug
        )

        logger.info("Updated recipe %s", sanitize_log_value(recipe_id))
        return updated

    except RecipeSlugError:
        raise
    except Exception as e:
        logger.error(
            "Error updating recipe %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        db.session.rollback()
        return None


def update_recipe_status(
    recipe_id: str,
    status: str,
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
    expected_status: Optional[str] = None,
    require_unclaimed: bool = False,
    clear_worker_claim: bool = False,
) -> bool:
    """Conditionally update an owner-scoped recipe status."""
    try:
        query = _apply_recipe_scope(
            Recipe.query.filter(Recipe.id == recipe_id),
            user_id,
            guest_session_id,
        )
        if expected_status is not None:
            query = query.filter(Recipe.status == expected_status)
        if require_unclaimed:
            query = query.filter(Recipe.worker_claim_token.is_(None))

        values = {
            "status": status,
            "updated_at": datetime.utcnow(),
        }
        if clear_worker_claim:
            values["worker_claim_token"] = None
        updated = cast(
            int,
            query.update(values, synchronize_session=False),
        )
        db.session.commit()
        return updated == 1
    except Exception as e:
        logger.error(
            "Error updating status for recipe %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        db.session.rollback()
        return False


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


def delete_recipe(
    recipe_id: str,
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
) -> bool:
    """
    Delete a recipe from the database.

    Args:
        recipe_id: UUID of the recipe to delete
        user_id: Optional user ID for ownership verification

    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        recipe = get_recipe_by_id(recipe_id, user_id, guest_session_id)

        if not recipe:
            logger.warning(
                "Recipe %s not found for user %s",
                sanitize_log_value(recipe_id),
                sanitize_log_value(user_id),
            )
            return False

        db.session.delete(recipe)
        db.session.commit()

        logger.info("Deleted recipe %s", sanitize_log_value(recipe_id))
        return True

    except Exception as e:
        logger.error(
            "Error deleting recipe %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        db.session.rollback()
        return False


def get_all_recipes(limit: int = 100) -> List[Recipe]:
    """
    Get all recipes across all users (admin function).

    Args:
        limit: Maximum number of recipes to return

    Returns:
        List of Recipe objects
    """
    try:
        query = Recipe.query.order_by(Recipe.created_at.desc())
        return query.limit(limit).all()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error("Error fetching all recipes: %s", sanitize_log_value(e))
        return []


def count_user_recipes(user_id: Optional[int], guest_session_id: Optional[str] = None) -> int:
    """
    Count the number of recipes for a user.

    Args:
        user_id: Database ID of the user

    Returns:
        Number of recipes
    """
    try:
        scoped = _apply_recipe_scope(Recipe.query, user_id, guest_session_id)
        return scoped.count()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            "Error counting recipes for user %s: %s",
            sanitize_log_value(user_id),
            sanitize_log_value(e),
        )
        return 0


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

        recipe = Recipe(
            id=recipe_id,
            user_id=user_id,
            name=recipe_name,
            slug=recipe_data.get("slug"),
            is_public=recipe_data.get("is_public", False),
            data=recipe_data,
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
