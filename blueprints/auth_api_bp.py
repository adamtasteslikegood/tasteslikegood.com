"""
API Authentication Blueprint for Tastes Like Good application.

Provides RESTful API endpoints for frontend authentication:
- /api/auth/login - Initiates OAuth login flow (returns redirect URL)
- /api/auth/me - Get current user info (requires session cookie)
- /api/auth/logout - Clear authentication session
"""

import logging
import os
from functools import wraps

import googleapiclient.discovery
from dotenv import load_dotenv
from flask import Blueprint, jsonify, request, session, url_for
from google_auth_oauthlib.flow import Flow

load_dotenv()

logger = logging.getLogger(__name__)

# Allow OAuth over HTTP for local development
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

auth_api_bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cloud-platform",
]


def credentials_to_dict(credentials):
    """Convert credentials object to a JSON-serializable dictionary."""
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }


def require_auth(f):
    """Decorator to require authentication for API endpoints."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "credentials" not in session or "user_info" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated_function


@auth_api_bp.route("/login", methods=["GET"])
def api_login():
    """
    Initiate OAuth login flow.

    Returns:
        JSON with authorization URL that Angular frontend should redirect to.

    Example response:
        {
            "authorization_url": "https://accounts.google.com/o/oauth2/auth?...",
            "state": "random_state_value"
        }
    """
    client_config = {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [url_for("auth_api.api_callback", _external=True)],
        }
    }

    # Validate configuration
    if not client_config["web"]["client_id"] or not client_config["web"]["client_secret"]:
        return (
            jsonify({"error": "OAuth credentials not configured"}),
            500,
        )

    try:
        flow = Flow.from_client_config(client_config, scopes=SCOPES)
        redirect_uri = url_for("auth_api.api_callback", _external=True)
        flow.redirect_uri = redirect_uri

        authorization_url, state = flow.authorization_url(
            access_type="offline", include_granted_scopes="true"
        )
        session["state"] = state

        return jsonify({"authorization_url": authorization_url, "state": state}), 200

    except Exception as e:
        return jsonify({"error": f"Failed to initiate login: {str(e)}"}), 500


@auth_api_bp.route("/callback", methods=["GET"])
def api_callback():
    """
    OAuth callback endpoint.

    Called by Google after user authenticates. Sets session cookies
    with user credentials and info, then redirects to frontend.
    """
    state = session.get("state")
    if not state:
        return jsonify({"error": "State missing from session"}), 400

    client_config = {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [url_for("auth_api.api_callback", _external=True)],
        }
    }

    try:
        flow = Flow.from_client_config(client_config, scopes=SCOPES, state=state)
        flow.redirect_uri = url_for("auth_api.api_callback", _external=True)
        authorization_response = request.url
        flow.fetch_token(authorization_response=authorization_response)
        credentials = flow.credentials
        session["credentials"] = credentials_to_dict(credentials)

        # Get user info from Google
        userinfo_service = googleapiclient.discovery.build("oauth2", "v2", credentials=credentials)
        user_info = userinfo_service.userinfo().get().execute()
        session["user_info"] = user_info

        # Persist user to database (Phase 3)
        from extensions import db
        from models import Cookbook, Recipe, User

        google_id = user_info.get("id")
        email = user_info.get("email")
        name = user_info.get("name")

        # Capture current anonymous session scope before assigning authenticated user.
        previous_guest_session_id = session.get("session_id")

        # Find or create user
        user = User.query.filter_by(google_id=google_id).first()

        if not user and email:
            # Check if user exists by email (might have signed up before)
            user = User.query.filter_by(email=email).first()

        if user:
            # Update existing user info
            user.name = name or user.name
            user.google_id = google_id
            if email and not user.email:
                user.email = email
        else:
            # Create new user
            if not email:
                return jsonify({"error": "Email is required for registration"}), 400

            user = User(email=email, name=name, google_id=google_id)
            db.session.add(user)

        try:
            db.session.commit()
            logger.info(f"User {user.id} ({email}) authenticated successfully")
        except Exception as e:
            db.session.rollback()
            logger.error(f"Database error during user creation: {e}")
            return jsonify({"error": "Failed to save user data"}), 500

        # Store database user ID in session (not just email)
        session["user_id"] = user.id
        session["db_user"] = user.to_dict()  # Cache user info

        # Merge anonymous rows for this browser session into the authenticated user.
        if previous_guest_session_id:
            try:
                # Migrate guest recipes — update ownership AND fix metadata
                guest_recipes = Recipe.query.filter_by(
                    user_id=None, guest_session_id=previous_guest_session_id
                ).all()

                for recipe in guest_recipes:
                    recipe.user_id = user.id
                    recipe.guest_session_id = None

                    # Update ai_metadata inside the data JSON so exported
                    # recipes reflect the real owner instead of "anonymous".
                    data = recipe.data or {}
                    meta = data.get("ai_metadata", {})
                    gen = meta.get("recipe_generation", {})
                    if gen:
                        gen["user_display_name"] = name or email
                        gen["user_id"] = email
                        gen["is_authenticated"] = True
                        meta["recipe_generation"] = gen
                        data["ai_metadata"] = meta
                        recipe.data = data

                migrated_recipe_count = len(guest_recipes)

                Cookbook.query.filter_by(
                    user_id=None, guest_session_id=previous_guest_session_id
                ).update(
                    {"user_id": user.id, "guest_session_id": None},
                    synchronize_session=False,
                )

                db.session.commit()

                if migrated_recipe_count:
                    logger.info(
                        "Migrated %d guest recipe(s) to user %s (%s)",
                        migrated_recipe_count,
                        user.id,
                        email,
                    )
            except Exception as merge_error:
                db.session.rollback()
                logger.warning(
                    "Guest session migration failed for user %s: %s",
                    user.id,
                    merge_error,
                )

        # Redirect to frontend (Angular)
        # In production, redirect to your actual frontend URL
        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:3000")
        return f'<script>window.location.href = "{frontend_url}?auth=success";</script>'

    except Exception as e:
        return jsonify({"error": f"Authentication failed: {str(e)}"}), 500


@auth_api_bp.route("/me", methods=["GET"])
@require_auth
def api_me():
    """
    Get current authenticated user information.

    Requires: Valid session cookie from /api/auth/callback

    Returns:
        JSON with user info from database:
        {
            "id": 123,
            "email": "user@example.com",
            "name": "User Name",
            "picture": "https://...",
            "authenticated": true,
            "created_at": "2026-03-01T12:00:00"
        }
    """
    from models import User

    user_id = session.get("user_id")
    user_info = session.get("user_info", {})

    # Try to fetch fresh user data from database
    user = None
    if user_id:
        user = User.query.get(user_id)

    if user:
        return (
            jsonify(
                {
                    **user.to_dict(),
                    "picture": user_info.get("picture"),  # Picture not stored in DB
                    "authenticated": True,
                }
            ),
            200,
        )
    else:
        # Fallback to session data if database lookup fails
        return (
            jsonify(
                {
                    "user_id": user_id,
                    "email": user_info.get("email"),
                    "name": user_info.get("name"),
                    "picture": user_info.get("picture"),
                    "authenticated": True,
                }
            ),
            200,
        )


@auth_api_bp.route("/logout", methods=["POST"])
def api_logout():
    """
    Clear user authentication session.

    Returns:
        JSON confirmation of logout
    """
    session.clear()
    return jsonify({"message": "Logged out successfully", "authenticated": False}), 200


@auth_api_bp.route("/check", methods=["GET"])
def api_check():
    """
    Check if user is currently authenticated.

    Returns:
        JSON with authentication status and user info (if authenticated)
    """
    if "credentials" in session and "user_info" in session:
        user_info = session.get("user_info", {})
        return (
            jsonify(
                {
                    "authenticated": True,
                    "user_id": session.get("user_id"),
                    "email": user_info.get("email"),
                    "name": user_info.get("name"),
                    "picture": user_info.get("picture"),
                }
            ),
            200,
        )
    else:
        return (
            jsonify(
                {
                    "authenticated": False,
                    "user_id": None,
                }
            ),
            200,
        )
