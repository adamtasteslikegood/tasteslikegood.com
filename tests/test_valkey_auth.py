"""Regression tests for Valkey IAM authentication (Memorystore).

Covers:
- TLS CA trust (VALKEY_CA_CERT → ssl_ca_data)
- RESP2 protocol enforcement (avoids redis-py 8.x RESP3 "default" username injection)
- Token refresh retry with exponential backoff on failure
"""

import ssl as ssl_mod
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_build_client_forces_resp2_protocol(monkeypatch):
    """redis-py 8.x defaults to RESP3 which injects 'default' as username.
    Memorystore IAM auth rejects the 'default' username, causing AuthenticationError.
    """
    captured = _capture_strictredis(monkeypatch)
    monkeypatch.setenv("VALKEY_CA_CERT", _FAKE_PEM)

    valkey_auth._build_client("10.128.0.11", 6379)

    assert captured.get("protocol") == 2, "Must force RESP2 to avoid 'default' username injection"


def test_refresh_loop_retries_on_failure(monkeypatch):
    """On token refresh failure, the loop should retry with backoff, not sleep 45 min."""
    call_count = 0
    sleeps = []

    def fake_refresh():
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ConnectionError("token refresh failed")
        return True

    monkeypatch.setattr(valkey_auth, "_refresh_token_in_place", fake_refresh)

    original_sleep = time.sleep

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise StopIteration("stop the loop")

    monkeypatch.setattr(time, "sleep", fake_sleep)

    try:
        valkey_auth._refresh_loop()
    except StopIteration:
        pass

    # First sleep is the normal 45-min interval
    assert sleeps[0] == valkey_auth._TOKEN_REFRESH_INTERVAL
    # After failure, retry backoff should be much shorter than 45 min
    assert sleeps[1] == valkey_auth._RETRY_BASE  # 30s first retry
    assert sleeps[2] == valkey_auth._RETRY_BASE * 2  # 60s second retry
