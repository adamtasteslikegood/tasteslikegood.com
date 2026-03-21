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
    for Valkey with IAM_AUTH, sends AUTH <service-account-email> <token>.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._username = None
        self._token = None
        self._token_expiry = 0

    def _refresh_token(self):
        self._username, self._token = _get_iam_credentials()
        self._token_expiry = time.time() + _TOKEN_REFRESH_INTERVAL
        logger.info("Refreshed IAM token for Valkey (user=%s)", self._username)

    def get_credentials(self):
        """Return (username, password) tuple for AUTH command."""
        with self._lock:
            if not self._token or time.time() >= self._token_expiry:
                self._refresh_token()
        return self._username, self._token


def create_iam_redis_client(host: str, port: int = 6379) -> redis.StrictRedis:
    """
    Create a Redis client configured for GCP IAM authentication + TLS.

    Tries CredentialProvider first. If it fails (protocol error),
    falls back to direct username/password auth.

    Args:
        host: Memorystore private IP (e.g. '10.128.0.11')
        port: Memorystore port (default 6379)

    Returns:
        redis.StrictRedis configured with IAM auth and TLS
    """
    credential_provider = IAMCredentialProvider()

    # Try CredentialProvider approach first
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
        logger.info("Valkey connection OK (CredentialProvider auth) at %s:%s", host, port)
        return client
    except Exception as e:
        logger.warning("CredentialProvider auth failed (%s), trying direct auth", e)

    # Fallback: direct username/password (no CredentialProvider)
    username, token = _get_iam_credentials()
    client = redis.StrictRedis(
        host=host,
        port=port,
        username=username,
        password=token,
        ssl=True,
        ssl_cert_reqs="none",
        decode_responses=False,
    )

    try:
        client.ping()
        logger.info("Valkey connection OK (direct auth) at %s:%s", host, port)
    except Exception as e2:
        logger.error("Both Valkey auth methods failed: %s", e2)
        raise

    return client
