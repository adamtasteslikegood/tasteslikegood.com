import threading
import sys
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


class _Pipeline:
    def __init__(self, client):
        self.client = client
        self.commands = []

    def setex(self, key, ttl, value):
        self.commands.append(("setex", key, ttl, value))
        return self

    def delete(self, key):
        self.commands.append(("delete", key))
        return self

    def execute(self):
        results = []
        with self.client.lock:
            for command in self.commands:
                if command[0] == "setex":
                    _, key, _ttl, value = command
                    self.client.values[key] = value
                    results.append(True)
                else:
                    _, key = command
                    results.append(int(self.client.values.pop(key, None) is not None))
        return results


class _RotationClient:
    def __init__(self):
        self.values = {"tlg:session:old": b"old"}
        self.lock = threading.Lock()
        self.partial_visible = threading.Event()
        self.release_delete = threading.Event()

    def setex(self, key, _ttl, value):
        with self.lock:
            self.values[key] = value
        self.partial_visible.set()
        self.release_delete.wait(timeout=2)
        return True

    def delete(self, key):
        with self.lock:
            return int(self.values.pop(key, None) is not None)

    def pipeline(self, transaction=True):
        assert transaction is True
        return _Pipeline(self)

    def snapshot(self):
        with self.lock:
            return set(self.values)


class _SessionManager(ValkeySessionMixin):
    def __init__(self, client):
        self.client = client


def test_session_rotation_never_exposes_old_and_new_tokens_together():
    client = _RotationClient()
    manager = _SessionManager(client)

    thread = threading.Thread(
        target=lambda: manager.rotate_session_token("old", "new", {"sub": "user"})
    )
    thread.start()

    saw_partial = client.partial_visible.wait(timeout=0.25)
    observed = client.snapshot()
    client.release_delete.set()
    thread.join(timeout=2)

    assert not (saw_partial and {"tlg:session:old", "tlg:session:new"}.issubset(observed))
    assert client.snapshot() == {"tlg:session:new"}
