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

import redis

from utils.valkey_config import resolve_valkey_config

logger = logging.getLogger(__name__)

# Token refresh interval (45 minutes — tokens last 60 min, refresh early)
_TOKEN_REFRESH_INTERVAL = 45 * 60

# Retry backoff after a failed token refresh (seconds). Starts at 30s,
# doubles on each consecutive failure, capped at the normal interval.
_RETRY_BASE = 30
_RETRY_MAX = _TOKEN_REFRESH_INTERVAL


def _get_iam_token():
    """Obtain a fresh IAM access token from the default service account."""
    from google.auth import default
    from google.auth.transport.requests import Request

    creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(Request())
    sa_email = getattr(creds, "service_account_email", None)
    return sa_email, creds.token


# ── Module-level state for token refresh ──────────────────────────
_lock = threading.Lock()
_current_client: redis.StrictRedis | None = None
_refresh_thread: threading.Thread | None = None


def _build_client(host: str, port: int) -> redis.StrictRedis:
    """Create a Redis client with a fresh IAM token (password-only, no username)."""
    email, token = _get_iam_token()
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
    return redis.StrictRedis(
        host=host,
        port=port,
        password=token,
        ssl=True,
        ssl_ca_data=ca_cert,
        decode_responses=False,
        protocol=2,
    )


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
    with _lock:
        if _current_client is None:
            return False

        _, new_token = _get_iam_token()

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

        return True


def _refresh_loop():
    """Background thread that refreshes the Valkey token before it expires.

    On failure, retries with exponential backoff (30s, 60s, 120s, ...)
    instead of waiting the full 45-minute interval, so the stale-token
    window stays as short as possible.
    """
    consecutive_failures = 0
    while True:
        if consecutive_failures == 0:
            time.sleep(_TOKEN_REFRESH_INTERVAL)
        else:
            backoff = min(_RETRY_BASE * (2 ** (consecutive_failures - 1)), _RETRY_MAX)
            logger.info(
                "Valkey token refresh retry in %ds (attempt %d)",
                backoff,
                consecutive_failures + 1,
            )
            time.sleep(backoff)

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
    Starts a background thread that refreshes the token every 45 minutes
    by updating the existing connection pool in-place.

    Returns None if the connection cannot be established, allowing the
    caller to fall back to a different session backend.
    """
    global _current_client, _refresh_thread

    try:
        client = _build_client(host, port)
        client.ping()
        logger.info("Valkey connection OK at %s:%s", host, port)

        with _lock:
            _current_client = client

        # Start background token refresh thread (daemon — dies with the process)
        if _refresh_thread is None or not _refresh_thread.is_alive():
            _refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
            _refresh_thread.start()
            logger.info("Started Valkey token refresh thread (every %ds)", _TOKEN_REFRESH_INTERVAL)

        return client
    except Exception as e:
        logger.error("Valkey IAM auth failed: %s — caller will fall back", e)
        return None
