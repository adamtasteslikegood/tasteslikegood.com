import logging
import os
import pickle

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

logger = logging.getLogger(__name__)


class _NullCache:
    """No-op fallback used when no cache backend (Valkey/Redis) is available."""

    def get(self, key):
        return None

    def set(self, key, value, timeout=None):
        pass

    def delete(self, key):
        pass

    def ping(self):
        return False


class ValkeyCache:
    """Cache backed by a Valkey/Redis client.

    Values are pickled so arbitrary Python objects (dicts, bytes, lists)
    round-trip transparently. Keys are plain strings.
    """

    def __init__(self, client):
        self._client = client

    def get(self, key):
        data = self._client.get(key)
        if data is None:
            return None
        return pickle.loads(data)

    def set(self, key, value, timeout=None):
        self._client.set(key, pickle.dumps(value), ex=timeout)

    def delete(self, key):
        self._client.delete(key)

    def ping(self):
        return bool(self._client.ping())


class CacheProxy:
    """Stable module-level handle whose backend is swapped at app init.

    Call sites import ``extensions.cache`` at module load, before
    ``create_app()`` has decided whether Valkey is reachable — the proxy
    lets ``init_cache()`` upgrade the backend later without re-imports.
    """

    def __init__(self):
        self._backend = _NullCache()

    @property
    def backend_name(self):
        return "valkey" if isinstance(self._backend, ValkeyCache) else "null"

    def configure(self, backend):
        self._backend = backend

    def get(self, key):
        return self._backend.get(key)

    def set(self, key, value, timeout=None):
        self._backend.set(key, value, timeout=timeout)

    def delete(self, key):
        self._backend.delete(key)

    def ping(self):
        return self._backend.ping()


db = SQLAlchemy()
migrate = Migrate()
cache = CacheProxy()


def init_cache():
    """Connect ``cache`` to Valkey when VALKEY_HOST is configured.

    Fault-tolerant by design: any failure leaves the null backend in place
    and the app serves requests without caching, exactly as before.

    Returns:
        str: The active backend name ("valkey" or "null").
    """
    host = os.environ.get("VALKEY_HOST")
    if not host:
        logger.info("VALKEY_HOST not set — response caching disabled (null backend)")
        return cache.backend_name

    port = int(os.environ.get("VALKEY_PORT", "6379"))
    auth_mode = os.environ.get("VALKEY_AUTH_MODE")

    try:
        if auth_mode == "iam":
            # GCP Memorystore: IAM token auth + TLS, with background refresh
            from utils.valkey_auth import create_iam_redis_client

            client = create_iam_redis_client(host, port)
        else:
            import redis

            client = redis.StrictRedis(
                host=host,
                port=port,
                password=os.environ.get("VALKEY_PASSWORD"),
                decode_responses=False,
            )
            client.ping()
    except Exception as e:
        logger.error("Valkey cache init failed: %s — continuing without cache", e)
        client = None

    if client is None:
        logger.warning("Valkey unavailable at %s:%s — response caching disabled", host, port)
        return cache.backend_name

    cache.configure(ValkeyCache(client))
    logger.info("Response cache connected to Valkey at %s:%s (auth=%s)", host, port, auth_mode)
    return cache.backend_name
