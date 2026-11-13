from datetime import datetime

from extensions import db


class User(db.Model):  # type: ignore[name-defined, misc]
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=True)
    google_id = db.Column(db.String(100), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # Recipe feedback (KAN-356, A1): true once a moderator has approved one of
    # this account's notes; its later notes then publish without review. A
    # moderator hiding any of its notes or entries clears it again.
    feedback_trusted = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "google_id": self.google_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
