"""Regression tests for the shared response cache (issue #143).

The original Flask-Caching/Valkey wiring (48d5c14) was silently dropped in a
merge (07123c2) and later replaced by a no-op _NullCache stub (d677942), so
every cache.get/set in the app silently did nothing. These tests pin the
restored behavior:

- extensions.cache is a real Flask-Caching backend (set -> get roundtrip)
- create_app() falls back to SimpleCache when no Valkey/Redis is configured
- the cache_utils safe helpers and invalidation helpers actually work
- /api/recipes/<id>/image is served from cache after the first fetch
"""

import base64
import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import cache, db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from utils import cache_utils  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    # config.py reads env at import time, so patch the module attributes to
    # guarantee the SimpleCache path regardless of the developer's .env.
    monkeypatch.setattr("config.VALKEY_HOST", None)
    monkeypatch.setattr("config.REDIS_URL", None)
    app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with app.app_context():
        db.create_all()
        cache.clear()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def test_default_backend_is_simplecache(app):
    """Without Valkey/Redis configured, create_app wires an in-process cache."""
    assert app.config["CACHE_TYPE"] == "SimpleCache"


def test_cache_roundtrip(app):
    """extensions.cache must actually store values (fails on a no-op stub)."""
    with app.app_context():
        cache.set("regression:143", "value", timeout=60)
        assert cache.get("regression:143") == "value"


def test_safe_helpers_roundtrip(app):
    with app.app_context():
        key = cache_utils.recipe_key(user_id=1, guest_session_id=None, recipe_id="abc")
        cache_utils.safe_set(key, {"name": "Chili"}, timeout=60)
        assert cache_utils.safe_get(key) == {"name": "Chili"}


def test_invalidate_recipe_deletes_keys(app):
    with app.app_context():
        rkey = cache_utils.recipe_key(1, None, "abc")
        skey = cache_utils.recipe_stats_key(1, None)
        cache_utils.safe_set(rkey, "recipe", timeout=60)
        cache_utils.safe_set(skey, "stats", timeout=60)

        cache_utils.invalidate_recipe(1, None, "abc")

        assert cache_utils.safe_get(rkey) is None
        assert cache_utils.safe_get(skey) is None


def test_image_endpoint_populates_and_serves_from_cache(app, client):
    """First image GET caches the bytes; later GETs are served from cache.

    Proven by deleting the DB row after the first request — the second
    request can only succeed if the bytes really live in the cache.
    """
    png_bytes = b"\x89PNG\r\n\x1a\ncached-image"
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        recipe = Recipe(
            id=recipe_id,
            user_id=None,
            name="Cached Curry",
            slug="cached-curry",
            is_public=True,
            data={
                "name": "Cached Curry",
                "ai_image_data": base64.b64encode(png_bytes).decode(),
            },
        )
        db.session.add(recipe)
        db.session.commit()

    first = client.get(f"/api/recipes/{recipe_id}/image")
    assert first.status_code == 200
    assert first.data == png_bytes

    with app.app_context():
        Recipe.query.filter_by(id=recipe_id).delete()
        db.session.commit()

    second = client.get(f"/api/recipes/{recipe_id}/image")
    assert second.status_code == 200
    assert second.data == png_bytes


def test_image_invalidation_clears_cached_bytes(app, client):
    with app.app_context():
        key = cache_utils.recipe_image_key("gone")
        cache_utils.safe_set(key, b"stale", timeout=60)
        cache_utils.invalidate_recipe_image("gone")
        assert cache_utils.safe_get(key) is None
