"""Regression tests for the shared response cache (issue #143).

The original Flask-Caching/Valkey wiring (48d5c14) was silently dropped in a
merge (07123c2) and later replaced by a no-op _NullCache stub (d677942), so
every cache.get/set in the app silently did nothing. These tests pin the
restored behavior:

- extensions.cache is a real Flask-Caching backend (set -> get roundtrip)
- create_app() falls back to SimpleCache when no Valkey/Redis is configured
- the VALKEY_HOST branch wires a RedisCache around the configured client,
  and an unreachable Valkey degrades to SimpleCache instead of failing boot
- the cache_utils safe helpers and invalidation helpers actually work
- /api/recipes/<id>/image is served from cache after the first fetch, but
  only AFTER the access check — cached bytes of private or deleted recipes
  are never served to unauthorized clients
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
from models.user import User  # noqa: E402
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


def _make_recipe(recipe_id, png_bytes, *, public=True, owner=None):
    return Recipe(
        id=recipe_id,
        user_id=owner.id if owner else None,
        name="Cached Curry",
        slug=f"cached-curry-{recipe_id[:8]}",
        is_public=public,
        data={
            "name": "Cached Curry",
            "ai_image_data": base64.b64encode(png_bytes).decode(),
        },
    )


# ── Backend selection ─────────────────────────────────────────────────────────


def test_default_backend_is_simplecache(app):
    """Without Valkey/Redis configured, create_app wires an in-process cache."""
    assert app.config["CACHE_TYPE"] == "SimpleCache"


class FakeRedis:
    """Minimal redis-py-compatible client backing cachelib's RedisCache."""

    def __init__(self, **kwargs):
        self.store = {}
        self.pinged = False

    def ping(self):
        self.pinged = True
        return True

    def get(self, name):
        return self.store.get(name)

    def set(self, name=None, value=None, **kwargs):
        self.store[name] = value
        return True

    def setex(self, name=None, value=None, time=None, **kwargs):
        self.store[name] = value
        return True

    def delete(self, *names):
        return sum(1 for n in names if self.store.pop(n, None) is not None)


def test_valkey_password_backend_roundtrips_through_client(monkeypatch):
    """VALKEY_HOST + password auth wires RedisCache around the real client."""
    fake = FakeRedis()
    monkeypatch.setattr("config.VALKEY_HOST", "valkey.test")
    monkeypatch.setattr("config.VALKEY_AUTH_MODE", "password")
    monkeypatch.setattr("config.REDIS_URL", None)
    monkeypatch.setattr("redis.StrictRedis", lambda **kwargs: fake)

    app = create_app(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")

    assert app.config["CACHE_TYPE"] == "RedisCache"
    assert fake.pinged
    with app.app_context():
        cache.set("k", {"n": 1}, timeout=60)
        assert cache.get("k") == {"n": 1}
    # The value must have travelled through the configured client.
    assert any(key.endswith("k") for key in fake.store)


def test_unreachable_valkey_falls_back_to_simplecache(monkeypatch, caplog):
    class BoomRedis:
        def __init__(self, **kwargs):
            pass

        def ping(self):
            raise ConnectionError("valkey unreachable")

    monkeypatch.setattr("config.VALKEY_HOST", "valkey.test")
    monkeypatch.setattr("config.VALKEY_AUTH_MODE", "password")
    monkeypatch.setattr("config.REDIS_URL", None)
    monkeypatch.setattr("redis.StrictRedis", BoomRedis)

    with caplog.at_level("WARNING"):
        app = create_app(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")

    assert app.config["CACHE_TYPE"] == "SimpleCache"
    # The fallback must be logged as configured-but-unavailable, not as
    # "no Valkey/Redis configured" — the two states need different triage.
    assert any("configured Valkey/Redis is unavailable" in r.message for r in caplog.records)
    with app.app_context():
        cache.set("k", "v", timeout=60)
        assert cache.get("k") == "v"


# ── Core roundtrip + helpers ──────────────────────────────────────────────────


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


def test_image_invalidation_clears_cached_bytes(app):
    with app.app_context():
        key = cache_utils.recipe_image_key("gone")
        cache_utils.safe_set(key, b"stale", timeout=60)
        cache_utils.invalidate_recipe_image("gone")
        assert cache_utils.safe_get(key) is None


# ── Image endpoint: caching behavior ──────────────────────────────────────────


def test_image_endpoint_populates_and_serves_from_cache(app, client):
    """First image GET caches the bytes; later GETs are served from cache.

    Proven by swapping the DB image after the first request — the second
    response returns the original bytes, which only exist in the cache.
    """
    png_bytes = b"\x89PNG\r\n\x1a\ncached-image"
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(_make_recipe(recipe_id, png_bytes, public=True))
        db.session.commit()

    first = client.get(f"/api/recipes/{recipe_id}/image")
    assert first.status_code == 200
    assert first.data == png_bytes
    assert first.headers["Cache-Control"] == "public, max-age=86400"

    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        recipe.data = {
            **recipe.data,
            "ai_image_data": base64.b64encode(b"regenerated-image").decode(),
        }
        db.session.commit()

    second = client.get(f"/api/recipes/{recipe_id}/image")
    assert second.status_code == 200
    assert second.data == png_bytes  # cache hit, not the regenerated DB bytes


# ── Image endpoint: access check runs BEFORE the cache lookup ─────────────────


def test_deleted_recipe_image_404s_even_when_cached(app, client):
    """A cached image must not outlive its recipe row."""
    png_bytes = b"\x89PNG\r\n\x1a\ndeleted"
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        db.session.add(_make_recipe(recipe_id, png_bytes, public=True))
        db.session.commit()

    assert client.get(f"/api/recipes/{recipe_id}/image").status_code == 200  # primes cache

    with app.app_context():
        Recipe.query.filter_by(id=recipe_id).delete()
        db.session.commit()

    assert client.get(f"/api/recipes/{recipe_id}/image").status_code == 404


def test_private_cached_image_not_served_to_strangers(app, client):
    """A primed private image must not be fetchable by unauthorized clients."""
    png_bytes = b"\x89PNG\r\n\x1a\nprivate"
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        owner = User(email="owner@example.com", name="Owner")
        db.session.add(owner)
        db.session.commit()
        db.session.add(_make_recipe(recipe_id, png_bytes, public=False, owner=owner))
        db.session.commit()
        # Simulate the owner having primed the cache.
        cache_utils.safe_set(cache_utils.recipe_image_key(recipe_id), png_bytes, timeout=60)

    resp = client.get(f"/api/recipes/{recipe_id}/image")
    assert resp.status_code == 404
    assert png_bytes not in resp.data
