from flask_caching import Cache
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()

# Shared response cache. The backend is selected in create_app():
# Valkey/Redis when VALKEY_HOST or REDIS_URL is configured, otherwise an
# in-process SimpleCache. Consumers go through utils/cache_utils.py.
cache = Cache()
