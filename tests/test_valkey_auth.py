"""Regression tests for Valkey IAM client TLS trust (Memorystore CA).

flask-backend silently fell back to in-process SimpleCache on every boot since
v0.3.5: _build_client() opened the TLS connection with ssl=True but never
trusted Memorystore's Google-managed private CA, so the handshake failed with
CERTIFICATE_VERIFY_FAILED, create_iam_redis_client() returned None, and the
Flask-Caching response cache degraded to a per-worker in-process SimpleCache.

The fix mirrors the working Express reference (server/valkey.ts): read the CA
PEM from VALKEY_CA_CERT and hand it to redis-py as ssl_ca_data, without ever
disabling certificate verification.
"""

import ssl as ssl_mod
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils import valkey_auth  # noqa: E402

# A syntactically shaped but fake PEM — the client is never actually opened.
_FAKE_PEM = (
    "-----BEGIN CERTIFICATE-----\n" "MIIBfakememorystorecabytes==\n" "-----END CERTIFICATE-----\n"
)

# ssl_cert_reqs values that would DISABLE or weaken verification.
_INSECURE_CERT_REQS = (None, "none", "optional", ssl_mod.CERT_NONE, ssl_mod.CERT_OPTIONAL)


def _capture_strictredis(monkeypatch):
    """Patch redis.StrictRedis to capture constructor kwargs; return the dict.

    Also stubs the GCP IAM token fetch so the test never touches the network.
    """
    captured = {}

    def fake_strictredis(**kwargs):
        captured.update(kwargs)
        return object()  # _build_client only constructs & returns; never calls it

    monkeypatch.setattr("redis.StrictRedis", fake_strictredis)
    monkeypatch.setattr(valkey_auth, "_get_iam_token", lambda: ("sa@project.iam", "iam-token"))
    return captured


def test_build_client_trusts_ca_cert_from_env(monkeypatch):
    """VALKEY_CA_CERT is passed to redis-py as ssl_ca_data, TLS still verified."""
    captured = _capture_strictredis(monkeypatch)
    monkeypatch.setenv("VALKEY_CA_CERT", _FAKE_PEM)

    valkey_auth._build_client("10.128.0.11", 6379)

    assert captured.get("ssl") is True
    assert captured.get("ssl_ca_data") == _FAKE_PEM
    # IAM token stays the password; host/port preserved.
    assert captured.get("password") == "iam-token"
    assert captured.get("host") == "10.128.0.11"
    assert captured.get("port") == 6379
    # Trusting the CA is the fix — verification must NOT be disabled as a shortcut.
    assert captured.get("ssl_cert_reqs", "required") not in _INSECURE_CERT_REQS


def test_build_client_without_ca_cert_keeps_tls_on_system_trust(monkeypatch):
    """Absent VALKEY_CA_CERT: TLS stays on via the system trust store (no ssl_ca_data).

    Guards the local/dev path so the fix stays conditional and never weakens TLS.
    """
    captured = _capture_strictredis(monkeypatch)
    monkeypatch.delenv("VALKEY_CA_CERT", raising=False)

    valkey_auth._build_client("10.128.0.11", 6379)

    assert captured.get("ssl") is True
    # No CA override -> None (or absent) -> redis-py uses the system trust store.
    assert not captured.get("ssl_ca_data")
    assert captured.get("ssl_cert_reqs", "required") not in _INSECURE_CERT_REQS
