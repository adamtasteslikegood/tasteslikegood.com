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


class IAMCredentialProvider(redis.CredentialProvider):
    """
    Redis credential provider that uses GCP IAM access tokens.

    Automatically refreshes tokens before they expire. Compatible with
    both Valkey and Redis on GCP Memorystore with IAM auth enabled.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._username = None
        self._token_expiry = 0

    def _refresh_token(self):
        """Obtain a fresh IAM access token from the default service account."""
        from google.auth import default
        from google.auth.transport.requests import Request

        creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())
        self._token = creds.token
        # Memorystore for Valkey IAM auth requires the SA email as username
        self._username = creds.service_account_email
        # Tokens last ~3600s; we refresh at 50 min to avoid edge cases
        self._token_expiry = time.time() + _TOKEN_REFRESH_INTERVAL
        logger.info("Refreshed IAM token for Valkey (user=%s)", self._username)

    def get_credentials(self):
        """Return (username, password) for Redis AUTH. Called by redis-py on each connection."""
        with self._lock:
            if not self._token or time.time() >= self._token_expiry:
                self._refresh_token()
        return self._username, self._token


def create_iam_redis_client(host: str, port: int = 6379) -> redis.StrictRedis:
    """
    Create a Redis client configured for GCP IAM authentication + TLS.

    Args:
        host: Memorystore private IP (e.g. '10.128.0.11')
        port: Memorystore port (default 6379)

    Returns:
        redis.StrictRedis configured with IAM credential provider and TLS
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

    logger.info(f"Created IAM-authenticated Valkey client for {host}:{port}")
    return client
