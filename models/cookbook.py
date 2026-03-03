from datetime import datetime

from sqlalchemy import JSON as GenericJSON

from extensions import db


class Cookbook(db.Model):
    __tablename__ = "cookbook"

    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    guest_session_id = db.Column(db.String(64), nullable=True, index=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(500), nullable=True, default="")
    cover_image = db.Column(db.String(500), nullable=True)
    recipe_ids = db.Column(GenericJSON, nullable=False, default=list)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user = db.relationship("User", backref=db.backref("cookbooks", lazy=True))

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "coverImage": self.cover_image,
            "recipeIds": self.recipe_ids or [],
            "user_id": self.user_id,
            "guest_session_id": self.guest_session_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
