"""Regression tests for Valkey IAM authentication (Memorystore).

Covers:
- TLS CA trust (VALKEY_CA_CERT → ssl_ca_data)
- RESP2 protocol enforcement (avoids redis-py 8.x RESP3 "default" username injection)
- Token refresh retry with exponential backoff on failure
- Expiry-aware refresh scheduling (KAN-268)
- AuthenticationError is never retried by redis-py (KAN-268)
- Debounced refresh-on-auth-failure hook (KAN-268)
"""

import ssl as ssl_mod
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from redis.backoff import NoBackoff
from redis.exceptions import AuthenticationError
from redis.exceptions import ConnectionError as RedisConnectionError

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils import valkey_auth  # noqa: E402


def _utcnow():
    """Naive UTC now — the form google-auth uses for Credentials.expiry."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    monkeypatch.setattr(
        valkey_auth, "_get_iam_token", lambda: ("sa@project.iam", "iam-token", None)
    )
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
    # Token reported as expiring in 35 min — the Cloud Run cached-token case.
    monkeypatch.setattr(valkey_auth, "_token_expiry", _utcnow() + timedelta(minutes=35))

    def fake_refresh():
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ConnectionError("token refresh failed")
        return True

    monkeypatch.setattr(valkey_auth, "_refresh_token_in_place", fake_refresh)

    class FakeCondition:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self, timeout):
            sleeps.append(timeout)
            if len(sleeps) >= 3:
                raise StopIteration("stop the loop")
            return False

    monkeypatch.setattr(valkey_auth, "_refresh_condition", FakeCondition())

    try:
        valkey_auth._refresh_loop()
    except StopIteration:
        pass

    # First sleep is expiry-derived (35 min − 5 min margin), NOT the fixed
    # 45 min that outlived the token (KAN-268).
    assert 29 * 60 <= sleeps[0] <= 30 * 60
    # After failure, retry backoff should be much shorter than 45 min
    assert sleeps[1] == valkey_auth._RETRY_BASE  # 30s first retry
    assert sleeps[2] == valkey_auth._RETRY_BASE * 2  # 60s second retry


def test_refresh_loop_recomputes_deadline_when_expiry_changes(monkeypatch):
    """An auth-triggered refresh wakes the loop before the old deadline."""
    waits = []
    refreshed = []
    monkeypatch.setattr(valkey_auth, "_token_expiry", _utcnow() + timedelta(minutes=60))
    monkeypatch.setattr(
        valkey_auth, "_refresh_token_in_place", lambda: refreshed.append(True) or True
    )

    class FakeCondition:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                # Model another thread installing a cached token with a shorter
                # lifetime and notifying the condition.
                valkey_auth._token_expiry = _utcnow() + timedelta(minutes=35)
                return True
            raise StopIteration("deadline recomputed")

    monkeypatch.setattr(valkey_auth, "_refresh_condition", FakeCondition())

    with pytest.raises(StopIteration, match="deadline recomputed"):
        valkey_auth._refresh_loop()

    assert waits[0] == valkey_auth._TOKEN_REFRESH_INTERVAL
    assert 29 * 60 <= waits[1] <= 30 * 60
    assert refreshed == []


def test_create_client_wakes_existing_refresh_loop(monkeypatch):
    """A replacement client's expiry must reschedule an already-running loop."""
    expiry = _utcnow() + timedelta(minutes=35)
    notifications = []

    class _Client:
        def ping(self):
            return True

    class _AliveThread:
        def is_alive(self):
            return True

    class _Condition:
        def notify_all(self):
            notifications.append(1)

    client = _Client()
    monkeypatch.setattr(valkey_auth, "_build_client", lambda host, port: (client, expiry))
    monkeypatch.setattr(valkey_auth, "_refresh_thread", _AliveThread())
    monkeypatch.setattr(valkey_auth, "_refresh_condition", _Condition())
    monkeypatch.setattr(valkey_auth, "_current_client", None)
    monkeypatch.setattr(valkey_auth, "_token_expiry", None)
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "monotonic", 1.0)
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "result", False)

    assert valkey_auth.create_iam_redis_client("10.128.0.11") is client
    assert valkey_auth._token_expiry == expiry
    assert notifications == [1]
    assert valkey_auth._auth_failure_refresh_state.monotonic is None
    assert valkey_auth._auth_failure_refresh_state.result is None


# ── Expiry-aware refresh scheduling (KAN-268) ─────────────────────────────


_NOW = datetime(2026, 9, 29, 12, 0, 0)


def test_refresh_delay_is_expiry_minus_margin():
    """A cached token with 35 min left is refreshed at 30 min, not 45."""
    delay = valkey_auth._refresh_delay(_NOW + timedelta(minutes=35), now=_NOW)
    assert delay == 30 * 60


def test_refresh_delay_capped_at_interval_for_fresh_token():
    """A full 60-min token still refreshes at the 45-min cap."""
    delay = valkey_auth._refresh_delay(_NOW + timedelta(minutes=60), now=_NOW)
    assert delay == valkey_auth._TOKEN_REFRESH_INTERVAL


def test_refresh_delay_floor_when_inside_margin_or_expired():
    """A token already inside the margin (or expired) re-polls at the floor."""
    near = valkey_auth._refresh_delay(_NOW + timedelta(minutes=3), now=_NOW)
    past = valkey_auth._refresh_delay(_NOW - timedelta(minutes=10), now=_NOW)
    assert near == valkey_auth._MIN_REFRESH_DELAY
    assert past == valkey_auth._MIN_REFRESH_DELAY
    assert valkey_auth._MIN_REFRESH_DELAY > 0


def test_refresh_delay_missing_expiry_is_short_not_45_min():
    """Unknown expiry must not fall back to the 45-min assumption that caused KAN-268."""
    delay = valkey_auth._refresh_delay(None, now=_NOW)
    assert delay == valkey_auth._UNKNOWN_EXPIRY_DELAY
    assert delay < valkey_auth._TOKEN_REFRESH_INTERVAL


def test_refresh_delay_accepts_aware_and_naive_mix():
    """Aware expiry vs naive now (and vice versa) compares in UTC, no TypeError."""
    aware_expiry = (_NOW + timedelta(minutes=35)).replace(tzinfo=timezone.utc)
    assert valkey_auth._refresh_delay(aware_expiry, now=_NOW) == 30 * 60
    aware_now = _NOW.replace(tzinfo=timezone.utc)
    assert valkey_auth._refresh_delay(_NOW + timedelta(minutes=35), now=aware_now) == 30 * 60
    # Non-UTC offset is normalised, not taken at face value.
    plus2 = timezone(timedelta(hours=2))
    offset_expiry = (_NOW + timedelta(hours=2, minutes=35)).replace(tzinfo=plus2)
    assert valkey_auth._refresh_delay(offset_expiry, now=_NOW) == 30 * 60


def test_refresh_delay_default_now_uses_naive_utc():
    """Without an explicit now, a naive-UTC expiry 35 min out yields ~30 min."""
    delay = valkey_auth._refresh_delay(_utcnow() + timedelta(minutes=35))
    assert 29 * 60 <= delay <= 30 * 60


def test_refresh_in_place_records_new_expiry(monkeypatch):
    """A successful in-place refresh stores the new token's expiry for the loop."""
    new_expiry = _NOW + timedelta(minutes=33)

    class _Pool:
        def __init__(self):
            self.connection_kwargs = {}
            self.disconnected = False

        def disconnect(self):
            self.disconnected = True

    class _Client:
        def __init__(self):
            self.connection_pool = _Pool()

        def ping(self):
            return True

    client = _Client()
    monkeypatch.setattr(valkey_auth, "_current_client", client)
    monkeypatch.setattr(valkey_auth, "_token_expiry", None)
    monkeypatch.setattr(valkey_auth, "_get_iam_token", lambda: ("sa", "tok2", new_expiry))

    assert valkey_auth._refresh_token_in_place() is True
    assert valkey_auth._token_expiry == new_expiry
    assert client.connection_pool.connection_kwargs["password"] == "tok2"
    assert client.connection_pool.disconnected is True


# ── redis-py must not retry a rejected credential (KAN-268) ───────────────


def test_build_client_installs_no_auth_retry(monkeypatch):
    captured = _capture_strictredis(monkeypatch)
    valkey_auth._build_client("10.128.0.11", 6379)
    assert isinstance(captured.get("retry"), valkey_auth._NoAuthRetry)
    assert captured["retry"].get_retries() == valkey_auth._REDIS_RETRIES


def test_no_auth_retry_fails_fast_on_authentication_error():
    """AuthenticationError (a ConnectionError subclass) raises on the first attempt."""
    retry = valkey_auth._NoAuthRetry(NoBackoff(), 10)
    calls = []
    fails = []

    def do():
        calls.append(1)
        raise AuthenticationError("invalid password")

    with pytest.raises(AuthenticationError):
        retry.call_with_retry(do, lambda e: fails.append(e))
    assert len(calls) == 1
    assert fails == []


def test_no_auth_retry_still_retries_plain_connection_errors():
    """Refused sockets / dropped connections keep the normal retry behaviour."""
    retry = valkey_auth._NoAuthRetry(NoBackoff(), 3)
    calls = []

    def do():
        calls.append(1)
        if len(calls) < 3:
            raise RedisConnectionError("connection reset")
        return "ok"

    assert retry.call_with_retry(do, lambda e: None) == "ok"
    assert len(calls) == 3


def test_no_auth_retry_honours_caller_is_retryable_and_failure_count():
    retry = valkey_auth._NoAuthRetry(NoBackoff(), 5)
    calls = []
    counts = []

    def do():
        calls.append(1)
        raise RedisConnectionError("nope")

    with pytest.raises(RedisConnectionError):
        retry.call_with_retry(
            do,
            lambda e, n: counts.append(n),
            is_retryable=lambda e: len(calls) < 2,
            with_failure_count=True,
        )
    assert len(calls) == 2
    assert counts == [1]


# ── refresh_after_auth_failure hook (KAN-268) ─────────────────────────────


def test_refresh_after_auth_failure_without_client_returns_false(monkeypatch):
    monkeypatch.setattr(valkey_auth, "_current_client", None)
    called = []
    monkeypatch.setattr(
        valkey_auth, "_refresh_token_in_place", lambda force=False: called.append(1)
    )
    assert valkey_auth.refresh_after_auth_failure() is False
    assert called == []


def test_auth_failure_recovery_is_not_suppressed_by_general_refresh_state(monkeypatch):
    """Only auth-failure attempts participate in the recovery debounce."""
    monkeypatch.setattr(valkey_auth, "_current_client", object())
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "monotonic", None)
    calls = []
    monkeypatch.setattr(
        valkey_auth, "_refresh_token_in_place", lambda force=False: calls.append(1) or True
    )

    assert valkey_auth.refresh_after_auth_failure() is True
    assert calls == [1]
    assert valkey_auth._auth_failure_refresh_state.monotonic is not None


def test_refresh_after_auth_failure_refreshes_once_then_debounces(monkeypatch):
    """A burst of auth failures triggers ONE refresh, not one per request."""
    monkeypatch.setattr(valkey_auth, "_current_client", object())
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "monotonic", None)
    calls = []

    def fake_refresh(force=False):
        calls.append(1)
        valkey_auth._auth_failure_refresh_state.monotonic = time.monotonic()
        return True

    monkeypatch.setattr(valkey_auth, "_refresh_token_in_place", fake_refresh)

    assert valkey_auth.refresh_after_auth_failure() is True
    assert valkey_auth.refresh_after_auth_failure() is True
    assert valkey_auth.refresh_after_auth_failure() is True
    assert len(calls) == 1


def test_refresh_after_auth_failure_refreshes_after_debounce_window(monkeypatch):
    monkeypatch.setattr(valkey_auth, "_current_client", object())
    stale = time.monotonic() - valkey_auth._AUTH_FAILURE_REFRESH_DEBOUNCE - 1
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "monotonic", stale)
    calls = []
    monkeypatch.setattr(
        valkey_auth, "_refresh_token_in_place", lambda force=False: calls.append(1) or True
    )
    assert valkey_auth.refresh_after_auth_failure() is True
    assert len(calls) == 1


def test_refresh_after_auth_failure_swallows_and_debounces_refresh_errors(monkeypatch):
    """Queued callers reuse a failed attempt instead of stampeding the metadata server."""
    monkeypatch.setattr(valkey_auth, "_current_client", object())
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "monotonic", None)
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "result", None)
    calls = []

    def boom(force=False):
        calls.append(1)
        raise RedisConnectionError("metadata server unreachable")

    monkeypatch.setattr(valkey_auth, "_refresh_token_in_place", boom)
    assert valkey_auth.refresh_after_auth_failure() is False
    assert valkey_auth.refresh_after_auth_failure() is False
    assert calls == [1]


def test_refresh_after_auth_failure_is_single_flight(monkeypatch):
    """Concurrent auth failures trigger one refresh and share its result."""
    monkeypatch.setattr(valkey_auth, "_current_client", object())
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "monotonic", None)
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    calls = []
    results = []

    def fake_refresh(force=False):
        calls.append(1)
        first_started.set()
        assert release_first.wait(timeout=2)
        valkey_auth._auth_failure_refresh_state.monotonic = time.monotonic()
        return True

    def worker(started=None):
        if started is not None:
            started.set()
        results.append(valkey_auth.refresh_after_auth_failure())

    monkeypatch.setattr(valkey_auth, "_refresh_token_in_place", fake_refresh)
    first = threading.Thread(target=worker)
    first.start()
    assert first_started.wait(timeout=2)

    second = threading.Thread(target=worker, args=(second_started,))
    second.start()
    assert second_started.wait(timeout=2)
    # Give the second worker a chance to contend for the single-flight lock
    # while the first refresh is deliberately held open.
    time.sleep(0.05)
    release_first.set()

    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert results == [True, True]
    assert len(calls) == 1


# ── PING runs outside the state lock (PR #338 review) ─────────────────────


def _refresh_fixture(monkeypatch, on_ping):
    """Install a fake client whose ping() runs ``on_ping``; return (client, expiry)."""
    new_expiry = _NOW + timedelta(minutes=33)

    class _Pool:
        def __init__(self):
            self.connection_kwargs = {}

        def disconnect(self):
            pass

    class _Client:
        def __init__(self):
            self.connection_pool = _Pool()

        def ping(self):
            return on_ping()

    client = _Client()
    monkeypatch.setattr(valkey_auth, "_current_client", client)
    monkeypatch.setattr(valkey_auth, "_token_expiry", None)
    monkeypatch.setattr(valkey_auth, "_get_iam_token", lambda: ("sa", "tok2", new_expiry))
    return client, new_expiry


def test_refresh_in_place_pings_without_holding_state_lock(monkeypatch):
    """A slow PING (redis-py retries on a flaky backend) must not hold _lock."""
    lock_free_during_ping = []

    def on_ping():
        acquired = valkey_auth._lock.acquire(blocking=False)
        if acquired:
            valkey_auth._lock.release()
        lock_free_during_ping.append(acquired)
        return True

    _, new_expiry = _refresh_fixture(monkeypatch, on_ping)

    assert valkey_auth._refresh_token_in_place() is True
    assert lock_free_during_ping == [True]
    assert valkey_auth._token_expiry == new_expiry


def test_refresh_superseded_during_ping_does_not_publish_expiry(monkeypatch):
    """A newer refresh that installs a token while this PING runs owns the expiry."""

    def on_ping():
        with valkey_auth._lock:
            valkey_auth._refresh_generation += 1  # a concurrent refresh installed
        return True

    _refresh_fixture(monkeypatch, on_ping)

    assert valkey_auth._refresh_token_in_place() is True
    assert valkey_auth._token_expiry is None


def test_refresh_after_client_replaced_during_ping_does_not_publish_expiry(monkeypatch):
    """A replacement client (create_iam_redis_client) publishes its own expiry."""
    replacement = object()

    def on_ping():
        with valkey_auth._lock:
            valkey_auth._current_client = replacement
        return True

    _refresh_fixture(monkeypatch, on_ping)

    assert valkey_auth._refresh_token_in_place() is True
    assert valkey_auth._token_expiry is None
    assert valkey_auth._current_client is replacement


def test_refresh_ping_failure_raises_and_keeps_old_expiry(monkeypatch):
    def on_ping():
        raise RedisConnectionError("backend down")

    _refresh_fixture(monkeypatch, on_ping)

    with pytest.raises(RedisConnectionError):
        valkey_auth._refresh_token_in_place()
    assert valkey_auth._token_expiry is None
    # The lock is not left held after the failure.
    assert valkey_auth._lock.acquire(blocking=False)
    valkey_auth._lock.release()


def test_refresh_delay_unknown_expiry_returns_float():
    """e808898: every return path of the ``-> float`` function returns a float."""
    assert isinstance(valkey_auth._refresh_delay(None, now=_NOW), float)


# ── Unchanged token keeps the pool (KAN-268 connection reuse) ─────────────


class _CountingPool:
    def __init__(self, password):
        self.connection_kwargs = {"password": password}
        self._available_connections = []
        self._in_use_connections = []
        self.disconnects = 0

    def disconnect(self):
        self.disconnects += 1


class _CountingClient:
    def __init__(self, password):
        self.connection_pool = _CountingPool(password)
        self.pings = 0

    def ping(self):
        self.pings += 1
        return True


def _install_counting_client(monkeypatch, pool_password, fetched_token):
    new_expiry = _NOW + timedelta(minutes=33)
    client = _CountingClient(pool_password)
    monkeypatch.setattr(valkey_auth, "_current_client", client)
    monkeypatch.setattr(valkey_auth, "_token_expiry", None)
    monkeypatch.setattr(valkey_auth, "_get_iam_token", lambda: ("sa", fetched_token, new_expiry))
    # create_iam_redis_client PINGs before installing, so a live pool is verified.
    monkeypatch.setattr(valkey_auth, "_pool_verified", True)
    return client, new_expiry


def test_unchanged_token_after_failed_ping_is_verified_again(monkeypatch):
    """A new token whose PING failed must not be trusted on the next attempt.

    The metadata server hands the same token back on the scheduled retry, so the
    retry sees it as unchanged. It must still disconnect and PING before it
    publishes the expiry and reports success (Backend #347 review)."""
    client, new_expiry = _install_counting_client(monkeypatch, "tok1", "tok2")
    pings = iter([RedisConnectionError("backend down"), True])

    def ping():
        client.pings += 1
        result = next(pings)
        if isinstance(result, Exception):
            raise result
        return result

    client.ping = ping

    with pytest.raises(RedisConnectionError):
        valkey_auth._refresh_token_in_place()
    assert valkey_auth._token_expiry is None
    assert valkey_auth._pool_verified is False

    # Retry: the pool already holds tok2, so the token looks unchanged.
    assert valkey_auth._refresh_token_in_place() is True
    assert client.connection_pool.disconnects == 2
    assert client.pings == 2
    assert valkey_auth._token_expiry == new_expiry
    assert valkey_auth._pool_verified is True

    # Now verified, an unchanged token takes the shortcut again.
    assert valkey_auth._refresh_token_in_place() is True
    assert client.connection_pool.disconnects == 2
    assert client.pings == 2


def test_refresh_with_unchanged_token_keeps_pool_and_skips_ping(monkeypatch):
    """Same token from the metadata server: no teardown, no re-dial, expiry published."""
    client, new_expiry = _install_counting_client(monkeypatch, "tok1", "tok1")
    generation = valkey_auth._refresh_generation

    assert valkey_auth._refresh_token_in_place() is True
    assert client.connection_pool.disconnects == 0
    assert client.pings == 0
    assert valkey_auth._token_expiry == new_expiry
    assert valkey_auth._refresh_generation == generation


def test_forced_refresh_with_unchanged_token_still_disconnects_and_verifies(monkeypatch):
    """Auth-failure path: a just-rejected token is re-verified even if unchanged."""
    client, new_expiry = _install_counting_client(monkeypatch, "tok1", "tok1")

    assert valkey_auth._refresh_token_in_place(force=True) is True
    assert client.connection_pool.disconnects == 1
    assert client.pings == 1
    assert valkey_auth._token_expiry == new_expiry


def test_refresh_with_new_token_disconnects_and_verifies_once(monkeypatch):
    client, new_expiry = _install_counting_client(monkeypatch, "tok1", "tok2")

    assert valkey_auth._refresh_token_in_place() is True
    assert client.connection_pool.connection_kwargs["password"] == "tok2"
    assert client.connection_pool.disconnects == 1
    assert client.pings == 1
    assert valkey_auth._token_expiry == new_expiry


def test_refresh_after_auth_failure_forces_verification(monkeypatch):
    """refresh_after_auth_failure must never take the unchanged-token shortcut."""
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "monotonic", None)
    monkeypatch.setattr(valkey_auth._auth_failure_refresh_state, "result", None)
    client, _ = _install_counting_client(monkeypatch, "tok1", "tok1")

    assert valkey_auth.refresh_after_auth_failure() is True
    assert client.connection_pool.disconnects == 1
    assert client.pings == 1


def test_scheduled_cycle_tears_down_pool_once_not_twice(monkeypatch):
    """The production pattern: the first fetch of a cycle returns the old token
    with little life left, the poll 60 s later returns the new one. That cycle
    must disconnect + PING once (it did twice before KAN-268's reuse fix)."""
    near_expiry = _NOW + timedelta(minutes=4)
    fresh_expiry = _NOW + timedelta(minutes=35)
    fetches = iter([("sa", "tok1", near_expiry), ("sa", "tok2", fresh_expiry)])
    client = _CountingClient("tok1")
    monkeypatch.setattr(valkey_auth, "_current_client", client)
    monkeypatch.setattr(valkey_auth, "_token_expiry", None)
    monkeypatch.setattr(valkey_auth, "_get_iam_token", lambda: next(fetches))
    monkeypatch.setattr(valkey_auth, "_pool_verified", True)

    assert valkey_auth._refresh_token_in_place() is True
    # Unchanged token inside the margin: the loop re-polls on the floor delay.
    assert (
        valkey_auth._refresh_delay(valkey_auth._token_expiry, now=_NOW)
        == valkey_auth._MIN_REFRESH_DELAY
    )
    assert valkey_auth._refresh_token_in_place() is True

    assert client.connection_pool.disconnects == 1
    assert client.pings == 1
    assert client.connection_pool.connection_kwargs["password"] == "tok2"
    assert valkey_auth._token_expiry == fresh_expiry


# ── Refresh diagnostics spans (KAN-268): spans only, no behaviour change ──────


class _FakeSpan:
    def __init__(self, tracer, name):
        self._tracer = tracer
        self.name = name
        self.tags = {}
        self.metrics = {}
        self.error = None

    def set_tag(self, key, value):
        self.tags[key] = value

    def set_metric(self, key, value):
        self.metrics[key] = value

    def __enter__(self):
        self._tracer.stack.append(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.error = exc
        self._tracer.stack.pop()
        return False


class _FakeTracer:
    """Records spans in start order; ``current_root_span`` mirrors ddtrace."""

    def __init__(self, outer=None):
        self.spans = []
        self.stack = []
        if outer is not None:
            # Simulate an already-active root (e.g. a Flask request span).
            self.stack.append(_FakeSpan(self, outer))

    def trace(self, name):
        span = _FakeSpan(self, name)
        self.spans.append(span)
        return span

    def current_root_span(self):
        return self.stack[0] if self.stack else None

    def names(self):
        return [s.name for s in self.spans]

    def get(self, name):
        return next(s for s in self.spans if s.name == name)


@pytest.fixture
def fake_tracer(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(valkey_auth, "tracer", tracer)
    return tracer


def test_scheduled_refresh_new_token_emits_root_and_step_spans(monkeypatch, fake_tracer):
    client, new_expiry = _install_counting_client(monkeypatch, "tok1", "tok2")

    assert valkey_auth._traced_scheduled_refresh() is True

    assert fake_tracer.names() == [
        "valkey.token_refresh",
        "valkey.token_fetch",
        "valkey.pool_disconnect",
        "valkey.ping",
    ]
    root = fake_tracer.get("valkey.token_refresh")
    assert root.tags["token_changed"] == "true"
    assert root.metrics["wall_ms"] >= 0
    assert root.metrics["thread_cpu_ms"] >= 0
    assert fake_tracer.get("valkey.ping").tags["valkey.includes_connect"] == "true"
    # Behaviour unchanged: one teardown, one verifying PING, expiry published.
    assert client.connection_pool.connection_kwargs["password"] == "tok2"
    assert client.connection_pool.disconnects == 1
    assert client.pings == 1
    assert valkey_auth._token_expiry == new_expiry


def test_scheduled_refresh_unchanged_token_tags_false_and_skips_steps(monkeypatch, fake_tracer):
    client, new_expiry = _install_counting_client(monkeypatch, "tok1", "tok1")

    assert valkey_auth._traced_scheduled_refresh() is True

    assert fake_tracer.names() == ["valkey.token_refresh", "valkey.token_fetch"]
    assert fake_tracer.get("valkey.token_refresh").tags["token_changed"] == "false"
    assert client.connection_pool.disconnects == 0
    assert client.pings == 0
    assert valkey_auth._token_expiry == new_expiry


def test_scheduled_refresh_records_wall_and_thread_cpu_deltas(monkeypatch, fake_tracer):
    """wall ≫ CPU is the throttling signature the tags exist to show."""
    _install_counting_client(monkeypatch, "tok1", "tok2")
    walls = iter([100.0, 103.0])  # 3,000 ms elapsed
    cpus = iter([5.0, 5.02])  # 20 ms of CPU
    monkeypatch.setattr(valkey_auth.time, "monotonic", lambda: next(walls))
    monkeypatch.setattr(valkey_auth.time, "thread_time", lambda: next(cpus))

    valkey_auth._traced_scheduled_refresh()

    root = fake_tracer.get("valkey.token_refresh")
    assert root.metrics["wall_ms"] == pytest.approx(3000.0)
    assert root.metrics["thread_cpu_ms"] == pytest.approx(20.0)


def test_scheduled_refresh_failure_propagates_and_still_records_timing(monkeypatch, fake_tracer):
    client, _ = _install_counting_client(monkeypatch, "tok1", "tok2")

    def failing_ping():
        raise RedisConnectionError("tls handshake timed out")

    monkeypatch.setattr(client, "ping", failing_ping)

    with pytest.raises(RedisConnectionError):
        valkey_auth._traced_scheduled_refresh()

    root = fake_tracer.get("valkey.token_refresh")
    assert isinstance(root.error, RedisConnectionError)
    assert "wall_ms" in root.metrics and "thread_cpu_ms" in root.metrics


def test_refresh_loop_runs_scheduled_refresh_under_root_span(monkeypatch, fake_tracer):
    _install_counting_client(monkeypatch, "tok1", "tok2")
    waits = []

    class FakeCondition:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) >= 2:
                raise StopIteration("stop the loop")
            return False

    monkeypatch.setattr(valkey_auth, "_refresh_condition", FakeCondition())

    with pytest.raises(StopIteration):
        valkey_auth._refresh_loop()

    assert fake_tracer.names()[0] == "valkey.token_refresh"


def test_auth_failure_refresh_does_not_tag_a_foreign_root_span(monkeypatch):
    """Inside a request the root is the Flask span: token_changed stays off it."""
    tracer = _FakeTracer(outer="flask.request")
    monkeypatch.setattr(valkey_auth, "tracer", tracer)
    client, _ = _install_counting_client(monkeypatch, "tok1", "tok2")

    assert valkey_auth._refresh_token_in_place(force=True) is True

    assert "token_changed" not in tracer.current_root_span().tags
    assert tracer.names() == ["valkey.token_fetch", "valkey.pool_disconnect", "valkey.ping"]
    assert client.pings == 1
