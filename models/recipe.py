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
    # Partial on `source_slug IS NOT NULL` by design, and narrower than that
    # reads: only `origin='saved'` rows carry a source_slug, so these indexes
    # constrain COPIES A USER TOOK from someone else's public page and do not
    # constrain a single recipe a user authored (~3% of user 1's rows).
    #
    # Still the right corner: a saved copy is the only case where "these two
    # rows are the same recipe" is a machine-checkable fact. Two separately
    # generated recipes have no such identity, and a name-based constraint was
    # rejected — two genuinely different recipes may share a title. See
    # migration c8f3b71d20a4 for the evidence and KAN-220 for why the covered
    # corner is where the real duplicates come from.
    #
    # Declared on the model as well as in migration c8f3b71d20a4 so that
    # `db.create_all()` (tests, fresh local dev) builds the same indexes the
    # migration builds in production. Without this the suite would pass against
    # a schema that cannot refuse anything.
    __table_args__ = (
        Index(
            "uq_recipe_user_source_slug",
            "user_id",
            "source_slug",
            unique=True,
            postgresql_where=text("source_slug IS NOT NULL AND user_id IS NOT NULL"),
            sqlite_where=text("source_slug IS NOT NULL AND user_id IS NOT NULL"),
        ),
        Index(
            "uq_recipe_guest_source_slug",
            "guest_session_id",
            "source_slug",
            unique=True,
            postgresql_where=text("source_slug IS NOT NULL AND guest_session_id IS NOT NULL"),
            sqlite_where=text("source_slug IS NOT NULL AND guest_session_id IS NOT NULL"),
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
    source_slug = db.Column(db.String(255), nullable=True)
    # How the recipe entered the system: 'manual' | 'generated' | 'saved'
    # (NULL = legacy/unknown). Manually entered recipes cannot be published
    # (KAN-140) — free-text content with no AI mediation must not reach the
    # public /r/<slug> surface. Settable while NULL, immutable once set.
    origin = db.Column(db.String(20), nullable=True)
    # MutableDict ensures in-place JSON updates are tracked (important for SQLite dev).
    data = db.Column(MutableDict.as_mutable(GenericJSON), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user = db.relationship("User", backref=db.backref("recipes", lazy=True))

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
            "origin": self.origin,
            "data": self.data,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
