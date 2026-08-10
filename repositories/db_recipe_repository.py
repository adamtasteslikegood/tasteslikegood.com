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
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Recipe
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


# Also returned verbatim by the API routes (fixed string, same rationale as
# PUBLIC_SLUG_REQUIRED_ERROR above).
CANONICAL_RECIPE_LOCKED_ERROR = (
    "This is a canonical public recipe: its publish state, slug, and row are "
    "locked. Content edits are still allowed."
)


class CanonicalRecipeError(ValueError):
    """Publish-state, slug, or delete changes to a canonical recipe are locked."""


# Also returned verbatim by the API routes (fixed string, same rationale as
# PUBLIC_SLUG_REQUIRED_ERROR above).
MANUAL_RECIPE_UNPUBLISHABLE_ERROR = (
    "Manually entered recipes cannot be published. Only generated recipes "
    "can have a public page."
)

# RCP-74: saved copies inherit their public page from the source recipe.
SAVED_COPY_PUBLISH_ERROR = "Cannot publish a saved copy."

# 'manual' gates publishing; the others exist so curation can query by
# provenance. NULL = legacy/unknown, treated as publishable.
_ALLOWED_ORIGINS = frozenset({"manual", "generated", "saved"})


class ManualRecipeError(ValueError):
    """Manually entered recipes cannot be published (KAN-140)."""


class SavedCopyPublishError(ValueError):
    """Saved copies cannot be published — the source page owns publication (RCP-74)."""


# Also returned verbatim by the API routes (fixed string, same rationale as
# PUBLIC_SLUG_REQUIRED_ERROR above).
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


# Also returned verbatim by the API routes (fixed string, same rationale as
# PUBLIC_SLUG_REQUIRED_ERROR above).
RECIPE_DUPLICATE_ERROR = "You already have this recipe saved."

# Machine-readable discriminator, sent alongside the message as `code` — same
# contract as the OWNERSHIP_CODE_* constants. A duplicate is a *successful*
# outcome from the user's point of view (the recipe they wanted is in their
# cookbook), which is the opposite of the ownership refusals, so the SPA needs
# to tell them apart to choose between an error toast and a benign one.
DUPLICATE_CODE_ALREADY_SAVED = "RECIPE_ALREADY_SAVED"


class RecipeDuplicateError(ValueError):
    """This owner already has a row for this source_slug — refuse the write (KAN-213).

    Raised when the database refuses the second save of the same public recipe
    by the same owner. The refusal itself comes from a partial unique index
    (uq_recipe_user_source_slug / uq_recipe_guest_source_slug, migration
    c8f3b71d20a4), not from application code: a check-then-write test in the
    SPA cannot close a two-tab race by construction, which is why six previous
    fixes at six different layers did not hold.

    This exception exists so that refusal reaches the client as a 409 rather
    than a 500. Sprint 6 named that the dominant risk (R1): the repository
    catches broadly and returns None, which the blueprint answers with 500, so
    a working constraint would have looked like a server outage and told the
    user to check their connection.
    """

    def __init__(self, message: str, code: str = DUPLICATE_CODE_ALREADY_SAVED):
        super().__init__(message)
        self.code = code


class RecipeOwnershipError(ValueError):
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


def _resolve_origin(current_origin: Optional[str], recipe_data: Dict[str, Any]) -> Optional[str]:
    """Column value for origin: settable while NULL, immutable once set.

    Once a recipe is labeled, a later payload cannot relabel it — otherwise
    a 'manual' recipe could launder itself into 'generated' and slip past
    the publish gate. Unknown labels are dropped rather than stored.
    """
    if current_origin:
        return current_origin
    candidate = recipe_data.get("origin")
    return candidate if candidate in _ALLOWED_ORIGINS else None


def _gate_manual_publish(origin: Optional[str], recipe_data: Dict[str, Any]) -> None:
    """Reject publication of manually entered recipes (KAN-140).

    The manual-entry form is free-text content with no AI mediation; the
    public /r/<slug> surface must not become an open publishing endpoint.
    Raises (a 400 at the API) instead of silently forcing the flag off, so
    the SPA's revert-and-toast path fires rather than diverging from the
    server state.
    """
    if origin == "manual" and recipe_data.get("is_public") is True:
        raise ManualRecipeError(MANUAL_RECIPE_UNPUBLISHABLE_ERROR)


def _guard_canonical(recipe: Recipe, recipe_data: Dict[str, Any]) -> None:
    """Reject payloads that would unpublish or re-slug a canonical recipe.

    Canonical recipes (seeded from specs/canonical-recipes.json in the
    cookbook repo) keep their /r/<slug> URL stable forever; content edits
    pass through untouched. Checked against the caller's raw payload, not
    the merged blob, so only explicit intent trips the guard.
    """
    if not recipe.is_canonical:
        return
    if recipe_data.get("is_public") is False or (
        "slug" in recipe_data and recipe_data["slug"] != recipe.slug
    ):
        raise CanonicalRecipeError(CANONICAL_RECIPE_LOCKED_ERROR)


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
    if (
        current_slug is not None
        and current_slug.strip()
        and "/" not in current_slug
        and "\\" not in current_slug
        and (provided_slug is None or str(provided_slug) == current_slug)
    ):
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


def _pin_source_slug_to_column(
    merged: Dict[str, Any], recipe_data: Dict[str, Any], existing: Recipe
) -> None:
    """Keep ``sourceSlug`` in the merged blob honest when the payload omits it.

    Exactly the rule already applied to ``slug`` above, for the same reason.
    ``source_slug`` is a column mirroring ``data['sourceSlug']``, and writes
    rebuild the blob as ``{**(existing.data or {}), **recipe_data}`` before
    restaging ``recipe.source_slug = data.get("sourceSlug")``. So anything that
    clears the column without also clearing the blob — the KAN-213 migration's
    pre-pass, the guest-merge legacy guard — is silently undone by the next
    partial PUT, which pulls the stale value out of the untouched blob and
    writes it back to the column. That resurrects a cleared duplicate and then
    trips the unique index on a write the user never meant as a save.

    The column is authoritative. Key presence, not truthiness: a payload that
    explicitly sends ``sourceSlug: null`` is the caller clearing it, and that
    value goes through unchanged.

    Found by Codex review on PR #273 — reproduced by
    ``test_clearing_the_column_survives_a_later_partial_update``.
    """
    if "sourceSlug" in recipe_data:
        return
    if existing.source_slug is not None:
        merged["sourceSlug"] = existing.source_slug
    else:
        merged.pop("sourceSlug", None)


def _duplicate_source_slug_owner(
    recipe_data: Dict[str, Any],
    recipe_id: str,
    owner_scope: Optional[Tuple[Optional[int], Optional[str]]],
) -> bool:
    """Did this write lose to an existing row on (owner, source_slug)? (KAN-213)

    Called only after a rollback. Constraint names in the IntegrityError are
    backend-specific (SQLite vs PostgreSQL), so — exactly as with the slug race
    above — the portable signal is to ask the database who owns the key now.

    Scoped to the acting owner on purpose: two different people saving the same
    public recipe is the product working, not a duplicate, and the partial
    indexes are per-owner for that reason.
    """
    if owner_scope is None:
        return False
    # Mirrors the index key COALESCE(source_slug, slug): a row's identity is the
    # public recipe it points at, or its own page when it is the original.
    source = recipe_data.get("sourceSlug")
    identity = source if source is not None else recipe_data.get("slug")
    if identity is None:
        return False  # no identity — outside both partial indexes

    user_id, guest_session_id = owner_scope
    query = Recipe.query.filter(
        func.coalesce(Recipe.source_slug, Recipe.slug) == identity,
        Recipe.id != recipe_id,
    )
    if user_id is not None:
        query = query.filter(Recipe.user_id == user_id)
    elif guest_session_id is not None:
        query = query.filter(Recipe.guest_session_id == guest_session_id)
    else:
        return False  # neither scope set — no partial index covers this row

    return query.first() is not None


def _commit_publish_retrying(
    stage: Callable[[Dict[str, Any]], Recipe],
    recipe_data: Dict[str, Any],
    recipe_id: str,
    current_slug: Optional[str] = None,
    owner_scope: Optional[Tuple[Optional[int], Optional[str]]] = None,
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
            if not lost_slug_race:
                # KAN-213: the (owner, source_slug) index refused a second save
                # of the same public recipe. Distinguish it from a genuine
                # integrity failure BEFORE re-raising, so it can reach the
                # client as a 409 refusal instead of a 500 (R1). Checked only
                # on the non-slug-race path, so the slug retry above is
                # untouched.
                if _duplicate_source_slug_owner(recipe_data, recipe_id, owner_scope):
                    logger.info(
                        "Recipe %s refused: owner already has source_slug %r",
                        sanitize_log_value(recipe_id),
                        sanitize_log_value(recipe_data.get("sourceSlug")),
                    )
                    raise RecipeDuplicateError(RECIPE_DUPLICATE_ERROR)
                raise
            if attempts > _SLUG_COMMIT_RETRIES:
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

    RCP-74: saved copies (source_slug is not None) cannot be published — the
    source page owns publication. Raises SavedCopyPublishError (403 at the API)
    so the SPA's revert-and-toast path fires.
    """
    wants_public = recipe_data.get("is_public") is True
    if wants_public and user_id is None:
        logger.warning(
            "Guest attempted to publish recipe %s — forcing is_public=False",
            sanitize_log_value(recipe_data.get("id", "<no id>")),
        )
        return {**recipe_data, "is_public": False}
    # RCP-74: only authenticated users reach here with wants_public=True.
    # A saved copy must not be re-published — the source page owns publication.
    if wants_public and recipe_data.get("sourceSlug") is not None:
        raise SavedCopyPublishError(SAVED_COPY_PUBLISH_ERROR)
    return {**recipe_data, "is_public": wants_public}


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

            _guard_canonical(existing, recipe_data)

            merged = {**(existing.data or {}), **recipe_data, "id": recipe_id}
            _preserve_worker_metadata(existing.data or {}, merged)
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
            recipe_data_with_id = _gate_is_public(merged, user_id)
            next_origin = _resolve_origin(existing.origin, recipe_data)
            _gate_manual_publish(next_origin, recipe_data_with_id)
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
                existing.origin = next_origin
                existing.data = data
                existing.status = next_status
                existing.updated_at = datetime.utcnow()
                return existing  # type: ignore[no-any-return]

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
        origin_value = _resolve_origin(None, recipe_data)
        _gate_manual_publish(origin_value, recipe_data_with_id)
        if origin_value is not None:
            recipe_data_with_id["origin"] = origin_value
        else:
            recipe_data_with_id.pop("origin", None)

        def stage_new(data: Dict[str, Any]) -> Recipe:
            # KAN-221: set author/saver columns on create.
            source_slug = data.get("sourceSlug")
            if source_slug is not None:
                # Saved copy: look up the source recipe's owner as author.
                source = Recipe.query.filter_by(slug=source_slug).first()
                author_id = source.user_id if source else None
                saved_to_id = user_id
            else:
                # Original recipe: author = owner, no saver.
                author_id = user_id
                saved_to_id = None

            recipe = Recipe(
                id=recipe_id,
                user_id=user_id,
                guest_session_id=None if user_id is not None else guest_session_id,
                name=recipe_name,
                slug=data.get("slug"),
                is_public=data.get("is_public", False),
                source_slug=source_slug,
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

        _guard_canonical(recipe, recipe_data)

        # Merge the payload into the persisted blob (and pin the id): PUT
        # accepts partial payloads such as {"is_public": true}, and replacing
        # the blob wholesale would delete ingredients/instructions at the
        # moment of publishing. Keys the payload does supply always win.
        merged = {**(recipe.data or {}), **recipe_data, "id": recipe_id}
        _preserve_worker_metadata(recipe.data or {}, merged)
        # Never writable through the API — pin the blob to the column.
        merged["is_canonical"] = recipe.is_canonical

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

        _pin_source_slug_to_column(recipe_data_with_id, recipe_data, recipe)

        next_origin = _resolve_origin(recipe.origin, recipe_data)
        _gate_manual_publish(next_origin, recipe_data_with_id)
        if next_origin is not None:
            recipe_data_with_id["origin"] = next_origin
        else:
            recipe_data_with_id.pop("origin", None)

        def stage_update(data: Dict[str, Any]) -> Recipe:
            recipe.name = recipe_data.get("name", recipe.name)
            recipe.slug = data.get("slug")
            recipe.is_public = data["is_public"]
            recipe.source_slug = data.get("sourceSlug")
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

        if recipe.is_canonical:
            logger.warning(
                "Refusing to delete canonical recipe %s (slug=%s)",
                sanitize_log_value(recipe_id),
                sanitize_log_value(recipe.slug),
            )
            raise CanonicalRecipeError(CANONICAL_RECIPE_LOCKED_ERROR)

        db.session.delete(recipe)
        db.session.commit()

        logger.info("Deleted recipe %s", sanitize_log_value(recipe_id))
        return True

    except CanonicalRecipeError:
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
