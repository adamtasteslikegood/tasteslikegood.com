import os
import sys
import threading

import pytest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils.valkey_ratelimit import ValkeyRateLimitMixin
from utils.valkey_sessions import ValkeySessionMixin


class _NonceManager(ValkeyRateLimitMixin):
    def __init__(self, client):
        self.client = client


class _ConcurrentNonceClient:
    def __init__(self):
        self.value = b"verifier"
        self.barrier = threading.Barrier(2)
        self.lock = threading.Lock()

    def get(self, _key):
        value = self.value
        self.barrier.wait(timeout=2)
        return value

    def delete(self, _key):
        self.value = None
        return 1

    def getdel(self, _key):
        with self.lock:
            value = self.value
            self.value = None
            return value


def test_oauth_state_nonce_is_consumed_exactly_once_under_concurrency():
    manager = _NonceManager(_ConcurrentNonceClient())
    results = []

    threads = [
        threading.Thread(target=lambda: results.append(manager.consume_oauth_state_nonce("state")))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert sorted(results, key=lambda value: value is not None) == [None, "verifier"]


class _ScriptClient:
    """In-memory stand-in that runs the two Lua scripts' logic under one lock.

    It checks the Python side (keys, arguments, return handling). The scripts
    themselves run against a real server in the live test below.
    """

    def __init__(self, values=None, ttls=None):
        self.values = dict(values or {})
        self.ttls = dict(ttls or {})
        self.lock = threading.Lock()

    def eval(self, script, numkeys, *args):
        keys, argv = args[:numkeys], args[numkeys:]
        with self.lock:
            if "INCR" in script:
                key = keys[0]
                self.values[key] = int(self.values.get(key, 0)) + 1
                if self.ttls.get(key, -1) < 0:
                    self.ttls[key] = int(argv[0])
                return self.values[key]
            old_key, new_key = keys
            if self.values.pop(old_key, None) is None:
                return 0
            self.ttls.pop(old_key, None)
            self.values[new_key] = argv[1]
            self.ttls[new_key] = int(argv[0])
            return 1

    def set(self, key, value, ex=None, xx=False):
        with self.lock:
            if xx and key not in self.values:
                return None
            self.values[key] = value
            self.ttls[key] = ex
            return True


class _RateManager(ValkeyRateLimitMixin):
    def __init__(self, client):
        self.client = client


def test_fixed_window_counter_sets_ttl_on_first_hit_and_keeps_it():
    client = _ScriptClient()
    manager = _RateManager(client)

    assert manager.check_fixed_window_rate_limit("b", limit=2, window_seconds=60) == (True, 1)
    client.ttls["tlg:ratelimit:b"] = 30  # time passes; window must not restart
    assert manager.check_fixed_window_rate_limit("b", limit=2, window_seconds=60) == (True, 2)
    assert manager.check_fixed_window_rate_limit("b", limit=2, window_seconds=60) == (False, 3)
    assert client.ttls["tlg:ratelimit:b"] == 30


def test_counter_without_ttl_is_repaired():
    # KAN-483: a key that exists with no expiry must not stay exhausted forever.
    client = _ScriptClient(values={"tlg:lockout:u": 5}, ttls={"tlg:lockout:u": -1})
    failures, _backoff = _RateManager(client).record_auth_backoff_failure("u", window_seconds=300)

    assert failures == 6
    assert client.ttls["tlg:lockout:u"] == 300


class _SessionManager(ValkeySessionMixin):
    def __init__(self, client):
        self.client = client


def test_rotation_replaces_live_session():
    client = _ScriptClient(values={"tlg:session:old": b"old"})
    assert _SessionManager(client).rotate_session_token("old", "new", {"sub": "user"}) is True
    assert set(client.values) == {"tlg:session:new"}


def test_replayed_old_session_cannot_rotate_twice():
    # KAN-484: replaying one old id must not yield several live sessions.
    client = _ScriptClient(values={"tlg:session:old": b"old"})
    manager = _SessionManager(client)
    results = []
    threads = [
        threading.Thread(
            target=lambda n=n: results.append(
                manager.rotate_session_token("old", f"new{n}", {"sub": "user"})
            )
        )
        for n in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert sorted(results) == [False, False, False, False, True]
    assert len(client.values) == 1


def test_revoked_or_missing_session_cannot_rotate():
    client = _ScriptClient()
    manager = _SessionManager(client)

    assert manager.rotate_session_token("gone", "new", {"sub": "user"}) is False
    assert manager.rotate_session_token("", "new", {"sub": "user"}) is False
    assert manager.rotate_session_token("gone", "gone", {"sub": "user"}) is False
    assert client.values == {}


def test_same_id_rotation_refreshes_live_session():
    client = _ScriptClient(values={"tlg:session:s": b"old"})
    assert _SessionManager(client).rotate_session_token("s", "s", {"sub": "user"}, 120) is True
    assert client.ttls["tlg:session:s"] == 120


@pytest.mark.skipif(not os.environ.get("VALKEY_TEST_URL"), reason="needs VALKEY_TEST_URL")
def test_lua_scripts_against_live_server():
    import redis

    client = redis.Redis.from_url(os.environ["VALKEY_TEST_URL"])
    client.delete("tlg:ratelimit:live", "tlg:session:a", "tlg:session:b", "tlg:session:c")
    rate = _RateManager(client)
    sessions = _SessionManager(client)

    assert rate.check_fixed_window_rate_limit("live", limit=5, window_seconds=60) == (True, 1)
    assert 0 < client.ttl("tlg:ratelimit:live") <= 60
    client.persist("tlg:ratelimit:live")  # simulate a key left with no expiry
    assert rate.check_fixed_window_rate_limit("live", limit=5, window_seconds=60) == (True, 2)
    assert 0 < client.ttl("tlg:ratelimit:live") <= 60

    sessions.store_session_token("a", {"sub": "u"})
    assert sessions.rotate_session_token("a", "b", {"sub": "u"}, 90) is True
    assert sessions.rotate_session_token("a", "c", {"sub": "u"}) is False
    assert client.exists("tlg:session:a", "tlg:session:c") == 0
    assert 0 < client.ttl("tlg:session:b") <= 90
