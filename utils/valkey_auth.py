"""
Valkey/Redis authentication utilities for GCP Memorystore.

Supports IAM authentication with automatic token refresh.
GCP IAM auth uses short-lived access tokens (1 hour) as the password,
with TLS via Google-managed certificates.

Ref: https://cloud.google.com/memorystore/docs/valkey/manage-iam-auth
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any

import redis
from ddtrace import tracer

from utils.valkey_config import resolve_valkey_config
from utils.valkey_ratelimit import (
    _AUTH_FAILURE_REFRESH_DEBOUNCE,
    _EXPIRED_TOKEN_WAIT,
    _EXPIRY_MARGIN,
    _IDLE_SAFETY_MARGIN,
    _INLINE_RETRY_INTERVAL,
    _MIN_REFRESH_DELAY,
    _RETRY_BASE,
    _RETRY_MAX,
    _TOKEN_REFRESH_INTERVAL,
    _UNKNOWN_EXPIRY_DELAY,
    ValkeyRateLimitMixin,
    _AuthFailureRefreshState,
    _inline_refresh_locked,
    _refresh_delay,
    _refresh_loop,
    _scheduled_refresh_single_flight,
    _seconds_left,
    _utcnow_naive,
    ensure_fresh_token,
    refresh_after_auth_failure,
)
from utils.valkey_sessions import (
    _REDIS_RETRIES,
    _REDIS_RETRY_BASE,
    _REDIS_RETRY_CAP,
    _REFRESH_ROOT_SPAN,
    ValkeySessionMixin,
    _build_client,
    _get_iam_token,
    _NoAuthRetry,
    _pool_lock,
    _redis_retry,
    _refresh_token_in_place,
    _tag_refresh_span,
    _traced_refresh,
)

logger = logging.getLogger(__name__)


# ── Module-level state for token refresh ──────────────────────────
_lock = threading.Lock()
# The refresh loop waits on this condition so any successful in-place refresh
# can wake it to recompute the deadline from the replacement token's expiry.
_refresh_condition = threading.Condition(_lock)
# Serializes refreshes initiated by AuthenticationError handlers. The main
# state lock cannot be held while calling _refresh_token_in_place(), because
# that function acquires it too.
_auth_failure_refresh_lock = threading.Lock()
# Single-flight gate for expiry-driven refreshes, inline (request path) and
# scheduled (idle safety net) alike, so the two never both refresh one expiry
# (KAN-318). Taken BEFORE _lock, never while holding it.
_expiry_refresh_lock = threading.Lock()
# time.monotonic() of the last inline attempt (see _INLINE_RETRY_INTERVAL).
_last_inline_attempt: float | None = None
_current_client: redis.StrictRedis | None = None
_refresh_thread: threading.Thread | None = None
_token_expiry: datetime | None = None
# Bumped under _lock each time a refresh installs a token on the pool, so a
# refresh whose PING finishes after a newer one does not publish a stale expiry.
_refresh_generation = 0
# True only once a PING has succeeded with the token the pool currently holds.
# A refresh clears it before installing a candidate token, so a failed PING
# leaves it False and the unchanged-token shortcut cannot vouch for a token
# that never verified (KAN-268, Backend #347 review).
_pool_verified = False

# This state is intentionally separate from client creation and scheduled
# refreshes: those events must not suppress recovery from a rejected credential.
_auth_failure_refresh_state = _AuthFailureRefreshState()


def get_valkey_client() -> redis.StrictRedis | None:
    """Return the current Valkey client (refreshed automatically)."""
    return _current_client


def create_iam_redis_client(host: str, port: int = 6379) -> redis.StrictRedis | None:
    """
    Create a Redis client configured for GCP IAM authentication + TLS.

    Uses password-only auth (no username) as required by Memorystore for Valkey.
    Starts a background thread that refreshes the token shortly before it
    expires by updating the existing connection pool in-place.

    Returns None if the connection cannot be established, allowing the
    caller to fall back to a different session backend.
    """
    global _current_client, _refresh_thread, _token_expiry, _pool_verified, _last_inline_attempt

    try:
        client, expiry = _build_client(host, port)
        client.ping()
        logger.info("Valkey connection OK at %s:%s", host, port)

        with _lock:
            _current_client = client
            _token_expiry = expiry
            _pool_verified = True  # the PING above used this client's token
            _auth_failure_refresh_state.monotonic = None
            _auth_failure_refresh_state.result = None
            _last_inline_attempt = None
            # Wake an already-running refresh loop so it recomputes its
            # deadline from the new token's expiry.
            _refresh_condition.notify_all()

        # Start background token refresh thread (daemon — dies with the process)
        if _refresh_thread is None or not _refresh_thread.is_alive():
            _refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
            _refresh_thread.start()
            logger.info(
                "Started Valkey token refresh thread (next refresh in %ds)",
                int(_refresh_delay(expiry)),
            )

        return client
    except Exception as e:
        logger.error("Valkey IAM auth failed: %s — caller will fall back", e)
        return None


class ValkeyAuthManager(ValkeySessionMixin, ValkeyRateLimitMixin):
    """Unified Valkey IAM auth, session storage, and rate-limiting facade."""

    def __init__(self, client: Any = None) -> None:
        self._explicit_client = client

    @property
    def client(self) -> Any:
        return self._explicit_client if self._explicit_client is not None else _current_client

    def ensure_fresh_token(self) -> None:
        ensure_fresh_token()

    def refresh_after_auth_failure(self) -> bool:
        return refresh_after_auth_failure()


_default_auth_manager: ValkeyAuthManager | None = None


def get_valkey_auth_manager() -> ValkeyAuthManager:
    """Return the process-wide ``ValkeyAuthManager`` singleton."""
    global _default_auth_manager
    if _default_auth_manager is None:
        _default_auth_manager = ValkeyAuthManager()
    return _default_auth_manager


__all__ = [
    "ValkeyAuthManager",
    "ValkeyRateLimitMixin",
    "ValkeySessionMixin",
    "_AUTH_FAILURE_REFRESH_DEBOUNCE",
    "_AuthFailureRefreshState",
    "_EXPIRED_TOKEN_WAIT",
    "_EXPIRY_MARGIN",
    "_IDLE_SAFETY_MARGIN",
    "_INLINE_RETRY_INTERVAL",
    "_MIN_REFRESH_DELAY",
    "_NoAuthRetry",
    "_REDIS_RETRIES",
    "_REDIS_RETRY_BASE",
    "_REDIS_RETRY_CAP",
    "_REFRESH_ROOT_SPAN",
    "_RETRY_BASE",
    "_RETRY_MAX",
    "_TOKEN_REFRESH_INTERVAL",
    "_UNKNOWN_EXPIRY_DELAY",
    "_auth_failure_refresh_lock",
    "_auth_failure_refresh_state",
    "_build_client",
    "_current_client",
    "_expiry_refresh_lock",
    "_get_iam_token",
    "_inline_refresh_locked",
    "_last_inline_attempt",
    "_lock",
    "_pool_lock",
    "_pool_verified",
    "_redis_retry",
    "_refresh_condition",
    "_refresh_delay",
    "_refresh_generation",
    "_refresh_loop",
    "_refresh_thread",
    "_refresh_token_in_place",
    "_scheduled_refresh_single_flight",
    "_seconds_left",
    "_tag_refresh_span",
    "_token_expiry",
    "_traced_refresh",
    "_utcnow_naive",
    "create_iam_redis_client",
    "ensure_fresh_token",
    "get_valkey_auth_manager",
    "get_valkey_client",
    "refresh_after_auth_failure",
    "resolve_valkey_config",
    "time",
    "tracer",
]
