"""KAN-318: the IAM token is refreshed on the request path, not a starved thread.

Covers the RCP-118 acceptance criteria:
- inline refresh inside ``_EXPIRY_MARGIN`` (and a no-op outside it);
- single-flight under concurrent callers, unexpired and expired token;
- the background thread as the idle safety net (later margin, same gate);
- #344/#348 semantics kept on the inline path (unchanged verified token keeps
  the pool; a token whose PING failed is verified again);
- a refresh never closes a connection another thread is using (the
  2026-10-02T04:45:49Z "I/O operation on closed file" burst), checked against
  real redis-py pool/connection objects.
"""

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils import cache_utils, valkey_auth  # noqa: E402


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _Pool:
    def __init__(self, password):
        self.connection_kwargs = {"password": password}
        self._available_connections = []
        self._in_use_connections = set()
        self.disconnect_calls = []

    def disconnect(self, inuse_connections=True):
        self.disconnect_calls.append(inuse_connections)


class _Client:
    def __init__(self, password):
        self.connection_pool = _Pool(password)
        self.pings = 0

    def ping(self):
        self.pings += 1
        return True


@pytest.fixture
def iam(monkeypatch):
    """A verified client whose token expires in ``minutes``; fetches are counted."""

    class _State:
        fetches = 0
        fetch_token = "tok2"
        fetch_expiry = _utcnow() + timedelta(minutes=33)
        fetch_delay = 0.0
        fetch_error = None

    state = _State()
    client = _Client("tok1")

    def fake_get_iam_token():
        state.fetches += 1
        if state.fetch_delay:
            time.sleep(state.fetch_delay)
        if state.fetch_error is not None:
            raise state.fetch_error
        return "sa", state.fetch_token, state.fetch_expiry

    monkeypatch.setattr(valkey_auth, "_current_client", client)
    monkeypatch.setattr(valkey_auth, "_pool_verified", True)
    monkeypatch.setattr(valkey_auth, "_last_inline_attempt", None)
    monkeypatch.setattr(valkey_auth, "_get_iam_token", fake_get_iam_token)

    def expire_in(minutes):
        monkeypatch.setattr(valkey_auth, "_token_expiry", _utcnow() + timedelta(minutes=minutes))

    state.client = client
    state.expire_in = expire_in
    return state


# ── inline refresh within the margin ─────────────────────────────────────


def test_outside_margin_is_a_no_op(iam):
    iam.expire_in(20)
    valkey_auth.ensure_fresh_token()
    assert iam.fetches == 0


def test_inside_margin_refreshes_inline(iam):
    iam.expire_in(3)
    valkey_auth.ensure_fresh_token()
    assert iam.fetches == 1
    assert iam.client.connection_pool.connection_kwargs["password"] == "tok2"
    assert iam.client.pings == 1
    assert valkey_auth._token_expiry == iam.fetch_expiry


def test_unknown_expiry_is_left_to_the_background_thread(iam, monkeypatch):
    monkeypatch.setattr(valkey_auth, "_token_expiry", None)
    valkey_auth.ensure_fresh_token()
    assert iam.fetches == 0


def test_no_client_is_a_no_op(monkeypatch):
    monkeypatch.setattr(valkey_auth, "_current_client", None)
    monkeypatch.setattr(valkey_auth, "_token_expiry", _utcnow())
    valkey_auth.ensure_fresh_token()  # must not raise


def test_inline_refresh_runs_in_a_request_triggered_span(iam, monkeypatch):
    spans = []

    class _Span:
        def __init__(self, name):
            self.name, self.tags, self.metrics = name, {}, {}

        def set_tag(self, k, v):
            self.tags[k] = v

        def set_metric(self, k, v):
            self.metrics[k] = v

        def __enter__(self):
            stack.append(self)
            return self

        def __exit__(self, *exc):
            stack.pop()
            return False

    stack = []

    class _Tracer:
        def trace(self, name):
            span = _Span(name)
            spans.append(span)
            return span

        def current_span(self):
            return stack[-1] if stack else None

    monkeypatch.setattr(valkey_auth, "tracer", _Tracer())
    iam.expire_in(3)
    valkey_auth.ensure_fresh_token()
    refresh = next(s for s in spans if s.name == "valkey.token_refresh")
    assert refresh.tags["trigger"] == "request"
    assert refresh.tags["token_changed"] == "true"
    assert {"wall_ms", "thread_cpu_ms"} <= set(refresh.metrics)
    assert [s.name for s in spans][1:] == [
        "valkey.token_fetch",
        "valkey.pool_disconnect",
        "valkey.ping",
    ]


def test_failed_inline_refresh_never_raises_and_is_throttled(iam):
    iam.expire_in(3)
    iam.fetch_error = RuntimeError("metadata server down")
    valkey_auth.ensure_fresh_token()
    valkey_auth.ensure_fresh_token()
    assert iam.fetches == 1  # second call inside _INLINE_RETRY_INTERVAL


def test_cached_token_still_in_margin_costs_one_fetch_per_interval(iam):
    """The metadata server can keep returning its cached token; don't hammer it."""
    iam.expire_in(3)
    iam.fetch_token = "tok1"  # unchanged
    iam.fetch_expiry = _utcnow() + timedelta(minutes=3)  # still inside the margin
    for _ in range(5):
        valkey_auth.ensure_fresh_token()
    assert iam.fetches == 1
    # Unchanged verified token: pool kept, no PING (#344).
    assert iam.client.connection_pool.disconnect_calls == []
    assert iam.client.pings == 0


def test_inline_retries_after_the_interval(iam, monkeypatch):
    iam.expire_in(3)
    iam.fetch_error = RuntimeError("down")
    valkey_auth.ensure_fresh_token()
    monkeypatch.setattr(
        valkey_auth,
        "_last_inline_attempt",
        time.monotonic() - valkey_auth._INLINE_RETRY_INTERVAL - 1,
    )
    iam.fetch_error = None
    valkey_auth.ensure_fresh_token()
    assert iam.fetches == 2
    assert valkey_auth._token_expiry == iam.fetch_expiry


# ── single-flight under concurrency ───────────────────────────────────────


def _run_concurrently(n, fn):
    barrier = threading.Barrier(n)
    durations = []

    def worker():
        barrier.wait()
        start = time.monotonic()
        fn()
        durations.append(time.monotonic() - start)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    return durations


def test_unexpired_token_concurrent_callers_refresh_once_and_do_not_wait(iam):
    iam.expire_in(3)
    iam.fetch_delay = 0.3
    durations = _run_concurrently(8, valkey_auth.ensure_fresh_token)
    assert iam.fetches == 1
    assert iam.client.pings == 1
    # Seven callers returned without waiting out the 0.3 s refresh.
    assert sorted(durations)[6] < iam.fetch_delay


def test_expired_token_concurrent_callers_wait_for_the_one_refresh(iam):
    iam.expire_in(-1)
    iam.fetch_delay = 0.3
    durations = _run_concurrently(8, valkey_auth.ensure_fresh_token)
    assert iam.fetches == 1
    # Waiters saw the published expiry under the gate and did not refetch.
    assert valkey_auth._token_expiry == iam.fetch_expiry
    assert min(durations) >= 0.25


def test_cache_ops_refresh_before_the_command(monkeypatch):
    order = []
    monkeypatch.setattr(cache_utils, "ensure_fresh_token", lambda: order.append("refresh"))

    class _Cache:
        def get(self, key):
            order.append("get")
            return b"v"

    monkeypatch.setattr(cache_utils, "cache", _Cache())
    assert cache_utils.safe_get("k") == b"v"
    assert order == ["refresh", "get"]


# ── background thread: idle safety net ────────────────────────────────────


def test_safety_net_wakes_after_the_inline_window(monkeypatch):
    expiry = _utcnow() + timedelta(minutes=35)
    monkeypatch.setattr(valkey_auth, "_token_expiry", expiry)
    waits = []

    class _Cond:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def wait(self, timeout):
            waits.append(timeout)
            raise StopIteration

    monkeypatch.setattr(valkey_auth, "_refresh_condition", _Cond())
    with pytest.raises(StopIteration):
        valkey_auth._refresh_loop()
    inline_deadline = valkey_auth._refresh_delay(expiry)
    assert waits[0] > inline_deadline  # requests get the first chance
    assert waits[0] == pytest.approx(
        valkey_auth._refresh_delay(expiry, margin=valkey_auth._IDLE_SAFETY_MARGIN), abs=2
    )
    assert valkey_auth._IDLE_SAFETY_MARGIN < valkey_auth._EXPIRY_MARGIN


def test_safety_net_refreshes_when_idle(iam):
    iam.expire_in(1)
    assert valkey_auth._scheduled_refresh_single_flight() is True
    assert iam.fetches == 1
    assert valkey_auth._token_expiry == iam.fetch_expiry


def test_safety_net_waits_for_an_in_flight_inline_refresh(iam):
    """Same gate: the scheduled refresh cannot run while a request refreshes."""
    iam.expire_in(3)
    order = []
    valkey_auth._expiry_refresh_lock.acquire()
    t = threading.Thread(
        target=lambda: order.append(("scheduled", valkey_auth._scheduled_refresh_single_flight()))
    )
    t.start()
    time.sleep(0.1)
    assert order == []  # blocked behind the inline holder
    order.append("inline-done")
    valkey_auth._expiry_refresh_lock.release()
    t.join(timeout=5)
    assert order == ["inline-done", ("scheduled", True)]


def test_safety_net_without_client_stops(monkeypatch):
    monkeypatch.setattr(valkey_auth, "_current_client", None)
    assert valkey_auth._scheduled_refresh_single_flight() is False


# ── #344/#348 semantics on the inline path ────────────────────────────────


def test_inline_unchanged_verified_token_keeps_pool(iam):
    iam.expire_in(3)
    iam.fetch_token = "tok1"
    valkey_auth.ensure_fresh_token()
    assert iam.client.connection_pool.disconnect_calls == []
    assert iam.client.pings == 0
    assert valkey_auth._token_expiry == iam.fetch_expiry


def test_inline_after_failed_ping_verifies_unchanged_token_again(iam, monkeypatch):
    iam.expire_in(3)
    pings = iter([RedisConnectionError("down"), True])

    def ping():
        iam.client.pings += 1
        result = next(pings)
        if isinstance(result, Exception):
            raise result
        return result

    iam.client.ping = ping
    valkey_auth.ensure_fresh_token()  # PING fails: swallowed, token unproven
    assert valkey_auth._pool_verified is False
    monkeypatch.setattr(valkey_auth, "_last_inline_attempt", None)
    valkey_auth.ensure_fresh_token()  # pool already holds tok2: "unchanged"
    assert iam.client.connection_pool.disconnect_calls == [False, False]
    assert iam.client.pings == 2
    assert valkey_auth._pool_verified is True


# ── never close a connection another thread is using ──────────────────────


class _Sock:
    def __init__(self):
        self.closed = False

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


# Test-only credential values; never passed as a literal password= argument.
OLD_TOKEN, NEW_TOKEN = "old-iam-token", "new-iam-token"


def _real_pool():
    """A real redis-py pool whose connections hold fake sockets (no network)."""
    pool = redis.ConnectionPool(host="10.0.0.1", port=6379)
    pool.connection_kwargs["password"] = OLD_TOKEN
    conns = []
    for _ in range(3):
        conn = pool.make_connection()
        conn.password = OLD_TOKEN
        conn._sock = _Sock()
        conns.append(conn)
    pool._available_connections.extend(conns)
    return pool, conns


def _install_real_pool(monkeypatch, pool, on_ping=None):
    class _RealPoolClient:
        connection_pool = pool

        def ping(self):
            return on_ping() if on_ping else True

    monkeypatch.setattr(valkey_auth, "_current_client", _RealPoolClient())
    monkeypatch.setattr(valkey_auth, "_pool_verified", True)
    monkeypatch.setattr(valkey_auth, "_token_expiry", None)
    monkeypatch.setattr(
        valkey_auth, "_get_iam_token", lambda: ("sa", NEW_TOKEN, _utcnow() + timedelta(minutes=33))
    )


def _checkout(pool):
    """get_connection()'s idle → in-use move, without its network connect()."""
    with pool._lock:
        conn = pool._available_connections.pop()
        pool._in_use_connections.add(conn)
    return conn


def test_refresh_marks_in_use_connections_instead_of_closing_them(monkeypatch):
    """Real redis-py pool: idle sockets close now, in-use ones on release."""
    pool, (idle, _, busy) = _real_pool()
    pool._available_connections.remove(busy)
    pool._in_use_connections.add(busy)
    idle_sock, busy_sock = idle._sock, busy._sock
    _install_real_pool(monkeypatch, pool)

    assert valkey_auth._refresh_token_in_place() is True
    assert idle_sock.closed is True
    assert busy_sock.closed is False  # an in-flight read keeps its socket
    assert busy.password == NEW_TOKEN and idle.password == NEW_TOKEN
    assert busy.should_reconnect() is True

    pool.release(busy)  # the request finishes with it
    assert busy_sock.closed is True
    assert busy.should_reconnect() is False  # reset by disconnect()


def test_checkout_racing_the_token_swap_cannot_keep_an_old_token_socket(monkeypatch):
    """#360 review: a checkout between the in-use snapshot and the idle
    disconnect must not escape both. Deterministic: the checkout is fired from
    inside pool.disconnect(), i.e. mid-swap, and must block on the pool lock
    until the swap is complete; the connection it then gets is disconnected
    (or, had it been in use, marked), so it re-authenticates with the new token.
    """
    pool, conns = _real_pool()
    got = []
    real_disconnect = pool.disconnect

    def disconnect_with_racing_checkout(inuse_connections=True):
        racer = threading.Thread(target=lambda: got.append(_checkout(pool)))
        racer.start()
        racer.join(timeout=0.2)
        assert racer.is_alive(), "checkout must wait for the token swap"
        real_disconnect(inuse_connections=inuse_connections)
        disconnect_with_racing_checkout.racer = racer

    pool.disconnect = disconnect_with_racing_checkout

    def ping():
        # Every connection the PING could be served by is either closed (will
        # reconnect with the new token) or marked to be dropped on release.
        for conn in conns:
            assert conn._sock is None or conn.should_reconnect()
        return True

    _install_real_pool(monkeypatch, pool, on_ping=ping)
    assert valkey_auth._refresh_token_in_place() is True
    disconnect_with_racing_checkout.racer.join(timeout=5)
    (raced,) = got
    assert raced._sock is None  # no old-token socket survived the swap
    assert raced.password == NEW_TOKEN
    assert pool.connection_kwargs["password"] == NEW_TOKEN
    assert valkey_auth._pool_verified is True
