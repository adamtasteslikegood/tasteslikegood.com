from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate


class _NullCache:
    """No-op cache used until a real cache backend (Valkey/Redis) is wired up."""

    def get(self, key):
        return None

    def set(self, key, value, timeout=None):
        pass

    def delete(self, key):
        pass


db = SQLAlchemy()
migrate = Migrate()
cache = _NullCache()
