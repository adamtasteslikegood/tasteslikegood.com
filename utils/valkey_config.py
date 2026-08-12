"""Single source of truth for Valkey/Redis environment configuration.

Every Valkey-related environment variable is read HERE and nowhere else
(KAN-160). Before this factory existed, config.py, app.py, and
utils/valkey_auth.py each hand-mirrored their own subset of the env vars,
which is how the silent SimpleCache degradation (#222/#3176) and the
migrate job's missing VALKEY_CA_CERT (KAN-136) slipped through. Any new
surface that needs Valkey settings must call resolve_valkey_config()
instead of touching os.environ.

Variables consolidated:
- VALKEY_HOST: Memorystore private IP (e.g. 10.128.0.11)
- VALKEY_PORT: defaults to 6379; invalid values warn loudly and default
- VALKEY_AUTH_MODE: 'iam' for GCP IAM auth (prod default), 'password'
  for static password auth
- VALKEY_PASSWORD: static password when VALKEY_AUTH_MODE='password'
- VALKEY_CA_CERT: PEM for the Google-managed private CA (TLS trust)
- REDIS_URL: legacy — full redis:// URL for local dev; used only when
  VALKEY_HOST is unset

Cache-backend priority contract (implemented by app.create_app, pinned by
tests/test_valkey_config.py): VALKEY_HOST > REDIS_URL > in-process
SimpleCache.
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_VALKEY_PORT = 6379


@dataclass(frozen=True)
class ValkeyConfig:
    """Immutable snapshot of every Valkey/Redis setting."""

    host: str | None
    port: int
    auth_mode: str
    password: str | None
    ca_cert: str | None
    redis_url: str | None


def _parse_port(raw: str | None) -> int:
    """Validate VALKEY_PORT, warning loudly instead of degrading silently.

    A non-numeric or out-of-range value names the bad value in the log and
    falls back to the explicit default 6379 — never a quiet crash or a
    quietly-wrong port.
    """
    if raw is None:
        return DEFAULT_VALKEY_PORT
    try:
        port = int(raw)
    except ValueError:
        logger.warning(
            "Invalid VALKEY_PORT %r: not an integer — using default %d",
            raw,
            DEFAULT_VALKEY_PORT,
        )
        return DEFAULT_VALKEY_PORT
    if not 1 <= port <= 65535:
        logger.warning(
            "Invalid VALKEY_PORT %r: outside 1-65535 — using default %d",
            raw,
            DEFAULT_VALKEY_PORT,
        )
        return DEFAULT_VALKEY_PORT
    return port


def resolve_valkey_config() -> ValkeyConfig:
    """Read all Valkey-related env vars in one place and return a snapshot.

    Reads the environment fresh on every call; callers own the resolution
    timing (config.py snapshots at import time, valkey_auth resolves at
    client-build time).
    """
    return ValkeyConfig(
        host=os.getenv("VALKEY_HOST"),
        port=_parse_port(os.getenv("VALKEY_PORT")),
        auth_mode=os.getenv("VALKEY_AUTH_MODE", "iam"),
        password=os.getenv("VALKEY_PASSWORD"),
        ca_cert=os.getenv("VALKEY_CA_CERT"),
        redis_url=os.getenv("REDIS_URL"),
    )
