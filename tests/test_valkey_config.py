"""Tests for the single Valkey config factory (KAN-160).

utils/valkey_config is the only module allowed to read VALKEY_*/REDIS_URL
environment variables. These tests pin:

- env consolidation: every variable resolves through the factory
- VALKEY_PORT validation: bad values warn loudly (naming the value) and
  fall back to the explicit default 6379 — the anti-silent-degradation
  contract from the 2026-07-25 board finding
- CA-cert passthrough for the Memorystore TLS trust chain
- the cache-backend priority contract VALKEY_HOST > REDIS_URL >
  in-process SimpleCache, exercised through create_app
"""

import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from utils.valkey_config import (  # noqa: E402
    DEFAULT_VALKEY_PORT,
    resolve_valkey_config,
)

_FAKE_PEM = (
    "-----BEGIN CERTIFICATE-----\n" "MIIBfakememorystorecabytes==\n" "-----END CERTIFICATE-----\n"
)

_ALL_VARS = (
    "VALKEY_HOST",
    "VALKEY_PORT",
    "VALKEY_AUTH_MODE",
    "VALKEY_PASSWORD",
    "VALKEY_CA_CERT",
    "REDIS_URL",
)


@pytest.fixture(autouse=True)
def _clean_valkey_env(monkeypatch):
    """Start every test from an empty Valkey env, whatever the local .env has."""
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


# ── Env consolidation ─────────────────────────────────────────────────────────


def test_resolve_reads_all_env_vars(monkeypatch):
    monkeypatch.setenv("VALKEY_HOST", "10.128.0.11")
    monkeypatch.setenv("VALKEY_PORT", "6380")
    monkeypatch.setenv("VALKEY_AUTH_MODE", "password")
    monkeypatch.setenv("VALKEY_PASSWORD", "hunter2")
    monkeypatch.setenv("VALKEY_CA_CERT", _FAKE_PEM)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    cfg = resolve_valkey_config()

    assert cfg.host == "10.128.0.11"
    assert cfg.port == 6380
    assert cfg.auth_mode == "password"
    assert cfg.password == "hunter2"
    assert cfg.ca_cert == _FAKE_PEM
    assert cfg.redis_url == "redis://localhost:6379/0"


def test_resolve_defaults_when_env_unset():
    cfg = resolve_valkey_config()

    assert cfg.host is None
    assert cfg.port == DEFAULT_VALKEY_PORT
    assert cfg.auth_mode == "iam"  # prod default, matches the pre-factory config.py
    assert cfg.password is None
    assert cfg.ca_cert is None
    assert cfg.redis_url is None


def test_ca_cert_passthrough(monkeypatch):
    """The Memorystore CA PEM must survive resolution byte-for-byte."""
    monkeypatch.setenv("VALKEY_CA_CERT", _FAKE_PEM)
    assert resolve_valkey_config().ca_cert == _FAKE_PEM


# ── VALKEY_PORT validation ────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["not-a-port", "", "6379.5"])
def test_port_non_numeric_warns_and_defaults(monkeypatch, caplog, bad):
    """Pre-factory this was int() at import time — a crash. Now: loud default."""
    monkeypatch.setenv("VALKEY_PORT", bad)

    with caplog.at_level("WARNING", logger="utils.valkey_config"):
        cfg = resolve_valkey_config()

    assert cfg.port == DEFAULT_VALKEY_PORT
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(repr(bad) in msg and str(DEFAULT_VALKEY_PORT) in msg for msg in warnings), (
        "warning must name the bad value and the default: %s" % warnings
    )


@pytest.mark.parametrize("bad", ["0", "-1", "65536", "700000"])
def test_port_out_of_range_warns_and_defaults(monkeypatch, caplog, bad):
    """Pre-factory an out-of-range port was accepted silently. Now: loud default."""
    monkeypatch.setenv("VALKEY_PORT", bad)

    with caplog.at_level("WARNING", logger="utils.valkey_config"):
        cfg = resolve_valkey_config()

    assert cfg.port == DEFAULT_VALKEY_PORT
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(repr(bad) in msg and str(DEFAULT_VALKEY_PORT) in msg for msg in warnings)


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("6380", 6380), ("65535", 65535)])
def test_port_valid_values_pass_through_without_warning(monkeypatch, caplog, raw, expected):
    monkeypatch.setenv("VALKEY_PORT", raw)

    with caplog.at_level("WARNING", logger="utils.valkey_config"):
        cfg = resolve_valkey_config()

    assert cfg.port == expected
    assert not caplog.records


# ── Priority contract: VALKEY_HOST > REDIS_URL > SimpleCache ─────────────────
#
# app.create_app branches on the config-module snapshot (which tests
# monkeypatch, same pattern as tests/test_cache.py), so the priority is
# exercised end to end through create_app.


class _FakeRedisClient:
    """Minimal client: just enough for create_app's ping() health check."""

    def __init__(self, **kwargs):
        self.pinged = False

    def ping(self):
        self.pinged = True
        return True


def _create_app():
    return create_app(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")


def test_priority_valkey_host_wins_over_redis_url(monkeypatch):
    """With both configured, VALKEY_HOST is used and REDIS_URL never consulted."""
    fake = _FakeRedisClient()
    from_url_calls = []
    monkeypatch.setattr("config.VALKEY_HOST", "valkey.test")
    monkeypatch.setattr("config.VALKEY_AUTH_MODE", "password")
    monkeypatch.setattr("config.REDIS_URL", "redis://should-be-ignored:6379/0")
    monkeypatch.setattr("redis.StrictRedis", lambda **kwargs: fake)
    monkeypatch.setattr(
        "redis.from_url",
        lambda *args, **kwargs: from_url_calls.append(args) or _FakeRedisClient(),
    )

    app = _create_app()

    assert app.config["CACHE_TYPE"] == "RedisCache"
    assert fake.pinged
    assert not from_url_calls


def test_priority_redis_url_used_when_no_valkey_host(monkeypatch):
    fake = _FakeRedisClient()
    monkeypatch.setattr("config.VALKEY_HOST", None)
    monkeypatch.setattr("config.REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr("redis.from_url", lambda url, **kwargs: fake)

    app = _create_app()

    assert app.config["CACHE_TYPE"] == "RedisCache"
    assert fake.pinged


def test_priority_neither_configured_falls_back_to_simplecache(monkeypatch):
    monkeypatch.setattr("config.VALKEY_HOST", None)
    monkeypatch.setattr("config.REDIS_URL", None)

    app = _create_app()

    assert app.config["CACHE_TYPE"] == "SimpleCache"


def test_password_auth_reads_password_via_factory(monkeypatch):
    """The old inline os.environ.get in app.py must now flow through the factory."""
    captured = {}

    def fake_strictredis(**kwargs):
        captured.update(kwargs)
        return _FakeRedisClient()

    monkeypatch.setattr("config.VALKEY_HOST", "valkey.test")
    monkeypatch.setattr("config.VALKEY_AUTH_MODE", "password")
    monkeypatch.setattr("config.REDIS_URL", None)
    monkeypatch.setenv("VALKEY_PASSWORD", "hunter2")
    monkeypatch.setattr("redis.StrictRedis", fake_strictredis)

    app = _create_app()

    assert app.config["CACHE_TYPE"] == "RedisCache"
    assert captured["password"] == "hunter2"
