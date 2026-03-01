from extensions import db
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy import JSON as GenericJSON

class Recipe(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    # Using GenericJSON for SQLite compatibility but acts like JSON in Postgres
    data = db.Column(GenericJSON, nullable=False)

    user = db.relationship('User', backref=db.backref('recipes', lazy=True))

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'data': self.data
        }
