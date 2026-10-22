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
from flask import Blueprint, jsonify, redirect, request, session, url_for
from google_auth_oauthlib.flow import Flow
from sqlalchemy.exc import IntegrityError

from services.oauth_flow import (
    _COOKBOOK_NAME_MAX,
    SCOPES,
    _dedupe_cookbook_name,
    _merge_guest_session_into_user,
    _recipe_identity_keys,
    _remap_recipe_ids,
    build_frontend_auth_redirect_url,
    build_login_authorization_url,
    build_oauth_client_config,
    credentials_to_dict,
    exchange_oauth_callback_tokens,
    has_oauth_credentials,
    upsert_authenticated_user,
)
from utils.cache_utils import invalidate_identity
from utils.log_sanitizer import sanitize_log_value

load_dotenv()

logger = logging.getLogger(__name__)

# Allow OAuth over HTTP for local development only. In production the request
# is HTTPS at the edge (Cloud Run) and ProxyFix forwards the scheme, so this
# flag should never be needed. Use setdefault so an operator can still pin
# the value explicitly via the runtime environment if needed.
if os.environ.get("FLASK_ENV") != "production":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# Tolerate scope set/order differences in Google's token response. Google may
# bundle previously-granted scopes (e.g. cloud-platform from an earlier consent)
# into the response and reorder the list; oauthlib raises on any mismatch by
# default, which fails the callback for returning users with stale grants.
# setdefault preserves any explicit value an operator sets in the environment.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

auth_api_bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")


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

    ``?prompt=select_account`` forces Google's account chooser (KAN-344).

    Returns:
        JSON with authorization URL that Angular frontend should redirect to.

    Example response:
        {
            "authorization_url": "https://accounts.google.com/o/oauth2/auth?...",
            "state": "random_state_value"
        }
    """
    redirect_uri = url_for("auth_api.api_callback", _external=True)
    client_config = build_oauth_client_config(redirect_uri)

    # Validate configuration
    if not has_oauth_credentials(client_config):
        return (
            jsonify({"error": "OAuth credentials not configured"}),
            500,
        )

    try:
        authorization_url, state, code_verifier = build_login_authorization_url(
            client_config,
            redirect_uri=redirect_uri,
            prompt=request.args.get("prompt"),
        )
        session["state"] = state
        # google-auth-oauthlib auto-generates a PKCE code_verifier and
        # embeds code_challenge in the auth URL. The callback must present
        # the same verifier at token exchange or Google rejects with
        # invalid_grant "Missing code verifier".
        if code_verifier:
            session["code_verifier"] = code_verifier

        return jsonify({"authorization_url": authorization_url, "state": state}), 200

    except Exception as e:
        logger.exception("OAuth login failed: %s", e)
        return jsonify({"error": "Failed to initiate login"}), 500


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

    redirect_uri = url_for("auth_api.api_callback", _external=True)
    client_config = build_oauth_client_config(redirect_uri)

    try:
        creds_dict, user_info = exchange_oauth_callback_tokens(
            client_config,
            state=state,
            redirect_uri=redirect_uri,
            authorization_response=request.url,
            code_verifier=session.get("code_verifier"),
        )
        session["credentials"] = creds_dict
        session["user_info"] = user_info

        # Persist user to database (Phase 3)
        from extensions import db

        email = user_info.get("email")

        # Capture current anonymous session scope before assigning authenticated user.
        previous_guest_session_id = session.get("session_id")

        user, error_msg = upsert_authenticated_user(user_info)
        if error_msg or user is None:
            return jsonify({"error": error_msg or "Email is required for registration"}), 400

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

        # Merge anonymous rows for this browser session into the authenticated
        # user. Cookbook names are unique per owner, so the merge renames any
        # guest cookbook whose name collides with one the user already owns
        # rather than failing and orphaning the guest's rows.
        if previous_guest_session_id:
            try:
                _merge_guest_session_into_user(user, previous_guest_session_id)
            except Exception as merge_error:
                db.session.rollback()
                logger.warning(
                    "Guest session migration failed for user %s: %s",
                    user.id,
                    merge_error,
                )

        # Redirect to frontend (Angular) with a real HTTP 302 (CSP-safe).
        return redirect(build_frontend_auth_redirect_url())

    except Exception as e:
        logger.exception("OAuth callback failed: %s", e)
        return jsonify({"error": "Authentication failed"}), 500


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


__all__ = [
    "Flow",
    "IntegrityError",
    "SCOPES",
    "_COOKBOOK_NAME_MAX",
    "_dedupe_cookbook_name",
    "_merge_guest_session_into_user",
    "_recipe_identity_keys",
    "_remap_recipe_ids",
    "api_callback",
    "api_check",
    "api_login",
    "api_logout",
    "api_me",
    "auth_api_bp",
    "build_frontend_auth_redirect_url",
    "build_login_authorization_url",
    "build_oauth_client_config",
    "credentials_to_dict",
    "exchange_oauth_callback_tokens",
    "googleapiclient",
    "has_oauth_credentials",
    "invalidate_identity",
    "require_auth",
    "sanitize_log_value",
    "upsert_authenticated_user",
]
