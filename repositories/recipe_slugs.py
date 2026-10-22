"""
Public slug resolution, collision retry, retired-slug reclamation, and publish
gates for the database-backed recipe repository (KAN-460).

Extracted from ``repositories.db_recipe_repository`` so slug uniqueness,
retirement tombstones, source-slug provenance pinning, and publication guards
live in a focused domain module while remaining re-exported from
``repositories.db_recipe_repository``.
"""

import logging
import sys
from typing import Any, Callable, Dict, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Recipe, RetiredSlug
from models.retired_slug import lock_slug
from services.recipe_metadata import queue_if_missing
from utils.log_sanitizer import sanitize_log_value
from utils.slug_utils import normalize_slug

logger = logging.getLogger("repositories.db_recipe_repository")

# Matches models/recipe.py: slug = db.Column(db.String(255), ...)
_SLUG_MAX_LENGTH = 255
# Commit retries when a concurrent publication wins the slug race.
_SLUG_COMMIT_RETRIES = 3

# Also returned verbatim by the API routes (a fixed string, so no exception
# internals can leak into responses — CodeQL py/stack-trace-exposure).
PUBLIC_SLUG_REQUIRED_ERROR = (
    "A public recipe needs a slug, and neither the provided slug nor "
    "the recipe name yields a usable one."
)

# Also returned verbatim by the API routes (fixed string, same rationale as
# PUBLIC_SLUG_REQUIRED_ERROR above).
CANONICAL_RECIPE_LOCKED_ERROR = (
    "This is a canonical public recipe: its publish state, slug, and row are "
    "locked. Content edits are still allowed."
)

# Also returned verbatim by the API routes (fixed string, same rationale as
# PUBLIC_SLUG_REQUIRED_ERROR above).
PUBLISHED_RECIPE_DELETE_ERROR = (
    "This recipe is published. Unpublish it before deleting it: deleting "
    "permanently retires its public page."
)

# Also returned verbatim by the API routes (fixed string, same rationale as
# PUBLIC_SLUG_REQUIRED_ERROR above).
MANUAL_RECIPE_UNPUBLISHABLE_ERROR = (
    "Only recipes generated here can have a public page. Manually entered, "
    "imported or still-generating recipes cannot be published."
)

# KAN-329: only these states prove the text worker finished. An allowlist keeps
# a typo or a future workflow state from silently becoming publishable. The
# cutover audit imports this so its eligibility rule cannot drift from the API.
PUBLISHABLE_RECIPE_STATUSES = frozenset({"ready", "generating_image"})

# RCP-74: saved copies inherit their public page from the source recipe.
SAVED_COPY_PUBLISH_ERROR = "Cannot publish a saved copy."

# Also returned verbatim by the API routes (fixed string, same rationale as
# PUBLIC_SLUG_REQUIRED_ERROR above).
RECIPE_DUPLICATE_ERROR = "You already have this recipe saved."

# Machine-readable discriminator, sent alongside the message as `code` — same
# contract as the OWNERSHIP_CODE_* constants. A duplicate is a *successful*
# outcome from the user's point of view (the recipe they wanted is in their
# cookbook), which is the opposite of the ownership refusals, so the SPA needs
# to tell them apart to choose between an error toast and a benign one.
DUPLICATE_CODE_ALREADY_SAVED = "RECIPE_ALREADY_SAVED"


class PublishGateError(ValueError):
    """Base error for recipe publication and canonical-lock gate refusals."""


class RecipeConflictError(ValueError):
    """Base error for recipe ownership, duplicate-save, and delete conflicts."""


class RecipeSlugError(PublishGateError):
    """A recipe cannot be published without a usable /r/<slug> address."""


class CanonicalRecipeError(PublishGateError):
    """Publish-state, slug, or delete changes to a canonical recipe are locked."""


class PublishedRecipeDeleteError(RecipeConflictError):
    """KAN-288: a published recipe cannot be deleted until it is unpublished."""


class ManualRecipeError(PublishGateError):
    """Manually entered recipes cannot be published (KAN-140)."""


class SavedCopyPublishError(PublishGateError):
    """Saved copies cannot be published — the source page owns publication (RCP-74)."""


class RecipeDuplicateError(RecipeConflictError):
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


def _get_repo_attr(name: str, default: Any) -> Any:
    """Resolve attributes through ``repositories.db_recipe_repository`` when loaded.

    Allows unit tests that ``monkeypatch.setattr(db_recipe_repository, ...)``
    (such as ``_resolve_public_slug`` or ``queue_if_missing``) to intercept
    internal calls made inside extracted helpers.
    """
    mod = sys.modules.get("repositories.db_recipe_repository")
    return getattr(mod, name, default) if mod is not None else default


def _gate_publish_transition(existing: Recipe, recipe_data: Dict[str, Any]) -> None:
    """Only a finished, worker-generated row may go private → public (KAN-329).

    Generalises the KAN-140 manual gate: the label a client wrote never
    counts, only the origin the worker stamped and a status that says the
    text landed. The check is on the TRANSITION alone — a row that is
    already public may be re-saved as public (image generation moves it
    through ``generating_image`` without unpublishing it), and unpublishing
    is always allowed. Raises (a 400 at the API) instead of silently forcing
    the flag off, so the SPA's revert-and-toast path fires rather than
    diverging from the server state.
    """
    if recipe_data.get("is_public") is not True or existing.is_public:
        return
    if existing.origin == "generated" and existing.status in PUBLISHABLE_RECIPE_STATUSES:
        return
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


def _slugify(text: str) -> str:
    """Normalize text to a route-safe slug.

    Same normalization as scripts/backfill_slugs.py:generate_slug, except
    that this returns "" for unusable input (the publish gate rejects it
    with RecipeSlugError) where the backfill falls back to "recipe".
    """
    return normalize_slug(text)


def _is_saved_copy(recipe: Recipe) -> bool:
    """Whether a persisted row is a saved copy of another recipe."""
    return bool(recipe.source_slug or recipe.source_recipe_id)


def _slug_retired_against(slug: str, recipe_id: str) -> bool:
    """Whether ``slug`` was retired by any recipe other than ``recipe_id`` (KAN-288).

    A deleted recipe's retirement (``recipe_id`` NULL) blocks everyone,
    including a restore that re-POSTs the same id.
    """
    retired = db.session.get(RetiredSlug, slug)
    return retired is not None and retired.recipe_id != recipe_id


def _slug_occupied_against(slug: str, recipe_id: str) -> bool:
    """Recheck both live and retired ownership after locking ``slug``."""
    live = Recipe.query.filter(Recipe.slug == slug, Recipe.id != recipe_id).first()
    retired_fn = _get_repo_attr("_slug_retired_against", _slug_retired_against)
    return live is not None or bool(retired_fn(slug, recipe_id))


def _clear_unowned_retired_slug_from_private(recipe_data: Dict[str, Any], recipe_id: str) -> None:
    """Do not let a private row occupy another recipe's retired alias.

    Private slugs are not public URLs and may be payload-provided, but the
    Recipe.slug uniqueness constraint would still prevent the retirement owner
    from reclaiming its alias. Clear only slugs retired against this row; the
    same recipe remains free to stage one of its own rename aliases.
    """
    slug = recipe_data.get("slug")
    lock_fn = _get_repo_attr("lock_slug", lock_slug)
    retired_fn = _get_repo_attr("_slug_retired_against", _slug_retired_against)
    if recipe_data.get("is_public") is False and slug:
        # Private rows share Recipe.slug's global uniqueness and can otherwise
        # win the same release-versus-retirement race as a publisher.
        lock_fn(db.session(), str(slug))
    if recipe_data.get("is_public") is False and slug and retired_fn(str(slug), recipe_id):
        recipe_data["slug"] = None


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
    lock_fn = _get_repo_attr("lock_slug", lock_slug)
    occupied_fn = _get_repo_attr("_slug_occupied_against", _slug_occupied_against)
    slugify_fn = _get_repo_attr("_slugify", _slugify)

    provided_slug = recipe_data.get("slug")
    if (
        current_slug is not None
        and current_slug.strip()
        and "/" not in current_slug
        and "\\" not in current_slug
        and (provided_slug is None or str(provided_slug) == current_slug)
        # KAN-288: a private row can hold any payload slug unvalidated, so the
        # slug it carries into publication may be one another recipe retired.
    ):
        lock_fn(db.session(), current_slug)
        if not occupied_fn(current_slug, recipe_id):
            return current_slug

    for source in (recipe_data.get("slug"), current_slug, recipe_data.get("name")):
        candidate = slugify_fn(str(source)) if source else ""
        if candidate:
            break
    else:
        raise RecipeSlugError(PUBLIC_SLUG_REQUIRED_ERROR)

    base = candidate[:_SLUG_MAX_LENGTH].rstrip("-")
    # Short enough that every truncated-for-suffix variant still matches.
    # _slugify output has no LIKE metacharacters (%, _).
    prefix = base[: _SLUG_MAX_LENGTH - 12].rstrip("-")
    occupied = (
        set(skip)
        | {
            slug
            for (slug,) in db.session.query(Recipe.slug).filter(
                Recipe.id != recipe_id, Recipe.slug.like(f"{prefix}%")
            )
        }
        # KAN-288: a slug that once served a published page is never reassigned.
        | {
            slug
            for (slug,) in db.session.query(RetiredSlug.slug).filter(
                RetiredSlug.slug.like(f"{prefix}%"),
                or_(RetiredSlug.recipe_id.is_(None), RetiredSlug.recipe_id != recipe_id),
            )
        }
    )
    candidate, suffix = base, 1
    while True:
        while candidate in occupied:
            suffix += 1
            tail = f"-{suffix}"
            candidate = f"{base[: _SLUG_MAX_LENGTH - len(tail)].rstrip('-')}{tail}"

        # The prefix probes above are intentionally optimistic. Serialize the
        # final choice with retirement, then recheck exact ownership while the
        # transaction holds the lock. This prevents a concurrent rename/delete
        # from releasing Recipe.slug just before its tombstone becomes visible.
        lock_fn(db.session(), candidate)
        if not occupied_fn(candidate, recipe_id):
            return str(candidate)
        occupied.add(candidate)


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

    The column is authoritative — in BOTH directions. When the payload omits
    the key, the column value is restored into the blob (the Codex #273 case,
    reproduced by ``test_clearing_the_column_survives_a_later_partial_update``).
    And when the payload sends ANY value (null or non-null) against a row
    whose column is already non-NULL, the persisted value wins: provenance
    is NOT client-mutable once set.
    """
    if "sourceSlug" in recipe_data:
        if existing.source_slug is not None:
            merged["sourceSlug"] = existing.source_slug
        return
    if existing.source_slug is not None:
        merged["sourceSlug"] = existing.source_slug
    else:
        merged.pop("sourceSlug", None)


def _resolve_source_recipe_id(
    current: Optional[str], source_slug: Optional[str], recipe_id: str
) -> Optional[str]:
    """Column value for source_recipe_id: resolved while NULL, immutable once set.

    KAN-221: the stable provenance key. Set from the source row whose slug the
    copy points at, exactly once — a later payload cannot re-point a copy at a
    different source (the same immutability rule as ``origin``), and no client
    value ever reaches this column.
    """
    if current is not None:
        return current
    if source_slug is None:
        return None
    # no_autoflush: this runs inside the stage functions with the row already
    # dirty — a flush here would hit the unique indexes mid-stage, outside
    # _commit_publish_retrying's IntegrityError handling. The lookup needs no
    # pending state; it resolves against committed rows only.
    with db.session.no_autoflush:
        source = Recipe.query.filter(
            Recipe.slug == source_slug,
            Recipe.is_public.is_(True),
            Recipe.id != recipe_id,
        ).first()
    return source.id if source is not None else None


def _duplicate_source_slug_owner(
    recipe_data: Dict[str, Any],
    recipe_id: str,
    owner_scope: Optional[Tuple[Optional[int], Optional[str]]],
) -> bool:
    """Did this write lose to an existing row on (owner, source_slug)? (KAN-213)

    Called only after a rollback. Constraint names in the IntegrityError are
    backend-specific (SQLite vs PostgreSQL), so — exactly as with the slug race
    above — the portable signal is to ask the database who owns the key now.
    """
    if owner_scope is None:
        return False
    # Mirrors the index key COALESCE(source_recipe_id, source_slug, id)
    # (KAN-221). Only a write that carries a source pointer can lose to the
    # index: rows without one key on their own unique id, which cannot collide.
    source = recipe_data.get("sourceSlug")
    if source is None:
        return False
    source_row = Recipe.query.filter(
        Recipe.slug == source, Recipe.is_public.is_(True), Recipe.id != recipe_id
    ).first()
    identity = source_row.id if source_row is not None else source

    user_id, guest_session_id = owner_scope
    query = Recipe.query.filter(
        func.coalesce(Recipe.source_recipe_id, Recipe.source_slug, Recipe.id) == identity,
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
    """
    resolver_input = dict(recipe_data)
    skip: set = set()
    attempts = 0
    while True:
        resolve_fn = _get_repo_attr("_resolve_public_slug", _resolve_public_slug)
        clear_fn = _get_repo_attr(
            "_clear_unowned_retired_slug_from_private",
            _clear_unowned_retired_slug_from_private,
        )
        dup_fn = _get_repo_attr("_duplicate_source_slug_owner", _duplicate_source_slug_owner)
        queue_fn = _get_repo_attr("queue_if_missing", queue_if_missing)

        if recipe_data["is_public"]:
            recipe_data["slug"] = resolve_fn(
                resolver_input, recipe_id, current_slug, skip=frozenset(skip)
            )
        else:
            clear_fn(recipe_data, recipe_id)
        recipe = stage(recipe_data)
        try:
            db.session.commit()
            # KAN-388: a public recipe with no current search metadata gets a build.
            queue_fn(recipe)
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
                # client as a 409 refusal instead of a 500 (R1).
                if dup_fn(recipe_data, recipe_id, owner_scope):
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


def _gate_is_public(
    recipe_data: Dict[str, Any],
    user_id: Optional[int],
    existing: Optional[Recipe] = None,
) -> Dict[str, Any]:
    """Only authenticated users may publish: guests get is_public forced False.

    RCP-74 / KAN-221: saved copies cannot be published — the source page owns
    publication, permanently, with no author exception.
    """
    wants_public = recipe_data.get("is_public") is True
    if wants_public and user_id is None:
        logger.warning(
            "Guest attempted to publish recipe %s — forcing is_public=False",
            sanitize_log_value(recipe_data.get("id", "<no id>")),
        )
        return {**recipe_data, "is_public": False}
    # RCP-74: only authenticated users reach here with wants_public=True.
    if wants_public:
        is_saved_copy = recipe_data.get("sourceSlug") is not None
        if existing is not None:
            is_saved_copy = (
                is_saved_copy
                or existing.source_slug is not None
                or existing.source_recipe_id is not None
            )
        if is_saved_copy:
            raise SavedCopyPublishError(SAVED_COPY_PUBLISH_ERROR)
    return {**recipe_data, "is_public": wants_public}


class RetiredSlugMixin:
    """Mixin providing slug resolution, retired-slug checks, and publish gates."""

    slugify = staticmethod(_slugify)
    is_saved_copy = staticmethod(_is_saved_copy)
    slug_retired_against = staticmethod(_slug_retired_against)
    slug_occupied_against = staticmethod(_slug_occupied_against)
    clear_unowned_retired_slug_from_private = staticmethod(_clear_unowned_retired_slug_from_private)
    resolve_public_slug = staticmethod(_resolve_public_slug)
    pin_source_slug_to_column = staticmethod(_pin_source_slug_to_column)
    resolve_source_recipe_id = staticmethod(_resolve_source_recipe_id)
    duplicate_source_slug_owner = staticmethod(_duplicate_source_slug_owner)
    commit_publish_retrying = staticmethod(_commit_publish_retrying)
    gate_publish_transition = staticmethod(_gate_publish_transition)
    guard_canonical = staticmethod(_guard_canonical)
    gate_is_public = staticmethod(_gate_is_public)


__all__ = [
    "_SLUG_MAX_LENGTH",
    "_SLUG_COMMIT_RETRIES",
    "PUBLIC_SLUG_REQUIRED_ERROR",
    "CANONICAL_RECIPE_LOCKED_ERROR",
    "PUBLISHED_RECIPE_DELETE_ERROR",
    "MANUAL_RECIPE_UNPUBLISHABLE_ERROR",
    "PUBLISHABLE_RECIPE_STATUSES",
    "SAVED_COPY_PUBLISH_ERROR",
    "RECIPE_DUPLICATE_ERROR",
    "DUPLICATE_CODE_ALREADY_SAVED",
    "PublishGateError",
    "RecipeConflictError",
    "RecipeSlugError",
    "CanonicalRecipeError",
    "PublishedRecipeDeleteError",
    "ManualRecipeError",
    "SavedCopyPublishError",
    "RecipeDuplicateError",
    "RetiredSlugMixin",
    "_gate_publish_transition",
    "_guard_canonical",
    "_slugify",
    "_is_saved_copy",
    "_slug_retired_against",
    "_slug_occupied_against",
    "_clear_unowned_retired_slug_from_private",
    "_resolve_public_slug",
    "_pin_source_slug_to_column",
    "_resolve_source_recipe_id",
    "_duplicate_source_slug_owner",
    "_commit_publish_retrying",
    "_gate_is_public",
]
