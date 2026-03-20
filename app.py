"""
Tastes Like Good - Vegan Recipe Generator

A Flask application for viewing and generating vegan recipes using Google's Gemini AI models.
Features OAuth authentication, AI-powered recipe generation with schema validation,
and a recipe browsing interface.

Refactored into a modular architecture with:
- config.py: Configuration and environment loading
- validators/: Recipe validation logic
- services/: Business logic (Gemini, images, stock images, models)
- repositories/: Data persistence with file locking
- blueprints/: Route handlers (recipes, generation, API)
"""

import os
from datetime import timedelta

from flask import Flask, render_template, request, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# Import blueprints
from auth import auth_bp
from blueprints.api_bp import api_bp
from blueprints.auth_api_bp import auth_api_bp
from blueprints.collections_api_bp import collections_api_bp
from blueprints.generation_api_bp import generation_api_bp
from blueprints.generation_bp import generation_bp
from blueprints.recipes_api_bp import recipes_api_bp
from blueprints.recipes_bp import recipes_bp
from utils.logging_config import setup_logging
# Import session utilities
from utils.session_utils import get_or_create_session_id

# Initialize logger
logger = setup_logging()


def create_app():
    """
    Application factory for creating the Flask app.

    Returns:
        Flask: Configured Flask application
    """
    app = Flask(__name__)

    # Trust proxy headers (X-Forwarded-Host, X-Forwarded-Proto, etc.)
    # so url_for(_external=True) generates correct public URLs
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    # Configure secret key for sessions
    # In production, use a persistent secret key from environment variables
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

    # Configure Database
    from config import SQLALCHEMY_DATABASE_URI, SQLALCHEMY_TRACK_MODIFICATIONS

    app.config["SQLALCHEMY_DATABASE_URI"] = SQLALCHEMY_DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = SQLALCHEMY_TRACK_MODIFICATIONS

    # Initialize extensions
    from extensions import db, migrate, sess

    db.init_app(app)
    migrate.init_app(app, db)

    # Server-side sessions stored in PostgreSQL via Flask-Session.
    # Replaces default cookie-based sessions to:
    #  - Survive container restarts (data in DB, not cookie)
    #  - Remove 4KB cookie size limit
    #  - Keep OAuth tokens server-side (security)
    app.config['SESSION_TYPE'] = 'sqlalchemy'
    app.config['SESSION_SQLALCHEMY'] = db
    app.config['SESSION_SQLALCHEMY_TABLE'] = 'flask_sessions'
    app.config['SESSION_PERMANENT'] = True
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=14)
    app.config['SESSION_KEY_PREFIX'] = 'vg:'
    app.config['SESSION_CLEANUP_N_REQUESTS'] = 100
    app.config['SESSION_COOKIE_NAME'] = 'vg_session'
    app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('FLASK_SECRET_KEY'))
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    sess.init_app(app)

    # Import models so they are registered with SQLAlchemy
    # This must be done after db is created / configured
    with app.app_context():
        pass

    # Production should continue using Flask-Migrate/Alembic as the primary path.

    # ####### DEVELOPMENT NOTE: If you want to quickly create tables without running migrations #######
    # # ***** (for example, for local development or testing), you can use db.create_all().  *****
    # # For local development or quick testing, you can use db.create_all() to create tables without running migrations.
    # # Uncomment the following lines if you want to use create_all() for quick local development without migrations.
    #
    # def create_tables_with_retry(app, db, attempts=5, delay_seconds=2):
    #     """
    #     Create database tables with retry logic.
    #
    #     Use only for temporary bootstrap scenarios (for example,
    #     local development, first-run smoke tests, or controlled deployment recovery).
    #     Prefer schema migrations for normal operation.
    #     """
    #     import time
    #     for attempt in range(attempts):
    #         try:
    #             db.create_all()
    #             app.logger.info("db.create_all() succeeded")
    #             return
    #         except Exception as e:
    #             app.logger.warning(f"db.create_all() attempt {attempt +1} failed: {e}")
    #             if attempt < attempts -1:
    #                 time.sleep(delay_seconds)
    #             else:
    #                 app.logger.error(f"db.create_all() gave up after {attempts} attempts")
    # # Uncomment the following line if you want to use create_all() for quick local development without migrations.
    # create_tables_with_retry(app, db)
    # # Note: In production, rely on proper migrations instead of create_all() to manage schema changes.

    # Configure CORS to allow Angular frontend to call this API
    # Allow both dev (4200, 8080) and production origins
    cors_origins = [
        "http://localhost:3000",  # Angular dev server (ng serve)
        "http://localhost:4200",  # Angular dev server (legacy)
        "http://localhost:8080",  # Express server dev
        "http://127.0.0.1:3000",
        "http://127.0.0.1:4200",
        "http://127.0.0.1:8080",
    ]

    # Add production origins from environment if set
    if os.environ.get("PRODUCTION_ORIGIN"):
        cors_origins.append(os.environ.get("PRODUCTION_ORIGIN"))

    # Always allow the production domains
    cors_origins.extend([
        "https://www.tasteslikegood.org",
        "https://tasteslikegood.org",
    ])

    CORS(
        app,
        origins=cors_origins,
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    )

    # Session middleware to ensure all users have session IDs
    @app.before_request
    def ensure_session_id():
        """
        Ensure all requests have a session ID for anonymous user tracking.

        This runs before every request and creates a session ID if one doesn't exist.
        For authenticated users, the session_id coexists with their user_id.
        """
        # Skip session creation for static files to avoid unnecessary overhead
        if request.endpoint and "static" in request.endpoint:
            return None

        # Ensure session has an ID for tracking
        get_or_create_session_id()
        return None

    # Register blueprints
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(auth_api_bp)  # /api/auth/* endpoints
    app.register_blueprint(recipes_bp)  # No prefix - includes '/' and '/recipe/*'
    app.register_blueprint(generation_bp)  # No prefix - includes '/generate_recipe'
    app.register_blueprint(api_bp)  # Prefix '/api' set in blueprint
    app.register_blueprint(recipes_api_bp)  # Prefix '/api/recipes' set in blueprint
    app.register_blueprint(collections_api_bp)  # Prefix '/api/collections' set in blueprint
    app.register_blueprint(generation_api_bp)  # Prefix '/api' — JSON generation endpoints

    # Error handlers
    @app.errorhandler(404)
    def not_found(error):
        """Handle 404 errors with a friendly message."""
        return render_template("404.html"), 404

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        """Log and handle unexpected exceptions."""
        logger.exception(f"Unexpected error: {error}")
        return render_template("500.html"), 500

    return app


# Create the app instance
app = create_app()

if __name__ == "__main__":
    # Run the development server
    # In production, use a WSGI server like gunicorn or uwsgi
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
