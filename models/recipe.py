from datetime import datetime

from sqlalchemy import JSON as GenericJSON
from sqlalchemy.ext.mutable import MutableDict

from extensions import db


class Recipe(db.Model):  # type: ignore[name-defined, misc]
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
