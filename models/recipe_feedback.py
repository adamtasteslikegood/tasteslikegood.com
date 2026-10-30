"""Recipe feedback entries (KAN-356, design ``docs/designs/recipe-feedback.md``).

One row is one entry: "I cooked this", a required 1 to 5 star rating and an
optional public note. It is keyed to the PUBLIC recipe's id, never to the
cook's saved copy or to the author's account, so a later change of authorship
cannot orphan it (design, "Planned after Phase 1 is live").

Rows are never deleted by the cook: "Delete feedback" stamps ``deleted_at`` and
the row stays for the moderator (R7). Only one row per (recipe, user) may be
active, enforced by the partial unique index below (Q2).
"""

from datetime import datetime

from sqlalchemy import text

from extensions import db

NOTE_MAX_LENGTH = 1000
STARS_MIN = 1
STARS_MAX = 5

# ``note_status`` values. ``None`` means the entry has no note.
NOTE_HELD = "held"  # waiting for a moderator
NOTE_APPROVED = "approved"  # a moderator approved it
NOTE_PUBLISHED = "published"  # from a trusted account; published without review
NOTE_REJECTED = "rejected"
NOTE_HIDDEN = "hidden"  # a moderator hid it after it was public
PUBLIC_NOTE_STATUSES = (NOTE_APPROVED, NOTE_PUBLISHED)

_ACTIVE = text("deleted_at IS NULL")


class RecipeFeedback(db.Model):  # type: ignore[name-defined, misc]
    __tablename__ = "recipe_feedback"
    __table_args__ = (
        db.Index(
            "uq_recipe_feedback_active",
            "recipe_id",
            "user_id",
            unique=True,
            sqlite_where=_ACTIVE,
            postgresql_where=_ACTIVE,
        ),
        db.CheckConstraint("stars BETWEEN 1 AND 5", name="ck_recipe_feedback_stars"),
    )

    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(
        db.String(36),
        db.ForeignKey("recipe.id", name="fk_recipe_feedback_recipe_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", name="fk_recipe_feedback_user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stars = db.Column(db.SmallInteger, nullable=False)
    note = db.Column(db.Text, nullable=True)
    note_status = db.Column(db.String(16), nullable=True)
    note_reviewed_at = db.Column(db.DateTime, nullable=True)
    entry_hidden = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    hidden_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    deleted_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])
    recipe = db.relationship("Recipe", foreign_keys=[recipe_id])

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None
