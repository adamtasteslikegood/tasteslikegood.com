"""KAN-268: cache_utils refreshes the IAM token and retries ONCE on AUTH failure.

The Memorystore IAM token can expire before the background refresh fires. The
safe helpers must turn the first AuthenticationError into one refresh + one
retry — never a loop — and fall back to their existing fault-tolerant path
when the refresh does not happen or the retry also fails.
"""

import sys
from pathlib import Path

import pytest
from redis.exceptions import AuthenticationError

sys.path.append(str(Path(__file__).resolve().parent.parent))

from utils import cache_utils  # noqa: E402


class _FakeCache:
    """Scriptable stand-in for extensions.cache: each call pops the next outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def _next(self, name, *args):
        self.calls.append((name, args))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get(self, key):
        return self._next("get", key)

    def set(self, key, value, timeout=None):
        return self._next("set", key, value, timeout)

    def delete(self, key):
        return self._next("delete", key)


@pytest.fixture
def refresh_calls(monkeypatch):
    """Patch the refresh hook; the list records calls, `.result` sets the return."""

    class _Hook(list):
        result = True

        def __call__(self):
            self.append(1)
            return self.result

    hook = _Hook()
    monkeypatch.setattr(cache_utils, "refresh_after_auth_failure", hook)
    return hook


def _install(monkeypatch, outcomes):
    fake = _FakeCache(outcomes)
    monkeypatch.setattr(cache_utils, "cache", fake)
    return fake


def test_get_refreshes_and_retries_once_on_auth_error(monkeypatch, refresh_calls):
    fake = _install(monkeypatch, [AuthenticationError("invalid password"), b"hit"])
    assert cache_utils.safe_get("vgc:img:1") == b"hit"
    assert len(fake.calls) == 2
    assert len(refresh_calls) == 1


def test_set_refreshes_and_retries_once_on_auth_error(monkeypatch, refresh_calls):
    fake = _install(monkeypatch, [AuthenticationError("invalid password"), True])
    cache_utils.safe_set("vgc:img:1", b"bytes", timeout=60)
    assert [c[0] for c in fake.calls] == ["set", "set"]
    assert len(refresh_calls) == 1


def test_delete_refreshes_and_retries_once_on_auth_error(monkeypatch, refresh_calls):
    fake = _install(monkeypatch, [AuthenticationError("invalid password"), 1])
    cache_utils.invalidate_recipe_image("r1")
    assert [c[0] for c in fake.calls] == ["delete", "delete"]
    assert len(refresh_calls) == 1


def test_auth_error_twice_retries_exactly_once_then_gives_up(monkeypatch, refresh_calls):
    """No infinite retry: a second AuthenticationError falls through to None."""
    fake = _install(
        monkeypatch,
        [AuthenticationError("invalid password")] * 5,
    )
    assert cache_utils.safe_get("vgc:img:1") is None
    assert len(fake.calls) == 2
    assert len(refresh_calls) == 1


def test_no_retry_when_refresh_did_not_happen(monkeypatch, refresh_calls):
    """No IAM client / refresh failed → one attempt, safe fallback."""
    refresh_calls.result = False
    fake = _install(monkeypatch, [AuthenticationError("invalid password"), b"never"])
    assert cache_utils.safe_get("vgc:img:1") is None
    assert len(fake.calls) == 1
    assert len(refresh_calls) == 1


def test_non_auth_errors_are_not_retried(monkeypatch, refresh_calls):
    fake = _install(monkeypatch, [RuntimeError("boom"), b"never"])
    assert cache_utils.safe_get("vgc:img:1") is None
    assert len(fake.calls) == 1
    assert refresh_calls == []


def test_happy_path_makes_one_call_and_no_refresh(monkeypatch, refresh_calls):
    fake = _install(monkeypatch, [b"hit"])
    assert cache_utils.safe_get("vgc:img:1") == b"hit"
    assert len(fake.calls) == 1
    assert refresh_calls == []
