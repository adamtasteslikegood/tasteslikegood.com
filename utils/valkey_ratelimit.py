"""
Valkey token-refresh rate limiting, debounce/backoff scheduling, and fixed-window rate limiters.

Extracted from ``utils.valkey_auth`` (KAN-463) so expiry-margin scheduling,
auth-failure debounce gates, single-flight inline refresh throttling,
exponential retry backoff loops, and Valkey fixed-window / OAuth-nonce
helpers live in a dedicated module while ``utils.valkey_auth`` remains the
backwards-compatible facade.
"""

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, TypeVar, cast

logger = logging.getLogger(__name__)

# Upper bound on the time between token refreshes. A freshly minted token
# lasts 60 min, but on Cloud Run the metadata server hands back a CACHED token
# with whatever lifetime it has left — observed 33–37 min (KAN-268) — so the
# actual sleep is derived from the token's expiry and this is only the cap.
_TOKEN_REFRESH_INTERVAL = 45 * 60

# Refresh this long before the token expires. Inside this margin the next
# request that touches Valkey refreshes inline, on request CPU (KAN-318).
_EXPIRY_MARGIN = 5 * 60

# The background thread is only the idle safety net (KAN-318): it wakes this
# long before expiry, i.e. after the inline window has had its chance.
_IDLE_SAFETY_MARGIN = 2 * 60

# An inline attempt that does not move the expiry out of the margin or that
# fails is not retried inline for this long.
_INLINE_RETRY_INTERVAL = 60

# How long a request waits for another thread's refresh when the token has
# already EXPIRED.
_EXPIRED_TOKEN_WAIT = 5.0

# Never sleep less than this between successful refreshes.
_MIN_REFRESH_DELAY = 60

# When the credential reports no expiry we cannot know its lifetime, so
# refresh on a short interval rather than assume 60 min (KAN-268).
_UNKNOWN_EXPIRY_DELAY = 10 * 60

# Auth-failure refresh attempts within this many seconds share the first
# attempt's result.
_AUTH_FAILURE_REFRESH_DEBOUNCE = 30

# Retry backoff after a failed token refresh (seconds). Starts at 30s,
# doubles on each consecutive failure, capped at the normal interval.
_RETRY_BASE = 30
_RETRY_MAX = _TOKEN_REFRESH_INTERVAL

_T = TypeVar("_T")


def _auth_mod() -> Any:
    """Return the loaded ``utils.valkey_auth`` module for dynamic state/monkeypatch resolution."""
    return sys.modules.get("utils.valkey_auth")


def _auth_attr(name: str, default: _T) -> _T:
    """Resolve ``name`` from ``utils.valkey_auth`` when loaded so test monkeypatches apply."""
    mod = _auth_mod()
    return cast(_T, getattr(mod, name, default)) if mod is not None else default


class _AuthFailureRefreshState:
    """Outcome of the latest auth-failure refresh attempt."""

    def __init__(self) -> None:
        self.monotonic: float | None = None
        self.result: bool | None = None


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seconds_left(expiry: datetime) -> float:
    if expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    return (expiry - _utcnow_naive()).total_seconds()


def _refresh_delay(
    expiry: datetime | None, now: datetime | None = None, margin: float = _EXPIRY_MARGIN
) -> float:
    """Seconds to sleep before the next token refresh.

    ``expiry - now - margin`` (default ``_EXPIRY_MARGIN``), clamped to
    [_MIN_REFRESH_DELAY, _TOKEN_REFRESH_INTERVAL]. google-auth reports expiry
    as a naive UTC datetime; aware datetimes are normalised to naive UTC so
    the two always compare. Unknown expiry → _UNKNOWN_EXPIRY_DELAY.
    """
    unknown_delay = _auth_attr("_UNKNOWN_EXPIRY_DELAY", _UNKNOWN_EXPIRY_DELAY)
    min_delay = _auth_attr("_MIN_REFRESH_DELAY", _MIN_REFRESH_DELAY)
    max_interval = _auth_attr("_TOKEN_REFRESH_INTERVAL", _TOKEN_REFRESH_INTERVAL)

    if expiry is None:
        return float(unknown_delay)
    if expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    elif now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    remaining = (expiry - now).total_seconds() - margin
    return float(max(min_delay, min(max_interval, remaining)))


def refresh_after_auth_failure() -> bool:
    """Refresh the IAM token because a command was rejected as unauthenticated.

    Called by utils/cache_utils when a cache operation raises
    ``AuthenticationError``. Returns True when the caller should retry the
    operation once (a refresh just succeeded, here or in another thread within
    the debounce window); False when there is no IAM client or the refresh
    failed. Never raises.
    """
    auth = _auth_mod()
    if auth is None:
        return False

    time_mod = getattr(auth, "time", time)
    debounce = getattr(auth, "_AUTH_FAILURE_REFRESH_DEBOUNCE", _AUTH_FAILURE_REFRESH_DEBOUNCE)

    with auth._auth_failure_refresh_lock:
        with auth._lock:
            if auth._current_client is None:
                return False
            last = auth._auth_failure_refresh_state.monotonic
            last_result = auth._auth_failure_refresh_state.result
        if last is not None and time_mod.monotonic() - last < debounce:
            return bool(last_result)
        try:
            refreshed = bool(auth._refresh_token_in_place(force=True))
        except Exception as e:
            with auth._lock:
                auth._auth_failure_refresh_state.monotonic = time_mod.monotonic()
                auth._auth_failure_refresh_state.result = False
            logger.warning("Valkey token refresh after auth failure failed: %s", e)
            return False
        with auth._lock:
            auth._auth_failure_refresh_state.monotonic = time_mod.monotonic()
            auth._auth_failure_refresh_state.result = refreshed
        if refreshed:
            logger.info("Valkey token refreshed after auth failure")
        return refreshed


def ensure_fresh_token() -> None:
    """Refresh the IAM token inline when a request finds it inside the margin."""
    auth = _auth_mod()
    if auth is None:
        return

    expiry = auth._token_expiry
    if auth._current_client is None or expiry is None:
        return

    time_mod = getattr(auth, "time", time)
    margin = getattr(auth, "_EXPIRY_MARGIN", _EXPIRY_MARGIN)
    retry_interval = getattr(auth, "_INLINE_RETRY_INTERVAL", _INLINE_RETRY_INTERVAL)
    expired_wait = getattr(auth, "_EXPIRED_TOKEN_WAIT", _EXPIRED_TOKEN_WAIT)

    left = _seconds_left(expiry)
    if left > margin:
        return
    if left > 0:
        last = auth._last_inline_attempt
        if last is not None and time_mod.monotonic() - last < retry_interval:
            return
        acquired = auth._expiry_refresh_lock.acquire(blocking=False)
    else:
        acquired = auth._expiry_refresh_lock.acquire(timeout=expired_wait)
    if not acquired:
        return
    try:
        inline_fn = getattr(auth, "_inline_refresh_locked", _inline_refresh_locked)
        inline_fn()
    finally:
        auth._expiry_refresh_lock.release()


def _inline_refresh_locked() -> None:
    """The inline refresh itself; caller holds ``_expiry_refresh_lock``."""
    auth = _auth_mod()
    if auth is None:
        return

    time_mod = getattr(auth, "time", time)
    margin = getattr(auth, "_EXPIRY_MARGIN", _EXPIRY_MARGIN)
    retry_interval = getattr(auth, "_INLINE_RETRY_INTERVAL", _INLINE_RETRY_INTERVAL)

    expiry = auth._token_expiry
    if auth._current_client is None or expiry is None or _seconds_left(expiry) > margin:
        return
    last = auth._last_inline_attempt
    if last is not None and time_mod.monotonic() - last < retry_interval:
        return
    auth._last_inline_attempt = time_mod.monotonic()
    try:
        auth._traced_refresh(trigger="request")
    except Exception as e:
        logger.warning("Valkey inline token refresh failed: %s", e)


def _scheduled_refresh_single_flight() -> bool:
    """One safety-net refresh through the same gate as the inline path."""
    auth = _auth_mod()
    if auth is None:
        return False
    with auth._expiry_refresh_lock:
        return bool(auth._traced_refresh(trigger="scheduled"))


def _refresh_loop() -> None:
    """Idle safety net: refresh the token when no request did it inline."""
    auth = _auth_mod()
    if auth is None:
        return

    consecutive_failures = 0
    while True:
        with auth._refresh_condition:
            if consecutive_failures == 0:
                idle_margin = getattr(auth, "_IDLE_SAFETY_MARGIN", _IDLE_SAFETY_MARGIN)
                delay = auth._refresh_delay(auth._token_expiry, margin=idle_margin)
            else:
                delay = min(
                    auth._RETRY_BASE * (2 ** (consecutive_failures - 1)),
                    auth._RETRY_MAX,
                )
                logger.info(
                    "Valkey token refresh retry in %ds (attempt %d)",
                    delay,
                    consecutive_failures + 1,
                )
            if auth._refresh_condition.wait(timeout=delay):
                consecutive_failures = 0
                continue

        try:
            refreshed = auth._scheduled_refresh_single_flight()
            if not refreshed:
                return
            logger.info("Valkey token refreshed in-place successfully")
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.warning(
                "Valkey token refresh failed (attempt %d, will retry): %s",
                consecutive_failures,
                e,
            )


# INCR and the expiry run as one server-side command. Setting the TTL whenever
# the key has none (not only when the count is 1) also repairs a key left
# without one, so a counter can never pin a bucket or a lockout forever
# (KAN-483). A key that already has a TTL keeps it: the window stays fixed.
_INCR_WITH_TTL_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


def _incr_with_ttl(client: Any, key: str, window_seconds: int) -> int:
    return int(client.eval(_INCR_WITH_TTL_SCRIPT, 1, key, max(1, int(window_seconds))))


class ValkeyRateLimitMixin:
    """Fixed-window rate-limiting, lockout/backoff counters, and OAuth state/nonce helpers."""

    RATELIMIT_KEY_PREFIX = "tlg:ratelimit:"
    LOCKOUT_KEY_PREFIX = "tlg:lockout:"
    OAUTH_STATE_PREFIX = "tlg:oauth_state:"

    def _resolve_ratelimit_client(self) -> Any:
        client = getattr(self, "client", None)
        if client is not None:
            return client
        auth = _auth_mod()
        return getattr(auth, "_current_client", None) if auth is not None else None

    def check_fixed_window_rate_limit(
        self, bucket: str, limit: int, window_seconds: int = 60
    ) -> tuple[bool, int]:
        """Increment a fixed-window counter in Valkey and return ``(allowed, count)``."""
        client = self._resolve_ratelimit_client()
        if client is None:
            return True, 0
        key = f"{self.RATELIMIT_KEY_PREFIX}{bucket}"
        count = _incr_with_ttl(client, key, window_seconds)
        return count <= limit, count

    def record_auth_backoff_failure(
        self, subject: str, window_seconds: int = 300
    ) -> tuple[int, int]:
        """Record a failed auth attempt and return ``(failures, backoff_seconds)``."""
        client = self._resolve_ratelimit_client()
        if client is None:
            return 1, _RETRY_BASE
        key = f"{self.LOCKOUT_KEY_PREFIX}{subject}"
        failures = _incr_with_ttl(client, key, window_seconds)
        backoff = int(min(_RETRY_BASE * (2 ** max(0, failures - 1)), _RETRY_MAX))
        return failures, backoff

    def clear_auth_backoff(self, subject: str) -> bool:
        """Clear lockout/backoff counter for ``subject``."""
        client = self._resolve_ratelimit_client()
        if client is None:
            return False
        return bool(client.delete(f"{self.LOCKOUT_KEY_PREFIX}{subject}"))

    def cache_oauth_state_nonce(
        self, state: str, verifier: str | None = None, ttl_seconds: int = 600
    ) -> bool:
        """Store an OAuth state/PKCE verifier nonce in Valkey with a short TTL."""
        client = self._resolve_ratelimit_client()
        if client is None:
            return False
        key = f"{self.OAUTH_STATE_PREFIX}{state}"
        value = (verifier or "1").encode("utf-8")
        client.setex(key, max(1, int(ttl_seconds)), value)
        return True

    def consume_oauth_state_nonce(self, state: str) -> str | None:
        """Atomically read and delete an OAuth state nonce from Valkey."""
        client = self._resolve_ratelimit_client()
        if client is None:
            return None
        key = f"{self.OAUTH_STATE_PREFIX}{state}"
        raw = client.getdel(key)
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
