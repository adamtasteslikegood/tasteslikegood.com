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
from datetime import datetime, timezone

import redis
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import AuthenticationError
from redis.retry import Retry

from utils.valkey_config import resolve_valkey_config

logger = logging.getLogger(__name__)

# Upper bound on the time between token refreshes. A freshly minted token
# lasts 60 min, but on Cloud Run the metadata server hands back a CACHED token
# with whatever lifetime it has left — observed 33–37 min (KAN-268) — so the
# actual sleep is derived from the token's expiry and this is only the cap.
_TOKEN_REFRESH_INTERVAL = 45 * 60

# Refresh this long before the token expires.
_EXPIRY_MARGIN = 5 * 60

# Never sleep less than this between successful refreshes. A token already
# inside the margin re-polls the (local, cheap) metadata server once a minute
# until it hands out a new one, instead of spinning.
_MIN_REFRESH_DELAY = 60

# When the credential reports no expiry we cannot know its lifetime, so
# refresh on a short interval rather than assume 60 min (that assumption is
# the KAN-268 bug). The metadata server serves cached tokens, so this is cheap.
_UNKNOWN_EXPIRY_DELAY = 10 * 60

# Auth-failure refresh attempts within this many seconds share the first
# attempt's result. This prevents request bursts from repeatedly fetching a
# token and disconnecting the pool during either recovery or an outage.
_AUTH_FAILURE_REFRESH_DEBOUNCE = 30

# Retry backoff after a failed token refresh (seconds). Starts at 30s,
# doubles on each consecutive failure, capped at the normal interval.
_RETRY_BASE = 30
_RETRY_MAX = _TOKEN_REFRESH_INTERVAL

# redis-py command/connect retry policy for the IAM client. Same shape as
# redis-py 8's default (10 retries, 10 ms → 1 s jittered backoff) except that
# AuthenticationError is never retried — see _NoAuthRetry.
_REDIS_RETRIES = 10
_REDIS_RETRY_BASE = 0.01
_REDIS_RETRY_CAP = 1.0


def _get_iam_token():
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


def _refresh_delay(expiry: datetime | None, now: datetime | None = None) -> float:
    """Seconds to sleep before the next token refresh.

    ``expiry - now - _EXPIRY_MARGIN``, clamped to
    [_MIN_REFRESH_DELAY, _TOKEN_REFRESH_INTERVAL]. google-auth reports expiry
    as a naive UTC datetime; aware datetimes are normalised to naive UTC so
    the two always compare. Unknown expiry → _UNKNOWN_EXPIRY_DELAY.
    """
    if expiry is None:
        return _UNKNOWN_EXPIRY_DELAY
    if expiry.tzinfo is not None:
        expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    elif now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    remaining = (expiry - now).total_seconds() - _EXPIRY_MARGIN
    return float(max(_MIN_REFRESH_DELAY, min(_TOKEN_REFRESH_INTERVAL, remaining)))


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


def _redis_retry() -> Retry:
    return _NoAuthRetry(
        ExponentialWithJitterBackoff(base=_REDIS_RETRY_BASE, cap=_REDIS_RETRY_CAP),
        _REDIS_RETRIES,
    )


# ── Module-level state for token refresh ──────────────────────────
_lock = threading.Lock()
# The refresh loop waits on this condition so any successful in-place refresh
# can wake it to recompute the deadline from the replacement token's expiry.
_refresh_condition = threading.Condition(_lock)
# Serializes refreshes initiated by AuthenticationError handlers. The main
# state lock cannot be held while calling _refresh_token_in_place(), because
# that function acquires it too.
_auth_failure_refresh_lock = threading.Lock()
_current_client: redis.StrictRedis | None = None
_refresh_thread: threading.Thread | None = None
_token_expiry: datetime | None = None


class _AuthFailureRefreshState:
    """Outcome of the latest auth-failure refresh attempt."""

    def __init__(self):
        self.monotonic: float | None = None
        self.result: bool | None = None


# This state is intentionally separate from client creation and scheduled
# refreshes: those events must not suppress recovery from a rejected credential.
_auth_failure_refresh_state = _AuthFailureRefreshState()


def _build_client(host: str, port: int) -> tuple[redis.StrictRedis, datetime | None]:
    """Create a Redis client with a fresh IAM token (password-only, no username).

    Returns the client and the token's reported expiry.
    """
    email, token, expiry = _get_iam_token()
    if email:
        logger.info("Creating Valkey client with fresh IAM token (sa=%s)", email)
    else:
        logger.info("Creating Valkey client with fresh IAM token (user credentials)")

    # Memorystore server certs chain to a Google-managed private CA that the
    # container's default trust store can't verify. Trust it explicitly via the
    # VALKEY_CA_CERT PEM (from Secret Manager); without it the TLS handshake
    # fails CERTIFICATE_VERIFY_FAILED and the caller silently degrades to an
    # in-process backend. ssl_ca_data=None means "use the system trust store"
    # (local/dev), so keep TLS verification on either way. Mirrors
    # server/valkey.ts (the Express side already does this correctly).
    # Read via the shared factory at client-build time (same timing as the
    # inline os.environ read this replaced — KAN-160).
    ca_cert = resolve_valkey_config().ca_cert

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
        retry=_redis_retry(),
    )
    return client, expiry


def _refresh_token_in_place() -> bool:
    """Refresh the IAM token on the EXISTING client's connection pool.

    This is critical because Flask-Caching holds a reference to the original
    client object. Creating a new client doesn't help — we must update the
    password on the pool it is already using, then drop stale connections so
    new ones authenticate with the fresh token.

    Returns:
        bool: True if a client was present and successfully refreshed,
        False if there is no current client (nothing to refresh).
    """
    global _token_expiry

    with _lock:
        if _current_client is None:
            return False

        _, new_token, new_expiry = _get_iam_token()

        # Work with a local reference while holding the lock to avoid races.
        client = _current_client
        pool = client.connection_pool

        # Update pool-level kwargs (used when creating NEW connections)
        pool.connection_kwargs["password"] = new_token

        # CRITICAL: also update EXISTING Connection objects — they cache password
        # independently and will re-auth with the stale token on reconnect
        for conn in list(getattr(pool, "_available_connections", [])):
            conn.password = new_token
        for conn in list(getattr(pool, "_in_use_connections", [])):
            conn.password = new_token

        # Close all sockets — next use triggers reconnect with the updated password
        pool.disconnect()

        # Verify the refreshed token works
        client.ping()

        _token_expiry = new_expiry
        _refresh_condition.notify_all()
        return True


def refresh_after_auth_failure() -> bool:
    """Refresh the IAM token because a command was rejected as unauthenticated.

    Called by utils/cache_utils when a cache operation raises
    ``AuthenticationError``. Returns True when the caller should retry the
    operation once (a refresh just succeeded, here or in another thread within
    the debounce window); False when there is no IAM client or the refresh
    failed. Never raises.
    """
    # The timestamp check and refresh decision must be single-flight. Without
    # this lock, a burst of failures can all observe the same stale timestamp,
    # then queue through _refresh_token_in_place() and repeatedly disconnect
    # the pool after the first caller already installed a fresh token.
    with _auth_failure_refresh_lock:
        with _lock:
            if _current_client is None:
                return False
            last = _auth_failure_refresh_state.monotonic
            last_result = _auth_failure_refresh_state.result
        if last is not None and time.monotonic() - last < _AUTH_FAILURE_REFRESH_DEBOUNCE:
            return bool(last_result)
        try:
            refreshed = _refresh_token_in_place()
        except Exception as e:
            with _lock:
                _auth_failure_refresh_state.monotonic = time.monotonic()
                _auth_failure_refresh_state.result = False
            logger.warning("Valkey token refresh after auth failure failed: %s", e)
            return False
        with _lock:
            _auth_failure_refresh_state.monotonic = time.monotonic()
            _auth_failure_refresh_state.result = refreshed
        if refreshed:
            logger.info("Valkey token refreshed after auth failure")
        return refreshed


def _refresh_loop():
    """Background thread that refreshes the Valkey token before it expires.

    Sleeps until ``_EXPIRY_MARGIN`` before the current token's reported expiry
    (capped at 45 min) rather than a fixed 45 min: the metadata server can
    return a cached token with only ~33 min left (KAN-268). On failure,
    retries with exponential backoff (30s, 60s, 120s, ...) so the stale-token
    window stays as short as possible.
    """
    consecutive_failures = 0
    while True:
        with _refresh_condition:
            if consecutive_failures == 0:
                delay = _refresh_delay(_token_expiry)
            else:
                delay = min(_RETRY_BASE * (2 ** (consecutive_failures - 1)), _RETRY_MAX)
                logger.info(
                    "Valkey token refresh retry in %ds (attempt %d)",
                    delay,
                    consecutive_failures + 1,
                )
            # A successful refresh from another thread updates _token_expiry and
            # notifies while holding the same lock. Wake without refreshing so
            # this loop recomputes its deadline from that replacement token.
            if _refresh_condition.wait(timeout=delay):
                consecutive_failures = 0
                continue

        try:
            refreshed = _refresh_token_in_place()
            if not refreshed:
                return  # no client to manage; stop the thread
            logger.info("Valkey token refreshed in-place successfully")
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.warning(
                "Valkey token refresh failed (attempt %d, will retry): %s",
                consecutive_failures,
                e,
            )


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
    global _current_client, _refresh_thread, _token_expiry

    try:
        client, expiry = _build_client(host, port)
        client.ping()
        logger.info("Valkey connection OK at %s:%s", host, port)

        with _lock:
            _current_client = client
            _token_expiry = expiry
            _auth_failure_refresh_state.monotonic = None
            _auth_failure_refresh_state.result = None

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
