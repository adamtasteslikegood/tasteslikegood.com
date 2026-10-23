"""
Database-backed recipe repository for Phase 3.

Handles recipe CRUD operations with SQLAlchemy ORM:
- Create, read, update, delete recipes
- User-scoped queries (recipes belong to users)
- Support for anonymous recipes (user_id = NULL)

Slug resolution, retired-slug tombstones, and publication gates live in
``repositories.recipe_slugs``; async worker lease/status/patch operations and
content locking live in ``repositories.recipe_worker_ops``; image-generation
queueing and legacy file migration live in ``repositories.recipe_image_queue``.
All public and private helpers remain re-exported here for backward
compatibility.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, cast

from extensions import db
from models import Recipe, RetiredSlug
from models.retired_slug import lock_slug
from repositories.recipe_image_queue import (
    ImageGenerationQueue,
    RecipeImageQueueMixin,
    _strip_inherited_stock_image,
    migrate_file_to_db,
    queue_image_generation,
    release_image_generation_queue,
)
from repositories.recipe_slugs import (
    _SLUG_COMMIT_RETRIES,
    _SLUG_MAX_LENGTH,
    CANONICAL_RECIPE_LOCKED_ERROR,
    DUPLICATE_CODE_ALREADY_SAVED,
    MANUAL_RECIPE_UNPUBLISHABLE_ERROR,
    PUBLIC_SLUG_REQUIRED_ERROR,
    PUBLISHABLE_RECIPE_STATUSES,
    PUBLISHED_RECIPE_DELETE_ERROR,
    RECIPE_DUPLICATE_ERROR,
    SAVED_COPY_PUBLISH_ERROR,
    CanonicalRecipeError,
    ManualRecipeError,
    PublishGateError,
    PublishedRecipeDeleteError,
    RecipeConflictError,
    RecipeDuplicateError,
    RecipeSlugError,
    RetiredSlugMixin,
    SavedCopyPublishError,
    _clear_unowned_retired_slug_from_private,
    _commit_publish_retrying,
    _duplicate_source_slug_owner,
    _gate_is_public,
    _gate_publish_transition,
    _guard_canonical,
    _is_saved_copy,
    _pin_source_slug_to_column,
    _resolve_public_slug,
    _resolve_source_recipe_id,
    _slug_occupied_against,
    _slug_retired_against,
    _slugify,
)
from repositories.recipe_worker_ops import (
    _ACTIVE_RECIPE_STATUSES,
    _CLIENT_ORIGINS,
    _LOCKED_CLIENT_FIELDS,
    _TEXT_GENERATION_STATUSES,
    _WORKER_METADATA_KEYS,
    _WORKER_TEXT_DROP_FIELDS,
    UNFINISHED_GENERATION_STATUSES,
    RecipeWorkerOpsMixin,
    WorkerRecipeUpdate,
    _apply_recipe_scope,
    _dedup_rows,
    _is_content_locked,
    _listed_recipes,
    _lock_client_payload,
    _preserve_worker_metadata,
    _resolve_origin,
    claim_recipe_for_worker,
    get_recipe_for_worker,
    patch_recipe_for_worker,
    set_recipe_status_for_worker,
    update_recipe_for_worker,
    update_recipe_status,
)
from services.recipe_metadata import queue_if_missing
from utils.log_sanitizer import sanitize_log_value
from utils.slug_utils import normalize_slug

logger = logging.getLogger(__name__)

# Also returned verbatim by the API routes (fixed string, same rationale as
# PUBLIC_SLUG_REQUIRED_ERROR in recipe_slugs).
RECIPE_OWNERSHIP_ERROR = (
    "This recipe belongs to a different account or guest session, so it "
    "cannot be saved or published from here."
)

# Machine-readable discriminators for the refusal, sent alongside the message as
# `code` (KAN-155). One 409 was carrying three situations with opposite remedies,
# and the client cannot tell them apart from the prose: "belongs to a different
# account or guest session" reads as final, but two of the three are recoverable
# by the user right now. Only the server knows which one fired — it is the only
# party that has looked at the stored row.
#
#   OTHER_ACCOUNT       the row belongs to a real, different account. Final; the
#                       acting user cannot resolve this. INV-4's core case.
#   OTHER_GUEST_SESSION both sides are guests, different sessions. RCP-61's stale
#                       tab: the user very likely owns this row under a session
#                       the page no longer holds. Logging in resolves it.
#   ORPHANED_GUEST_ROW  the row has no user_id and the caller IS authenticated —
#                       a guest row that was never claimed at login-merge. This
#                       is KAN-155's known-incomplete case, called out in #256 as
#                       still failing; the repair policy is Adam's open call.
#
# These are a superset of the message, never a replacement: the prose stays the
# fallback for any client that does not read `code`.
OWNERSHIP_CODE_OTHER_ACCOUNT = "OWNERSHIP_OTHER_ACCOUNT"
OWNERSHIP_CODE_OTHER_GUEST_SESSION = "OWNERSHIP_OTHER_GUEST_SESSION"
OWNERSHIP_CODE_ORPHANED_GUEST_ROW = "OWNERSHIP_ORPHANED_GUEST_ROW"


class RecipeOwnershipError(RecipeConflictError):
    """The row exists but is owned by someone else — refuse the write (KAN-155).

    This refusal is deliberate and load-bearing: it is what stops one account's
    write from landing on another account's row. See KAN-181 INV-4, verified in
    production 2026-07-29. Do not relax the ownership test to make this stop
    firing — the defect this exception fixes is that the refusal was
    indistinguishable from a server error, not that the refusal happened.

    `code` narrows WHICH refusal fired (see the OWNERSHIP_CODE_* constants). It
    changes what the client can honestly tell the user; it does not change the
    decision. Every code still refuses, and still writes nothing.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def get_user_recipes(
    user_id: Optional[int], guest_session_id: Optional[str] = None
) -> List[Recipe]:
    """
    Get a user's recipes, without rows that never finished generating.

    Args:
        user_id: Database ID of the user (None for anonymous recipes)

    Returns:
        List of Recipe objects sorted by creation date (newest first)
    """
    try:
        query = _listed_recipes(
            _apply_recipe_scope(Recipe.query, user_id, guest_session_id)
        ).order_by(Recipe.created_at.desc())
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
                # KAN-155: the refusal itself is correct and unchanged. What was
                # wrong is that returning bare None made it indistinguishable
                # from an internal failure, so the route answered 500 and the UI
                # blamed the user's connection for a deliberate refusal.
                #
                # The code narrows which refusal this is. Order matters: check
                # the row's user_id first, because "existing.user_id is None"
                # means something different depending on whether the CALLER is
                # authenticated (unclaimed guest row the caller may well own) or
                # a guest (someone else's live guest session).
                if existing.user_id is not None:
                    code = OWNERSHIP_CODE_OTHER_ACCOUNT
                elif user_id is not None:
                    code = OWNERSHIP_CODE_ORPHANED_GUEST_ROW
                else:
                    code = OWNERSHIP_CODE_OTHER_GUEST_SESSION
                # Classify BEFORE logging, so the log carries the discriminator.
                # This is not just ops garnish: KAN-155's remaining open item is
                # the ownership-repair policy (reassign orphaned guest rows at
                # login-merge vs a one-off backfill), and choosing between those
                # depends on how often ORPHANED_GUEST_ROW actually fires against
                # OTHER_ACCOUNT in production. There is no staging environment
                # (KAN-182), so this log line is the only place that question can
                # be answered. Without the code every refusal reads identically
                # and the data does not exist. `code` is a module constant, so it
                # needs no sanitizing — unlike the caller-supplied values below.
                logger.warning(
                    "Recipe ID collision (%s) for id=%s (user_id=%s, guest_session_id=%s)",
                    code,
                    sanitize_log_value(recipe_id),
                    sanitize_log_value(user_id),
                    sanitize_log_value(guest_session_id),
                )
                raise RecipeOwnershipError(RECIPE_OWNERSHIP_ERROR, code)

            # KAN-328: filter BEFORE anything reads the payload, so the name
            # column, slug, sourceSlug and media below all see the locked set.
            recipe_data = _lock_client_payload(existing, recipe_data)
            _guard_canonical(existing, recipe_data)

            merged = {**(existing.data or {}), **recipe_data, "id": recipe_id}
            _preserve_worker_metadata(existing.data or {}, merged)
            # KAN-215: a payload echo must not re-pin a saved copy to a stock
            # image inherited from its source. Same reasoning as is_canonical
            # below — enforced on every write path, not just create. Guard on
            # the RESULTING state, not the pre-update state: a payload that
            # first-turns a non-copy row into a saved copy (by supplying
            # ``sourceSlug``) would otherwise slip past a pre-update check and
            # persist the inherited URL on the very write that made it a copy.
            if _is_saved_copy(existing) or recipe_data.get("sourceSlug"):
                _strip_inherited_stock_image(merged)
            # The is_canonical column is never writable through the API; pin
            # the blob to the column so a payload echo can't fake a lock.
            merged["is_canonical"] = existing.is_canonical
            if "is_public" not in recipe_data:
                merged["is_public"] = existing.is_public
            if "slug" not in recipe_data:
                if existing.slug is not None:
                    merged["slug"] = existing.slug
                else:
                    merged.pop("slug", None)
            _pin_source_slug_to_column(merged, recipe_data, existing)
            recipe_data_with_id = _gate_is_public(merged, user_id, existing)
            next_origin = _resolve_origin(existing.origin, recipe_data)
            _gate_publish_transition(existing, recipe_data_with_id)
            if next_origin is not None:
                recipe_data_with_id["origin"] = next_origin
            else:
                recipe_data_with_id.pop("origin", None)
            recipe_name = recipe_data.get("name", existing.name)
            next_status = existing.status if existing.status in _ACTIVE_RECIPE_STATUSES else status

            def stage_existing(data: Dict[str, Any]) -> Recipe:
                existing.name = recipe_name
                existing.slug = data.get("slug")
                existing.is_public = data.get("is_public", False)
                existing.source_slug = data.get("sourceSlug")
                existing.source_recipe_id = _resolve_source_recipe_id(
                    existing.source_recipe_id, data.get("sourceSlug"), recipe_id
                )
                existing.origin = next_origin
                existing.data = data
                existing.status = next_status
                existing.updated_at = datetime.utcnow()
                return existing

            return _commit_publish_retrying(
                stage_existing,
                recipe_data_with_id,
                recipe_id,
                existing.slug,
                owner_scope=(existing.user_id, existing.guest_session_id),
            )

        recipe_name = recipe_data.get("name", "Unnamed Recipe")
        recipe_data_with_id = _gate_is_public({**recipe_data, "id": recipe_id}, user_id)
        # A client cannot mint its own canonical lock.
        recipe_data_with_id.pop("is_canonical", None)
        # KAN-329: a new row has no worker text behind it, so it is never
        # created public. Saved private rather than refused: old bundles POST
        # new rows with the flag set, and a 400 would leave them a local
        # "published" ghost the server never had.
        if recipe_data_with_id.get("is_public") is True:
            logger.info(
                "New recipe %s asked to be public; saved private",
                sanitize_log_value(recipe_id),
            )
            recipe_data_with_id["is_public"] = False
        origin_value = _resolve_origin(None, recipe_data)
        if origin_value is not None:
            recipe_data_with_id["origin"] = origin_value
        else:
            recipe_data_with_id.pop("origin", None)

        def stage_new(data: Dict[str, Any]) -> Recipe:
            # KAN-221: set author/saver columns and the stable provenance key
            # on create.
            source_slug = data.get("sourceSlug")
            if source_slug is not None:
                # Saved copy: look up the source recipe — its owner is the
                # author, its id is the immutable provenance key. Only public
                # sources resolve (matches migration backfill predicate).
                source = Recipe.query.filter(
                    Recipe.slug == source_slug, Recipe.is_public.is_(True)
                ).first()
                author_id = source.user_id if source else None
                source_recipe_id = source.id if source else None
                saved_to_id = user_id
                # KAN-215: the SPA's saved-copy payload mirrors the source's
                # stock_image_url. Strip that inherited value at the server
                # boundary so it cannot masquerade as an image the copy owns.
                # Otherwise _recipe_image_url returns it before resolving the
                # source and pins the copy to the stock image even after the
                # source gains an AI image. The immutable source_recipe_id
                # retains provenance; public_bp resolves current source media
                # at render time instead.
                _strip_inherited_stock_image(data)
            else:
                # Original recipe: author = owner, no saver, no source.
                author_id = user_id
                source_recipe_id = None
                saved_to_id = None

            recipe = Recipe(
                id=recipe_id,
                user_id=user_id,
                guest_session_id=None if user_id is not None else guest_session_id,
                name=recipe_name,
                slug=data.get("slug"),
                is_public=data.get("is_public", False),
                source_slug=source_slug,
                source_recipe_id=source_recipe_id,
                origin=origin_value,
                user_id_author=author_id,
                user_id_saved_to=saved_to_id,
                data=data,
                status=status,
            )
            db.session.add(recipe)
            return recipe

        recipe = _commit_publish_retrying(
            stage_new,
            recipe_data_with_id,
            recipe_id,
            owner_scope=(user_id, None if user_id is not None else guest_session_id),
        )

        logger.info(
            "Created recipe %s for user %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(user_id),
        )
        return recipe

    except (
        RecipeSlugError,
        CanonicalRecipeError,
        ManualRecipeError,
        SavedCopyPublishError,
        RecipeOwnershipError,
        RecipeDuplicateError,
    ):
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

        # KAN-328: same lock as the upsert branch, applied before any read.
        recipe_data = _lock_client_payload(recipe, recipe_data)
        _guard_canonical(recipe, recipe_data)

        # Merge the payload into the persisted blob (and pin the id): PUT
        # accepts partial payloads such as {"is_public": true}, and replacing
        # the blob wholesale would delete ingredients/instructions at the
        # moment of publishing. Keys the payload does supply always win.
        merged = {**(recipe.data or {}), **recipe_data, "id": recipe_id}
        _preserve_worker_metadata(recipe.data or {}, merged)
        if _is_saved_copy(recipe) or recipe_data.get("sourceSlug"):
            _strip_inherited_stock_image(merged)
        # Never writable through the API — pin the blob to the column.
        merged["is_canonical"] = recipe.is_canonical

        if "is_public" not in recipe_data:
            # The is_public column is authoritative, like slug below: a
            # partial PUT must not publish or unpublish a recipe off a
            # stale blob value.
            merged["is_public"] = recipe.is_public

        # Pin BEFORE the gate, so the gate never reads a stale blob value the
        # column has disowned (or a payload null the column overrides).
        _pin_source_slug_to_column(merged, recipe_data, recipe)

        recipe_data_with_id = _gate_is_public(merged, user_id, recipe)

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

        next_origin = _resolve_origin(recipe.origin, recipe_data)
        _gate_publish_transition(recipe, recipe_data_with_id)
        if next_origin is not None:
            recipe_data_with_id["origin"] = next_origin
        else:
            recipe_data_with_id.pop("origin", None)

        def stage_update(data: Dict[str, Any]) -> Recipe:
            recipe.name = recipe_data.get("name", recipe.name)
            recipe.slug = data.get("slug")
            recipe.is_public = data["is_public"]
            recipe.source_slug = data.get("sourceSlug")
            recipe.source_recipe_id = _resolve_source_recipe_id(
                recipe.source_recipe_id, data.get("sourceSlug"), recipe_id
            )
            recipe.origin = next_origin
            recipe.data = data
            if status is not None:
                recipe.status = status
            recipe.updated_at = datetime.utcnow()
            return recipe

        updated = _commit_publish_retrying(
            stage_update,
            recipe_data_with_id,
            recipe_id,
            recipe.slug,
            owner_scope=(recipe.user_id, recipe.guest_session_id),
        )

        logger.info("Updated recipe %s", sanitize_log_value(recipe_id))
        return updated

    except (
        RecipeSlugError,
        CanonicalRecipeError,
        ManualRecipeError,
        SavedCopyPublishError,
        RecipeDuplicateError,
    ):
        raise
    except Exception as e:
        logger.error(
            "Error updating recipe %s: %s",
            sanitize_log_value(recipe_id),
            sanitize_log_value(e),
        )
        db.session.rollback()
        return None


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
        # Serialize deletion with publish/rename updates.  In addition to the
        # row lock, ``populate_existing`` is important here: this scoped
        # session may already hold a stale private Recipe while a concurrent
        # transaction has just published it.  Rechecking the locked database
        # row keeps the 409 guard authoritative.
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
            return False

        if recipe.is_canonical:
            logger.warning(
                "Refusing to delete canonical recipe %s (slug=%s)",
                sanitize_log_value(recipe_id),
                sanitize_log_value(recipe.slug),
            )
            raise CanonicalRecipeError(CANONICAL_RECIPE_LOCKED_ERROR)

        if recipe.is_public:
            # KAN-288: deleting would retire the live /r/<slug> for good, so
            # unpublishing is its own, explicit (and reversible) step first.
            logger.info(
                "Refusing to delete published recipe %s (slug=%s)",
                sanitize_log_value(recipe_id),
                sanitize_log_value(recipe.slug),
            )
            raise PublishedRecipeDeleteError(PUBLISHED_RECIPE_DELETE_ERROR)

        db.session.delete(recipe)
        db.session.commit()

        logger.info("Deleted recipe %s", sanitize_log_value(recipe_id))
        return True

    except (CanonicalRecipeError, PublishedRecipeDeleteError):
        raise
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
    Count a user's recipes: the same rows get_user_recipes() lists.

    Args:
        user_id: Database ID of the user

    Returns:
        Number of recipes
    """
    try:
        scoped = _listed_recipes(_apply_recipe_scope(Recipe.query, user_id, guest_session_id))
        return scoped.count()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(
            "Error counting recipes for user %s: %s",
            sanitize_log_value(user_id),
            sanitize_log_value(e),
        )
        return 0


class DbRecipeRepository(
    RetiredSlugMixin,
    RecipeWorkerOpsMixin,
    RecipeImageQueueMixin,
):
    """Facade class combining recipe CRUD, slug resolution, worker ops, and image queue."""

    get_user_recipes = staticmethod(get_user_recipes)
    get_recipe_by_id = staticmethod(get_recipe_by_id)
    create_recipe = staticmethod(create_recipe)
    update_recipe = staticmethod(update_recipe)
    delete_recipe = staticmethod(delete_recipe)
    get_all_recipes = staticmethod(get_all_recipes)
    count_user_recipes = staticmethod(count_user_recipes)


PostgresRecipeRepository = DbRecipeRepository

__all__ = [
    "DbRecipeRepository",
    "PostgresRecipeRepository",
    "RetiredSlugMixin",
    "RecipeWorkerOpsMixin",
    "RecipeImageQueueMixin",
    "WorkerRecipeUpdate",
    "ImageGenerationQueue",
    "PublishGateError",
    "RecipeConflictError",
    "RecipeSlugError",
    "CanonicalRecipeError",
    "PublishedRecipeDeleteError",
    "ManualRecipeError",
    "SavedCopyPublishError",
    "RecipeDuplicateError",
    "RecipeOwnershipError",
    "PUBLIC_SLUG_REQUIRED_ERROR",
    "CANONICAL_RECIPE_LOCKED_ERROR",
    "PUBLISHED_RECIPE_DELETE_ERROR",
    "MANUAL_RECIPE_UNPUBLISHABLE_ERROR",
    "PUBLISHABLE_RECIPE_STATUSES",
    "SAVED_COPY_PUBLISH_ERROR",
    "RECIPE_OWNERSHIP_ERROR",
    "OWNERSHIP_CODE_OTHER_ACCOUNT",
    "OWNERSHIP_CODE_OTHER_GUEST_SESSION",
    "OWNERSHIP_CODE_ORPHANED_GUEST_ROW",
    "RECIPE_DUPLICATE_ERROR",
    "DUPLICATE_CODE_ALREADY_SAVED",
    "UNFINISHED_GENERATION_STATUSES",
    "_SLUG_MAX_LENGTH",
    "_SLUG_COMMIT_RETRIES",
    "_WORKER_METADATA_KEYS",
    "_ACTIVE_RECIPE_STATUSES",
    "_CLIENT_ORIGINS",
    "_LOCKED_CLIENT_FIELDS",
    "_TEXT_GENERATION_STATUSES",
    "_WORKER_TEXT_DROP_FIELDS",
    "_is_content_locked",
    "_lock_client_payload",
    "_resolve_origin",
    "_gate_publish_transition",
    "_guard_canonical",
    "_slugify",
    "_preserve_worker_metadata",
    "_strip_inherited_stock_image",
    "_is_saved_copy",
    "_slug_retired_against",
    "_slug_occupied_against",
    "_clear_unowned_retired_slug_from_private",
    "_resolve_public_slug",
    "_pin_source_slug_to_column",
    "_resolve_source_recipe_id",
    "_duplicate_source_slug_owner",
    "_commit_publish_retrying",
    "_apply_recipe_scope",
    "_gate_is_public",
    "_listed_recipes",
    "_dedup_rows",
    "get_user_recipes",
    "get_recipe_by_id",
    "get_recipe_for_worker",
    "claim_recipe_for_worker",
    "set_recipe_status_for_worker",
    "update_recipe_for_worker",
    "patch_recipe_for_worker",
    "create_recipe",
    "update_recipe",
    "update_recipe_status",
    "queue_image_generation",
    "release_image_generation_queue",
    "delete_recipe",
    "get_all_recipes",
    "count_user_recipes",
    "migrate_file_to_db",
    "RetiredSlug",
    "lock_slug",
    "queue_if_missing",
    "normalize_slug",
]
