"""Tests for the Valkey-backed response cache (issue #143).

extensions.cache was a permanent _NullCache stub — every safe_get/safe_set in
utils/cache_utils silently no-oped, so recipe images were re-fetched from
GCS/DB on every request. These tests pin the new wiring:

- ValkeyCache pickles arbitrary values and honors TTLs.
- CacheProxy starts on the null backend and can be upgraded in place.
- init_cache() selects the backend from VALKEY_* env vars and never raises.
- /api/status reports the live cache backend.
- The IAM client verifies TLS against VALKEY_CA_CERT when provided.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

import extensions
from extensions import CacheProxy, ValkeyCache, _NullCache, init_cache


class FakeRedis:
    """Minimal in-memory stand-in for redis.StrictRedis."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex

    def delete(self, key):
        self.store.pop(key, None)

    def ping(self):
        return True


@pytest.fixture(autouse=True)
def reset_cache_backend():
    """init_cache mutates the module-level singleton — always restore it."""
    yield
    extensions.cache.configure(_NullCache())


class TestValkeyCache:
    def test_round_trips_dicts(self):
        c = ValkeyCache(FakeRedis())
        c.set("k", {"title": "Chili", "servings": 4})
        assert c.get("k") == {"title": "Chili", "servings": 4}

    def test_round_trips_raw_bytes(self):
        # generation_api_bp caches recipe image bytes directly
        c = ValkeyCache(FakeRedis())
        c.set("img", b"\x89PNG\r\n")
        assert c.get("img") == b"\x89PNG\r\n"

    def test_miss_returns_none(self):
        assert ValkeyCache(FakeRedis()).get("absent") is None

    def test_timeout_forwarded_as_expiry(self):
        fake = FakeRedis()
        ValkeyCache(fake).set("k", "v", timeout=300)
        assert fake.ttls["k"] == 300

    def test_delete_removes_key(self):
        fake = FakeRedis()
        c = ValkeyCache(fake)
        c.set("k", "v")
        c.delete("k")
        assert c.get("k") is None


class TestCacheProxy:
    def test_defaults_to_null_backend(self):
        proxy = CacheProxy()
        assert proxy.backend_name == "null"
        proxy.set("k", "v")
        assert proxy.get("k") is None
        assert proxy.ping() is False

    def test_configure_swaps_backend_in_place(self):
        proxy = CacheProxy()
        proxy.configure(ValkeyCache(FakeRedis()))
        assert proxy.backend_name == "valkey"
        proxy.set("k", "v")
        assert proxy.get("k") == "v"

    def test_cache_utils_flow_through_proxy(self):
        from utils import cache_utils

        extensions.cache.configure(ValkeyCache(FakeRedis()))
        key = cache_utils.recipe_image_key("abc123")
        cache_utils.safe_set(key, b"bytes", timeout=cache_utils.TTL_IMAGE)
        assert cache_utils.safe_get(key) == b"bytes"
        cache_utils.invalidate_recipe_image("abc123")
        assert cache_utils.safe_get(key) is None


class TestInitCache:
    def test_no_host_keeps_null_backend(self, monkeypatch):
        monkeypatch.delenv("VALKEY_HOST", raising=False)
        assert init_cache() == "null"
        assert extensions.cache.backend_name == "null"

    def test_iam_mode_uses_iam_client(self, monkeypatch):
        monkeypatch.setenv("VALKEY_HOST", "10.0.0.5")
        monkeypatch.setenv("VALKEY_AUTH_MODE", "iam")
        fake = FakeRedis()
        with patch("utils.valkey_auth.create_iam_redis_client", return_value=fake) as m:
            assert init_cache() == "valkey"
        m.assert_called_once_with("10.0.0.5", 6379)
        assert extensions.cache.backend_name == "valkey"

    def test_iam_client_failure_keeps_null_backend(self, monkeypatch):
        monkeypatch.setenv("VALKEY_HOST", "10.0.0.5")
        monkeypatch.setenv("VALKEY_AUTH_MODE", "iam")
        with patch("utils.valkey_auth.create_iam_redis_client", return_value=None):
            assert init_cache() == "null"
        assert extensions.cache.backend_name == "null"

    def test_plain_mode_pings_before_use(self, monkeypatch):
        monkeypatch.setenv("VALKEY_HOST", "localhost")
        monkeypatch.delenv("VALKEY_AUTH_MODE", raising=False)
        monkeypatch.setenv("VALKEY_PORT", "6380")
        fake = MagicMock()
        with patch("redis.StrictRedis", return_value=fake) as m:
            assert init_cache() == "valkey"
        fake.ping.assert_called_once()
        assert m.call_args.kwargs["host"] == "localhost"
        assert m.call_args.kwargs["port"] == 6380

    def test_connection_error_never_raises(self, monkeypatch):
        monkeypatch.setenv("VALKEY_HOST", "localhost")
        monkeypatch.delenv("VALKEY_AUTH_MODE", raising=False)
        with patch("redis.StrictRedis", side_effect=ConnectionError("refused")):
            assert init_cache() == "null"
        assert extensions.cache.backend_name == "null"


class TestStatusEndpoint:
    @pytest.fixture
    def client(self):
        from app import create_app
        from extensions import db

        app = create_app(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
        with app.app_context():
            db.create_all()
            yield app.test_client()
            db.session.remove()
            db.drop_all()

    def test_status_reports_null_cache_without_valkey(self, client):
        body = client.get("/api/status").get_json()
        assert body["cache"] == {"backend": "null", "status": "disabled"}

    def test_status_reports_connected_valkey(self, client):
        extensions.cache.configure(ValkeyCache(FakeRedis()))
        body = client.get("/api/status").get_json()
        assert body["cache"] == {"backend": "valkey", "status": "connected"}


class TestIamClientTls:
    def _build(self, monkeypatch, ca_cert):
        from utils import valkey_auth

        if ca_cert is None:
            monkeypatch.delenv("VALKEY_CA_CERT", raising=False)
        else:
            monkeypatch.setenv("VALKEY_CA_CERT", ca_cert)
        with (
            patch.object(valkey_auth, "_get_iam_token", return_value=("sa@x", "tok")),
            patch("redis.StrictRedis") as m,
        ):
            valkey_auth._build_client("10.0.0.5", 6379)
        return m.call_args.kwargs

    def test_ca_cert_enables_verification(self, monkeypatch):
        kwargs = self._build(monkeypatch, "-----BEGIN CERTIFICATE-----\n...")
        assert kwargs["ssl"] is True
        assert kwargs["ssl_ca_data"].startswith("-----BEGIN CERTIFICATE-----")
        assert "ssl_cert_reqs" not in kwargs

    def test_missing_ca_cert_still_connects_with_tls(self, monkeypatch):
        kwargs = self._build(monkeypatch, None)
        assert kwargs["ssl"] is True
        assert kwargs["ssl_cert_reqs"] == "none"
        assert "ssl_ca_data" not in kwargs
