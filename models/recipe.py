from datetime import datetime

from sqlalchemy import JSON as GenericJSON
from sqlalchemy import Index, text
from sqlalchemy.ext.mutable import MutableDict

from extensions import db


class Recipe(db.Model):  # type: ignore[name-defined, misc]
    # KAN-213 — the duplicate invariant lives here, not in the SPA.
    #
    # Exactly one of (user_id, guest_session_id) is non-NULL on a row, so two
    # partial indexes are the right shape rather than one composite constraint
    # — the same reasoning as cookbook's uq_cookbook_* pair.
    #
    # Partial on `COALESCE(source_slug, slug) IS NOT NULL`, but narrower than
    # that reads: only `origin='saved'` rows carry a source_slug, and `slug`
    # is already globally unique, so these indexes constrain COPIES A USER TOOK
    # from someone else's public page and do not constrain a single recipe a
    # user authored (~3% of user 1's rows).
    #
    # Still the right corner: a saved copy is the only case where "these two
    # rows are the same recipe" is a machine-checkable fact. Two separately
    # generated recipes have no such identity, and a name-based constraint was
    # rejected — two genuinely different recipes may share a title. See
    # migration c8f3b71d20a4 for the evidence and KAN-220 for why the covered
    # corner is where the real duplicates come from.
    #
    # Declared on the model as well as in migration a3c9e1f4b7d2 so that
    # `db.create_all()` (tests, fresh local dev) builds the same indexes the
    # migration builds in production. Without this the suite would pass against
    # a schema that cannot refuse anything.
    #
    # KAN-221 re-key: the provenance key moved from the mutable slug string to
    # the stable source row id. Reading the COALESCE:
    #
    #   source_recipe_id  a saved copy whose source resolved at save time (or
    #                     via the a3c9e1f4b7d2 backfill) keys on the source's
    #                     immutable id, so it collides with the source row
    #                     itself and with every other resolved copy of it under
    #                     the same owner — regardless of slug renames.
    #   source_slug       a copy whose source never resolved (raw/legacy rows,
    #                     source gone before backfill) falls back to the slug
    #                     pointer, preserving KAN-213's dedup between such
    #                     copies.
    #   id                every other row keys on its own primary key. This is
    #                     what makes the source row collide with a resolved
    #                     copy of it (copy.source_recipe_id == source.id) —
    #                     the Codex #273 identity case — without the slug
    #                     aliasing KAN-221 retires. Between originals it is
    #                     vacuously unique, so the constraint still only ever
    #                     refuses pairs involving a saved copy.
    #
    # Scope: for saved copies user_id == user_id_saved_to by construction
    # (create sets both to the saver; login-merge sets both to the new user),
    # so scoping on user_id IS the locked (user_id_saved_to, source_recipe_id)
    # constraint on the rows it covers, while non-copy rows keep their
    # identity semantics under the same pair of indexes.
    _IDENTITY = "coalesce(source_recipe_id, source_slug, id)"

    __table_args__ = (
        Index(
            "uq_recipe_user_recipe_identity",
            text("user_id"),
            text(_IDENTITY),
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
            sqlite_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_recipe_guest_recipe_identity",
            text("guest_session_id"),
            text(_IDENTITY),
            unique=True,
            postgresql_where=text("guest_session_id IS NOT NULL"),
            sqlite_where=text("guest_session_id IS NOT NULL"),
        ),
    )

    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    guest_session_id = db.Column(db.String(64), nullable=True, index=True)
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="ready")
    worker_claim_token = db.Column(db.String(36), nullable=True)
    slug = db.Column(db.String(255), unique=True, index=True, nullable=True)
    is_public = db.Column(db.Boolean, default=False, nullable=False, server_default="0")
    # Canonical recipes (curated in the cookbook repo's specs/canonical-recipes.json)
    # are locked: publish state, slug, and the row itself cannot be changed via the
    # API — only content edits. Never writable through the API; seeded by migration.
    is_canonical = db.Column(db.Boolean, default=False, nullable=False, server_default="0")
    # Slug of the public recipe this row was saved from (the blob's sourceSlug,
    # mirrored to a column so publish-state checks don't need to parse JSON).
    # Kept after KAN-221: it still builds the /r/<slug> link — only the
    # identity KEY moved to source_recipe_id below.
    source_slug = db.Column(db.String(255), nullable=True)
    # KAN-221: id of the source recipe this row was saved from — the stable,
    # immutable provenance key. Resolved server-side at save time (never
    # client-writable) and backfilled from source_slug by migration
    # a3c9e1f4b7d2. ON DELETE SET NULL: deleting the source clears the pointer,
    # while source_slug stays, so the copy remains a saved copy (and remains
    # unpublishable) after the source is gone.
    source_recipe_id = db.Column(
        db.String(36),
        db.ForeignKey("recipe.id", name="fk_recipe_source_recipe_id", ondelete="SET NULL"),
        nullable=True,
    )
    # How the recipe entered the system: 'manual' | 'generated' | 'saved'
    # (NULL = legacy/unknown). Manually entered recipes cannot be published
    # (KAN-140) — free-text content with no AI mediation must not reach the
    # public /r/<slug> surface. Settable while NULL, immutable once set.
    origin = db.Column(db.String(20), nullable=True)
    # KAN-221: split user_id into explicit author and saver identities.
    # user_id_author = the user who created/generated the recipe (immutable).
    # user_id_saved_to = the user who saved a copy from a public page (NULL
    # for originals). Existing user_id retains its meaning ("the account that
    # holds this row") — these columns add precision without removing anything.
    user_id_author = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    user_id_saved_to = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    # MutableDict ensures in-place JSON updates are tracked (important for SQLite dev).
    data = db.Column(MutableDict.as_mutable(GenericJSON), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user = db.relationship("User", foreign_keys=[user_id], backref=db.backref("recipes", lazy=True))

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "guest_session_id": self.guest_session_id,
            "name": self.name,
            "status": self.status,
            "slug": self.slug,
            "is_public": self.is_public,
            "is_canonical": self.is_canonical,
            "source_slug": self.source_slug,
            "source_recipe_id": self.source_recipe_id,
            "origin": self.origin,
            "user_id_author": self.user_id_author,
            "user_id_saved_to": self.user_id_saved_to,
            "data": self.data,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
