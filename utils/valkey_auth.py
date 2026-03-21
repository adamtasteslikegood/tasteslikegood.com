"""
Valkey/Redis authentication utilities for GCP Memorystore.

Supports IAM authentication with automatic token refresh.
GCP IAM auth uses short-lived access tokens (1 hour) as the Redis password,
with TLS via Google-managed certificates.
"""

import logging
import threading
import time

import redis

logger = logging.getLogger(__name__)

# Token refresh interval (50 minutes — tokens last 60 min, refresh early)
_TOKEN_REFRESH_INTERVAL = 50 * 60


def _get_iam_credentials():
    """Obtain a fresh IAM access token from the default service account."""
    from google.auth import default
    from google.auth.transport.requests import Request

    creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(Request())
    return creds.service_account_email, creds.token


class IAMCredentialProvider(redis.CredentialProvider):
    """
    Redis credential provider that uses GCP IAM access tokens.

    Automatically refreshes tokens before they expire. For Memorystore
    for Valkey with IAM_AUTH, sends AUTH <token> (password only, no username).
    GCP docs: https://cloud.google.com/memorystore/docs/valkey/manage-iam-auth
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._token_expiry = 0

    def _refresh_token(self):
        _email, self._token = _get_iam_credentials()
        self._token_expiry = time.time() + _TOKEN_REFRESH_INTERVAL
        logger.info("Refreshed IAM token for Valkey (sa=%s)", _email)

    def get_credentials(self):
        """Return token string for AUTH command (password only, no username)."""
        with self._lock:
            if not self._token or time.time() >= self._token_expiry:
                self._refresh_token()
        return self._token


def create_iam_redis_client(host: str, port: int = 6379) -> redis.StrictRedis | None:
    """
    Create a Redis client configured for GCP IAM authentication + TLS.

    Returns None if the connection cannot be established, allowing the
    caller to fall back to a different session backend.

    Args:
        host: Memorystore private IP (e.g. '10.128.0.11')
        port: Memorystore port (default 6379)

    Returns:
        redis.StrictRedis configured with IAM auth and TLS, or None on failure
    """
    credential_provider = IAMCredentialProvider()

    client = redis.StrictRedis(
        host=host,
        port=port,
        credential_provider=credential_provider,
        ssl=True,
        ssl_cert_reqs="none",  # GCP-managed certs — no client cert validation needed
        decode_responses=False,  # Flask-Session stores binary data
    )

    try:
        client.ping()
        logger.info("Valkey connection OK at %s:%s", host, port)
        return client
    except Exception as e:
        logger.warning("Valkey CredentialProvider auth failed: %s", e)

    # Fallback: direct password auth (no CredentialProvider, no username)
    try:
        _email, token = _get_iam_credentials()
        client = redis.StrictRedis(
            host=host,
            port=port,
            password=token,
            ssl=True,
            ssl_cert_reqs="none",
            decode_responses=False,
        )
        client.ping()
        logger.info("Valkey connection OK (direct auth) at %s:%s", host, port)
        return client
    except Exception as e2:
        logger.error("Both Valkey auth methods failed: %s — falling back to SQLAlchemy sessions", e2)
        return None
