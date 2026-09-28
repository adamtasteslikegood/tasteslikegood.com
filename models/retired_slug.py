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
  slug back; while it lives under a new one, ``/r/<old>`` temporarily redirects.

The retirement is written by a ``before_flush`` hook rather than at each call
site, so every write path (API delete, guest-merge cleanup, update/upsert
restaging, scripts) is covered without having to remember it.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session

from extensions import db

from .recipe import Recipe


class RetiredSlug(db.Model):  # type: ignore[name-defined, misc]
    __tablename__ = "retired_slug"

    slug = db.Column(db.String(255), primary_key=True)
    # No FK: the row this points at is usually gone. NULL = deleted, permanent.
    recipe_id = db.Column(db.String(36), nullable=True, index=True)
    retired_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def lock_slug(session: Session, slug: str) -> None:
    """Serialize live/retired ownership changes for ``slug`` on PostgreSQL.

    ``recipe.slug`` and ``retired_slug.slug`` live in separate tables, so a
    database uniqueness constraint cannot span both. A transaction-scoped
    advisory lock closes the rename/delete versus publish race without leaving
    locks behind after commit or rollback. SQLite already serializes writers.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:slug, 0))"),
            {"slug": slug},
        )


class RetiredSlugTakenError(ValueError):
    """A write tried to give a recipe a slug retired against a different recipe."""


def _guard_new_slug(session: Session, obj: Recipe) -> None:
    """Refuse a newly assigned slug that is retired against another recipe.

    The repository resolvers never pick such a slug; this backstops every
    other writer (scripts such as ``backfill_slugs.py``, ad-hoc ORM code), so
    a tombstoned /r/<slug> can never start serving a different recipe.
    """
    lock_slug(session, obj.slug)
    retired = session.get(RetiredSlug, obj.slug)
    if retired is not None and retired.recipe_id != obj.id:
        raise RetiredSlugTakenError(f"slug {obj.slug!r} is retired and cannot be reassigned")


def _retire(session: Session, slug: str, recipe_id: Optional[str]) -> None:
    lock_slug(session, slug)
    existing = session.get(RetiredSlug, slug)
    if existing is None:
        session.add(RetiredSlug(slug=slug, recipe_id=recipe_id, retired_at=datetime.utcnow()))
    elif recipe_id is None or existing.recipe_id is not None:
        # A delete makes any retirement permanent; a permanent one never softens.
        existing.recipe_id = recipe_id


def _retire_published_slugs(session: Session, _flush_context: Any, _instances: Any) -> None:
    # Historical data migrations run against schemas that predate this model's
    # column/table. They opt out explicitly so this global listener cannot lazy
    # load ``first_published_at`` before the column exists.
    if session.info.get("skip_retired_slug_listener"):
        return

    with session.no_autoflush:
        for obj in list(session.new):
            if not isinstance(obj, Recipe):
                continue
            if obj.slug:
                _guard_new_slug(session, obj)
            if obj.is_public and obj.first_published_at is None:
                obj.first_published_at = datetime.utcnow()

        for obj in list(session.dirty):
            if not isinstance(obj, Recipe):
                continue
            state: Any = inspect(obj)
            is_becoming_public = bool(obj.is_public and state.attrs.is_public.history.has_changes())
            if obj.slug and (state.attrs.slug.history.added or is_becoming_public):
                _guard_new_slug(session, obj)
            was_published = obj.first_published_at is not None
            if obj.is_public and not was_published:
                obj.first_published_at = datetime.utcnow()
            if not was_published:
                continue
            old_slugs: tuple[Optional[str], ...] = tuple(state.attrs.slug.history.deleted or ())
            for old_slug in old_slugs:
                if old_slug and old_slug != obj.slug:
                    _retire(session, old_slug, obj.id)

        for obj in list(session.deleted):
            if not isinstance(obj, Recipe):
                continue
            # Deleting a recipe makes every URL it ever served permanent, not
            # just its current slug. Otherwise a same-id restore could reclaim
            # an older rename alias because owned retirements are reclaimable.
            # Migration-created owner markers also protect ambiguous legacy
            # private slugs without falsely setting ``first_published_at``.
            owned_retirements = list(
                session.query(RetiredSlug).filter(RetiredSlug.recipe_id == obj.id)
            )
            if obj.first_published_at is None and not owned_retirements:
                continue
            for retired in owned_retirements:
                lock_slug(session, retired.slug)
                retired.recipe_id = None
            if obj.first_published_at is None:
                continue
            deleted_slugs: tuple[Optional[str], ...] = tuple(
                inspect(obj).attrs.slug.history.deleted or ()
            )
            for deleted_slug in deleted_slugs:
                if deleted_slug:
                    _retire(session, deleted_slug, None)
            # If the slug changed immediately before deletion, SQLAlchemy
            # cancels that UPDATE and only the persisted value in
            # ``history.deleted`` ever served a public URL. Do not tombstone
            # the transient replacement as though it had gone live.
            if obj.slug and not deleted_slugs:
                _retire(session, obj.slug, None)


if not event.contains(Session, "before_flush", _retire_published_slugs):
    event.listen(Session, "before_flush", _retire_published_slugs)
