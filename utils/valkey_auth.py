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

logger = logging.getLogger(__name__)

# Token refresh interval (50 minutes — tokens last 60 min, refresh early)
_TOKEN_REFRESH_INTERVAL = 50 * 60


def _get_iam_token():
    """Obtain a fresh IAM access token from the default service account."""
    from google.auth import default
    from google.auth.transport.requests import Request

    creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(Request())
    return creds.service_account_email, creds.token


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
    logger.info("Creating Valkey client with fresh IAM token (sa=%s)", email)
    return redis.StrictRedis(
        host=host,
        port=port,
        password=token,
        ssl=True,
        ssl_cert_reqs="none",
        decode_responses=False,
    )


def _refresh_loop():
    """Background thread that refreshes the Valkey client before token expires."""
    global _current_client, _token_created_at
    while True:
        time.sleep(_TOKEN_REFRESH_INTERVAL)
        try:
            with _lock:
                if _client_host is None:
                    return  # no longer needed
                new_client = _build_client(_client_host, _client_port)
                new_client.ping()
                _current_client = new_client
                _token_created_at = time.time()
                logger.info("Valkey token refreshed successfully")
        except Exception as e:
            logger.warning("Valkey token refresh failed (will retry next cycle): %s", e)


def get_valkey_client() -> redis.StrictRedis | None:
    """Return the current Valkey client (refreshed automatically)."""
    return _current_client


def create_iam_redis_client(host: str, port: int = 6379) -> redis.StrictRedis | None:
    """
    Create a Redis client configured for GCP IAM authentication + TLS.

    Uses password-only auth (no username) as required by Memorystore for Valkey.
    Starts a background thread that refreshes the token every 50 minutes.

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
        logger.error("Valkey IAM auth failed: %s — falling back to SQLAlchemy sessions", e)
        return None
