"""
Async worker claim/status/patch operations, owner/status scoping, and
worker-owned content lock helpers for the recipe repository (KAN-460).

Extracted from ``repositories.db_recipe_repository`` so worker lease
management, optimistic status transitions, and generated-content locking live
in a dedicated module while remaining re-exported from
``repositories.db_recipe_repository``.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, cast

from sqlalchemy import and_, or_

from extensions import db
from models import Recipe
from utils.log_sanitizer import sanitize_log_value

logger = logging.getLogger("repositories.db_recipe_repository")

_WORKER_METADATA_KEYS = ("image_enqueue", "image_request")
_ACTIVE_RECIPE_STATUSES = frozenset({"generating", "processing", "generating_image"})

# KAN-329: 'generated' is written only by update_recipe_for_worker, at the
# moment the model's text lands. A payload may still label its own row
# 'manual' or 'saved'; a client-supplied 'generated' is dropped, because that
# label is what the publish gate trusts.
_CLIENT_ORIGINS = frozenset({"manual", "saved"})

# KAN-328: on a generated row (or a placeholder the worker still owns) a
# client write can change only these keys. Everything else in the payload is
# ignored rather than refused: the SPA echoes the whole recipe on every save,
# so refusing would break the publish toggle on every old bundle.
_LOCKED_CLIENT_FIELDS = frozenset({"id", "is_public", "personalNotes"})

# The statuses between POST /api/generate and the worker's text write.
_TEXT_GENERATION_STATUSES = frozenset({"generating", "processing"})

# KAN-329: the text model's JSON is prompt-mediated, and a prompt can ask it
# to echo an image URL or bytes. Only the image worker may populate media,
# and only the columns may say anything about publication or provenance, so
# these keys are dropped from the text worker's output before it is stamped.
_WORKER_TEXT_DROP_FIELDS = frozenset(
    {
        "ai_image_url",
        "ai_image_data",
        "ai_image_gcs",
        "stock_image_url",
        "is_public",
        "slug",
        "origin",
        "sourceSlug",
        "sourceRecipeId",
        "is_canonical",
        "first_published_at",
        "slug_reserved",
        "personalNotes",
    }
)

# KAN-381: statuses of a row that has no recipe in it. POST /api/generate
# writes a "Generating..." placeholder first; the worker fills it in and sets
# "ready". A placeholder whose generation is still running, or died, is not a
# recipe, so the owner's list and count leave it out. "generating_image" is a
# finished recipe waiting for its picture and stays in.
UNFINISHED_GENERATION_STATUSES = frozenset({"generating", "processing", "error"})


@dataclass(frozen=True)
class WorkerRecipeUpdate:
    user_id: Optional[int]
    guest_session_id: Optional[str]


def _apply_recipe_scope(query, user_id: Optional[int], guest_session_id: Optional[str]):
    """Scope queries to either authenticated user or anonymous session."""
    if user_id is not None:
        return query.filter_by(user_id=user_id)
    if guest_session_id:
        return query.filter_by(user_id=None, guest_session_id=guest_session_id)
    return query.filter_by(user_id=None, guest_session_id=None)


def _listed_recipes(query):
    """Filter out placeholder rows whose initial text generation never completed."""
    return query.filter(Recipe.status.notin_(UNFINISHED_GENERATION_STATUSES))


def _dedup_rows(rows: Iterable[Recipe]) -> List[Recipe]:
    """Deduplicate recipe rows by id (and provenance identity) while preserving order."""
    seen_ids: set[str] = set()
    result: List[Recipe] = []
    for row in rows:
        row_id = getattr(row, "id", None)
        if row_id is not None:
            if row_id in seen_ids:
                continue
            seen_ids.add(row_id)
        result.append(row)
    return result


def _is_content_locked(recipe: Recipe) -> bool:
    """True when a client write may change nothing but the locked-field set.

    A generated row's content is the worker's; a placeholder the worker still
    owns (no origin yet, TEXT generation in flight) must stay empty so the
    stamp never lands on client text. Image generation on an origin-less row
    does not lock it: that row is not generated, cannot be published, and its
    owner may keep editing it while the image worker runs.
    """
    if recipe.origin == "generated":
        return True
    return recipe.origin is None and recipe.status in _TEXT_GENERATION_STATUSES


def _lock_client_payload(recipe: Recipe, recipe_data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop every payload key but the locked-field set on a locked row."""
    if not _is_content_locked(recipe):
        return recipe_data
    dropped = sorted(k for k in recipe_data if k not in _LOCKED_CLIENT_FIELDS)
    if dropped:
        logger.info(
            "Ignored %d client field(s) on locked recipe %s",
            len(dropped),
            sanitize_log_value(recipe.id),
        )
    return {k: v for k, v in recipe_data.items() if k in _LOCKED_CLIENT_FIELDS}


def _resolve_origin(current_origin: Optional[str], recipe_data: Dict[str, Any]) -> Optional[str]:
    """Column value for origin: settable while NULL, immutable once set.

    Once a recipe is labeled, a later payload cannot relabel it — otherwise
    a 'manual' recipe could launder itself into 'generated' and slip past
    the publish gate. Unknown labels are dropped rather than stored.
    """
    if current_origin:
        return current_origin
    candidate = recipe_data.get("origin")
    return candidate if candidate in _CLIENT_ORIGINS else None


def _preserve_worker_metadata(current_data: Dict[str, Any], merged: Dict[str, Any]) -> None:
    """Carry forward worker-owned metadata keys across client updates."""
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
    # KAN-329: this write is what makes a recipe 'generated', so the blob is
    # REPLACED, not merged — no public content a client managed to put in the
    # placeholder survives — and the model's output is projected away from
    # media, publication and provenance keys before the stamp lands. The one
    # client-owned private field, personalNotes, is carried forward explicitly.
    # Ownership and visibility come from the row's columns, never from either
    # payload.
    existing_data = recipe.data if isinstance(recipe.data, dict) else {}
    merged = {k: v for k, v in recipe_data.items() if k not in _WORKER_TEXT_DROP_FIELDS}
    if "personalNotes" in existing_data:
        merged["personalNotes"] = existing_data["personalNotes"]
    merged["id"] = recipe_id
    merged["user_id"] = recipe.user_id
    merged["is_public"] = recipe.is_public
    merged["origin"] = "generated"
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
                "origin": "generated",
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


class RecipeWorkerOpsMixin:
    """Mixin exposing async worker claim/status/patch operations on the repository."""

    get_recipe_for_worker = staticmethod(get_recipe_for_worker)
    claim_recipe_for_worker = staticmethod(claim_recipe_for_worker)
    set_recipe_status_for_worker = staticmethod(set_recipe_status_for_worker)
    update_recipe_for_worker = staticmethod(update_recipe_for_worker)
    patch_recipe_for_worker = staticmethod(patch_recipe_for_worker)
    update_recipe_status = staticmethod(update_recipe_status)
    dedup_rows = staticmethod(_dedup_rows)


__all__ = [
    "_WORKER_METADATA_KEYS",
    "_ACTIVE_RECIPE_STATUSES",
    "_CLIENT_ORIGINS",
    "_LOCKED_CLIENT_FIELDS",
    "_TEXT_GENERATION_STATUSES",
    "_WORKER_TEXT_DROP_FIELDS",
    "UNFINISHED_GENERATION_STATUSES",
    "WorkerRecipeUpdate",
    "RecipeWorkerOpsMixin",
    "_apply_recipe_scope",
    "_listed_recipes",
    "_dedup_rows",
    "_is_content_locked",
    "_lock_client_payload",
    "_resolve_origin",
    "_preserve_worker_metadata",
    "get_recipe_for_worker",
    "claim_recipe_for_worker",
    "set_recipe_status_for_worker",
    "update_recipe_for_worker",
    "patch_recipe_for_worker",
    "update_recipe_status",
]
