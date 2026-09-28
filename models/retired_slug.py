"""Slugs that once served a published /r/<slug> page and can never be reassigned.

KAN-288 (Adam, 2026-09-28): a recipe's public URL is never taken over by a
different recipe. Before this table a deleted row simply freed its slug, and
``_resolve_public_slug`` handed it to the next recipe with the same name, so
an indexed URL, its backlinks and every saved copy's ``source_slug`` silently
started pointing at someone else's recipe.

A slug is retired when a row that has ever been published (``first_published_at``
set) gives it up:

- **deleted** -> ``recipe_id`` NULL. Permanent: nobody reclaims it, not even a
  restore that re-POSTs the same recipe id. ``/r/<slug>`` answers 410 Gone.
- **renamed** -> ``recipe_id`` = the row's id. The same recipe may take its old
  slug back; while it lives under a new one, ``/r/<old>`` 301s there.

The retirement is written by a ``before_flush`` hook rather than at each call
site, so every write path (API delete, guest-merge cleanup, update/upsert
restaging, scripts) is covered without having to remember it.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from extensions import db

from .recipe import Recipe


class RetiredSlug(db.Model):  # type: ignore[name-defined, misc]
    __tablename__ = "retired_slug"

    slug = db.Column(db.String(255), primary_key=True)
    # No FK: the row this points at is usually gone. NULL = deleted, permanent.
    recipe_id = db.Column(db.String(36), nullable=True)
    retired_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def _retire(session: Session, slug: str, recipe_id: Optional[str]) -> None:
    existing = session.get(RetiredSlug, slug)
    if existing is None:
        session.add(RetiredSlug(slug=slug, recipe_id=recipe_id, retired_at=datetime.utcnow()))
    elif recipe_id is None or existing.recipe_id is not None:
        # A delete makes any retirement permanent; a permanent one never softens.
        existing.recipe_id = recipe_id


def _retire_published_slugs(session: Session, _flush_context: Any, _instances: Any) -> None:
    with session.no_autoflush:
        for obj in list(session.new):
            if isinstance(obj, Recipe) and obj.is_public and obj.first_published_at is None:
                obj.first_published_at = datetime.utcnow()

        for obj in list(session.dirty):
            if not isinstance(obj, Recipe):
                continue
            was_published = obj.first_published_at is not None
            if obj.is_public and not was_published:
                obj.first_published_at = datetime.utcnow()
            if not was_published:
                continue
            old_slugs: tuple[Optional[str], ...] = tuple(
                inspect(obj).attrs.slug.history.deleted or ()
            )
            for old_slug in old_slugs:
                if old_slug and old_slug != obj.slug:
                    _retire(session, old_slug, obj.id)

        for obj in list(session.deleted):
            if not isinstance(obj, Recipe) or obj.first_published_at is None:
                continue
            # Deleting a recipe makes every URL it ever served permanent, not
            # just its current slug. Otherwise a same-id restore could reclaim
            # an older rename alias because owned retirements are reclaimable.
            for retired in session.query(RetiredSlug).filter(RetiredSlug.recipe_id == obj.id):
                retired.recipe_id = None
            deleted_slugs: tuple[Optional[str], ...] = tuple(
                inspect(obj).attrs.slug.history.deleted or ()
            )
            for deleted_slug in deleted_slugs:
                if deleted_slug:
                    _retire(session, deleted_slug, None)
            if obj.slug:
                _retire(session, obj.slug, None)


if not event.contains(Session, "before_flush", _retire_published_slugs):
    event.listen(Session, "before_flush", _retire_published_slugs)


def retired_slug_owner(slug: str) -> tuple[bool, Optional[str]]:
    """``(retired, recipe_id)`` for ``slug``; ``recipe_id`` is None once deleted."""
    row = db.session.get(RetiredSlug, slug)
    return (row is not None, row.recipe_id if row is not None else None)
