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

from flask import Flask, render_template, request, session

# Import blueprints
from auth import auth_bp
from blueprints.api_bp import api_bp
from blueprints.generation_bp import generation_bp
from blueprints.recipes_bp import recipes_bp

# Import session utilities
from utils.session_utils import get_or_create_session_id


from utils.logging_config import setup_logging
import logging

# Initialize logger
logger = setup_logging()

def create_app():
    """
    Application factory for creating the Flask app.

    Returns:
        Flask: Configured Flask application
    """
    app = Flask(__name__)

    # Configure secret key for sessions
    # In production, use a persistent secret key from environment variables
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

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
    app.register_blueprint(recipes_bp)  # No prefix - includes '/' and '/recipe/*'
    app.register_blueprint(generation_bp)  # No prefix - includes '/generate_recipe'
    app.register_blueprint(api_bp)  # Prefix '/api' set in blueprint

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
    app.run(debug=True, host="0.0.0.0", port=5000)
