"""
Valkey connection-pool session lifecycle, IAM token rotation, and session-token storage helpers.

Extracted from ``utils.valkey_auth`` (KAN-463) so connection pool construction,
non-auth retry policy, in-place IAM token rotation across idle/in-use pooled
connections, traced refresh spans, and session-token CRUD helpers live in a
cohesive module while ``utils.valkey_auth`` remains the backwards-compatible
facade and state holder.
"""

import contextlib
import json
import logging
import sys
import time
from datetime import datetime
from typing import Any, TypeVar, cast

import redis
from ddtrace import tracer
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import AuthenticationError
from redis.retry import Retry

from utils.valkey_config import resolve_valkey_config

logger = logging.getLogger(__name__)

# redis-py command/connect retry policy for the IAM client. Same shape as
# redis-py 8's default (10 retries, 10 ms → 1 s jittered backoff) except that
# AuthenticationError is never retried — see _NoAuthRetry.
_REDIS_RETRIES = 10
_REDIS_RETRY_BASE = 0.01
_REDIS_RETRY_CAP = 1.0

_REFRESH_ROOT_SPAN = "valkey.token_refresh"

_T = TypeVar("_T")


def _get_iam_token() -> tuple[str | None, str, datetime | None]:
    """Obtain an IAM access token from the default service account.

    Returns ``(sa_email, token, expiry)``. ``expiry`` is what google-auth
    reports after the refresh — a naive UTC datetime, or None if unknown. On
    Cloud Run it can be well under 60 min away (KAN-268).
    """
    from google.auth import default
    from google.auth.transport.requests import Request

    creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(Request())
    sa_email = getattr(creds, "service_account_email", None)
    return sa_email, creds.token, getattr(creds, "expiry", None)


def _auth_mod() -> Any:
    """Return the loaded ``utils.valkey_auth`` module for dynamic state/monkeypatch resolution."""
    return sys.modules.get("utils.valkey_auth")


def _auth_attr(name: str, default: _T) -> _T:
    """Resolve ``name`` from ``utils.valkey_auth`` when loaded so test monkeypatches apply."""
    mod = _auth_mod()
    return cast(_T, getattr(mod, name, default)) if mod is not None else default


class _NoAuthRetry(Retry):
    """redis-py Retry that never retries an AuthenticationError.

    ``AuthenticationError`` subclasses ``ConnectionError``, so the default
    policy retried a rejected token ~10 times with backoff — ~4.4 s per
    command, at both the connect and the command level — before failing
    anyway (KAN-268). A rejected credential does not heal by waiting: fail
    fast and let the caller refresh. Every other error keeps the normal retry.
    """

    def call_with_retry(self, do, fail, is_retryable=None, with_failure_count=False):
        def _retryable(error):
            if isinstance(error, AuthenticationError):
                return False
            return is_retryable is None or is_retryable(error)

        return super().call_with_retry(
            do, fail, is_retryable=_retryable, with_failure_count=with_failure_count
        )


def _pool_lock(pool: Any) -> Any:
    """redis-py's pool lock, or a no-op for pools that have none (test fakes)."""
    lock = getattr(pool, "_lock", None)
    return lock if lock is not None else contextlib.nullcontext()


def _redis_retry() -> Retry:
    retry_cls = _auth_attr("_NoAuthRetry", _NoAuthRetry)
    base = _auth_attr("_REDIS_RETRY_BASE", _REDIS_RETRY_BASE)
    cap = _auth_attr("_REDIS_RETRY_CAP", _REDIS_RETRY_CAP)
    retries = _auth_attr("_REDIS_RETRIES", _REDIS_RETRIES)
    return retry_cls(
        ExponentialWithJitterBackoff(base=base, cap=cap),
        retries,
    )


def _build_client(host: str, port: int) -> tuple[redis.StrictRedis, datetime | None]:
    """Create a Redis client with a fresh IAM token (password-only, no username).

    Returns the client and the token's reported expiry.
    """
    get_token_fn = _auth_attr("_get_iam_token", _get_iam_token)
    email, token, expiry = get_token_fn()
    if email:
        logger.info("Creating Valkey client with fresh IAM token (sa=%s)", email)
    else:
        logger.info("Creating Valkey client with fresh IAM token (user credentials)")

    ca_cert = resolve_valkey_config().ca_cert
    retry_fn = _auth_attr("_redis_retry", _redis_retry)

    # Force RESP2 protocol: redis-py 8.x defaults to RESP3 which sends
    # HELLO 3 AUTH default <token> — the injected "default" username is
    # rejected by Memorystore IAM auth which expects password-only AUTH.
    client = redis.StrictRedis(
        host=host,
        port=port,
        password=token,
        ssl=True,
        ssl_ca_data=ca_cert,
        decode_responses=False,
        protocol=2,
        retry=retry_fn(),
    )
    return client, expiry


def _tag_refresh_span(key: str, value: str) -> None:
    """Tag the enclosing ``valkey.token_refresh`` span, if that is what is running."""
    active_tracer = _auth_attr("tracer", tracer)
    span = active_tracer.current_span()
    if span is not None and span.name == _REFRESH_ROOT_SPAN:
        span.set_tag(key, value)


def _refresh_token_in_place(force: bool = False) -> bool:
    """Refresh the IAM token on the EXISTING client's connection pool.

    Updates the password on the pool that Flask-Caching holds, drops idle
    connections so new ones authenticate with the fresh token, marks in-use
    connections to reconnect on release, and verifies with a PING outside the
    state lock.
    """
    auth = _auth_mod()
    if auth is None:
        return False

    active_tracer = getattr(auth, "tracer", tracer)

    with auth._lock:
        if auth._current_client is None:
            return False

        with active_tracer.trace("valkey.token_fetch"):
            _, new_token, new_expiry = auth._get_iam_token()

        client = auth._current_client
        pool = client.connection_pool

        token_changed = pool.connection_kwargs.get("password") != new_token
        _tag_refresh_span("token_changed", str(token_changed).lower())

        if not force and not token_changed and auth._pool_verified:
            auth._token_expiry = new_expiry
            auth._refresh_condition.notify_all()
            logger.info(
                "Valkey token unchanged; pool kept (next refresh in %ds)",
                int(auth._refresh_delay(new_expiry)),
            )
            return True

        auth._pool_verified = False

        with active_tracer.trace("valkey.pool_disconnect") as disconnect_span, _pool_lock(pool):
            pool.connection_kwargs["password"] = new_token
            idle = list(getattr(pool, "_available_connections", []))
            in_use = list(getattr(pool, "_in_use_connections", []))
            for conn in idle + in_use:
                conn.password = new_token
            disconnect_span.set_metric("valkey.in_use_marked", len(in_use))
            pool.disconnect(inuse_connections=False)
            for conn in in_use:
                conn.mark_for_reconnect()

        auth._refresh_generation += 1
        generation = auth._refresh_generation

    with active_tracer.trace("valkey.ping") as ping_span:
        ping_span.set_tag("valkey.includes_connect", "true")
        client.ping()

    with auth._lock:
        if auth._current_client is client and auth._refresh_generation == generation:
            auth._token_expiry = new_expiry
            auth._pool_verified = True
            auth._refresh_condition.notify_all()
    return True


def _traced_refresh(trigger: str) -> bool:
    """One expiry-driven refresh, wrapped in a ``valkey.token_refresh`` span."""
    auth = _auth_mod()
    active_tracer = getattr(auth, "tracer", tracer) if auth is not None else tracer
    time_mod = getattr(auth, "time", time) if auth is not None else time
    refresh_fn = (
        getattr(auth, "_refresh_token_in_place", _refresh_token_in_place)
        if auth is not None
        else _refresh_token_in_place
    )

    with active_tracer.trace(_REFRESH_ROOT_SPAN) as span:
        span.set_tag("trigger", trigger)
        cpu_start = time_mod.thread_time()
        wall_start = time_mod.monotonic()
        try:
            return bool(refresh_fn())
        finally:
            span.set_metric("thread_cpu_ms", (time_mod.thread_time() - cpu_start) * 1000)
            span.set_metric("wall_ms", (time_mod.monotonic() - wall_start) * 1000)


# Deleting the old key and writing the new one run as one server-side command,
# and the write happens only if the delete removed a live session (KAN-484).
# An existing destination key is never overwritten: rotation fails and the
# old session stays live.
_ROTATE_SESSION_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then
    return 0
end
if redis.call('DEL', KEYS[1]) == 1 then
    redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[1])
    return 1
end
return 0
"""


class ValkeySessionMixin:
    """Session token storage, refresh-token rotation, revocation, and TTL helpers."""

    SESSION_KEY_PREFIX = "tlg:session:"

    def _resolve_session_client(self) -> Any:
        client = getattr(self, "client", None)
        if client is not None:
            return client
        auth = _auth_mod()
        return getattr(auth, "_current_client", None) if auth is not None else None

    def _session_key(self, session_id: str) -> str:
        return f"{self.SESSION_KEY_PREFIX}{session_id}"

    def store_session_token(
        self, session_id: str, payload: dict[str, Any], ttl_seconds: int = 3600
    ) -> bool:
        """Persist a serialized session token payload in Valkey with a bounded TTL."""
        client = self._resolve_session_client()
        if client is None:
            return False
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        client.setex(self._session_key(session_id), max(1, int(ttl_seconds)), raw)
        return True

    def get_session_token(self, session_id: str) -> dict[str, Any] | None:
        """Load and decode a stored session token payload from Valkey."""
        client = self._resolve_session_client()
        if client is None:
            return None
        raw = client.get(self._session_key(session_id))
        if not raw:
            return None
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        data = json.loads(text)
        return data if isinstance(data, dict) else None

    def rotate_session_token(
        self,
        old_session_id: str,
        new_session_id: str,
        payload: dict[str, Any],
        ttl_seconds: int = 3600,
    ) -> bool:
        """Replace a live session token with a new one in a single atomic step.

        Returns False, and stores nothing, when the old session no longer
        exists: a revoked, expired or already-rotated id cannot be rotated,
        and replaying one id cannot produce several live sessions (KAN-484).
        Also returns False, changing nothing, when ``new_session_id`` already
        names a live session, so rotation never overwrites another session.
        Use ``store_session_token`` to create a session.
        """
        client = self._resolve_session_client()
        if client is None or not old_session_id:
            return False
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        new_key = self._session_key(new_session_id)
        ttl = max(1, int(ttl_seconds))
        if old_session_id == new_session_id:
            # Refresh in place, only if the session still exists.
            return bool(client.set(new_key, raw, ex=ttl, xx=True))
        return bool(
            client.eval(
                _ROTATE_SESSION_SCRIPT,
                2,
                self._session_key(old_session_id),
                new_key,
                ttl,
                raw,
            )
        )

    def revoke_session_token(self, session_id: str) -> bool:
        """Delete a session token from Valkey immediately."""
        client = self._resolve_session_client()
        if client is None:
            return False
        return bool(client.delete(self._session_key(session_id)))

    def get_session_ttl(self, session_id: str) -> int:
        """Return remaining TTL in seconds for ``session_id`` (-2 if missing, -1 if no expiry)."""
        client = self._resolve_session_client()
        if client is None:
            return -2
        return int(client.ttl(self._session_key(session_id)))
