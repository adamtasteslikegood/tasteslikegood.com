"""
Valkey/Redis authentication utilities for GCP Memorystore.

Supports IAM authentication with automatic token refresh.
GCP IAM auth uses short-lived access tokens (1 hour) as the password,
with TLS via Google-managed certificates.

Ref: https://cloud.google.com/memorystore/docs/valkey/manage-iam-auth
"""

import logging
import os
import threading
import time

import redis

logger = logging.getLogger(__name__)

# Token refresh interval (45 minutes — tokens last 60 min, refresh early)
_TOKEN_REFRESH_INTERVAL = 45 * 60


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
_client_host: str | None = None
_client_port: int = 6379
_token_created_at: float = 0
_refresh_thread: threading.Thread | None = None


def _build_client(host: str, port: int) -> redis.StrictRedis:
    """Create a Redis client with a fresh IAM token (password-only, no username)."""
    email, token = _get_iam_token()
    if email:
        logger.info("Creating Valkey client with fresh IAM token (sa=%s)", email)
    else:
        logger.info("Creating Valkey client with fresh IAM token (user credentials)")

    # Memorystore server certs chain to a Google-managed private CA that the
    # system trust store cannot verify. VALKEY_CA_CERT carries that CA's PEM
    # (same secret the Express service uses — see cookbook cloudbuild.yaml).
    # Without it we still connect over TLS to the private VPC IP, but skip
    # chain verification rather than fail and silently lose the cache.
    ssl_kwargs: dict = {"ssl": True}
    ca_cert = os.environ.get("VALKEY_CA_CERT")
    if ca_cert:
        ssl_kwargs["ssl_ca_data"] = ca_cert
    else:
        logger.warning(
            "VALKEY_CA_CERT not set — connecting to Valkey with TLS but without "
            "certificate verification. Wire the VALKEY_CA_CERT secret to enable it."
        )
        ssl_kwargs["ssl_cert_reqs"] = "none"

    return redis.StrictRedis(
        host=host,
        port=port,
        password=token,
        decode_responses=False,
        **ssl_kwargs,
    )


def _refresh_token_in_place() -> bool:
    """Refresh the IAM token on the EXISTING client's connection pool.

    This is critical because Flask-Session and Flask-Caching hold references
    to the original client object. Creating a new client doesn't help — we
    must update the password on the pool they're already using, then drop
    stale connections so new ones authenticate with the fresh token.

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
    """Background thread that refreshes the Valkey token before it expires."""
    global _token_created_at
    while True:
        time.sleep(_TOKEN_REFRESH_INTERVAL)
        try:
            refreshed = _refresh_token_in_place()
            if not refreshed:
                return  # no client to manage; stop the thread
            _token_created_at = time.time()
            logger.info("Valkey token refreshed in-place successfully")
        except Exception as e:
            logger.warning("Valkey token refresh failed (will retry next cycle): %s", e)


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
    global _current_client, _client_host, _client_port, _token_created_at, _refresh_thread

    try:
        client = _build_client(host, port)
        client.ping()
        logger.info("Valkey connection OK at %s:%s", host, port)

        with _lock:
            _current_client = client
            _client_host = host
            _client_port = port
            _token_created_at = time.time()

        # Start background token refresh thread (daemon — dies with the process)
        if _refresh_thread is None or not _refresh_thread.is_alive():
            _refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
            _refresh_thread.start()
            logger.info("Started Valkey token refresh thread (every %ds)", _TOKEN_REFRESH_INTERVAL)

        return client
    except Exception as e:
        logger.error("Valkey IAM auth failed: %s — caller falls back to its non-Valkey path", e)
        return None
