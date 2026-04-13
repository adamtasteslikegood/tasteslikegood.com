import os
from functools import wraps

import google.oauth2.credentials  # noqa: F401
import googleapiclient.discovery
from dotenv import load_dotenv
from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from google_auth_oauthlib.flow import Flow

load_dotenv()

# Allow OAuth over HTTP for local development only.
# Production runs over HTTPS via Cloud Run so this flag must not be set there.
if os.environ.get("FLASK_ENV") != "production":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

auth_bp = Blueprint("auth", __name__)

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cloud-platform",
]


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "credentials" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)

    return decorated_function


@auth_bp.route("/login")
def login():
    client_config = {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [url_for("auth.callback", _external=True)],
        }
    }

    # Check if credentials are present
    if not client_config["web"]["client_id"] or not client_config["web"]["client_secret"]:
        return (
            "Error: GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set in environment.",
            500,
        )

    flow = Flow.from_client_config(client_config, scopes=SCOPES)

    # Explicitly set the redirect_uri and log it for debugging
    redirect_uri = url_for("auth.callback", _external=True)
    print(f"DEBUG: Generated Redirect URI: {redirect_uri}")
    flow.redirect_uri = redirect_uri

    authorization_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true"
    )
    session["state"] = state
    return redirect(authorization_url)


@auth_bp.route("/callback")
def callback():
    state = session.get("state")
    if not state:
        abort(400, description="State missing from session.")

    client_config = {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [url_for("auth.callback", _external=True)],
        }
    }

    try:
        flow = Flow.from_client_config(client_config, scopes=SCOPES, state=state)
        flow.redirect_uri = url_for("auth.callback", _external=True)
        authorization_response = request.url
        flow.fetch_token(authorization_response=authorization_response)
        credentials = flow.credentials
        session["credentials"] = credentials_to_dict(credentials)

        # Get user info
        userinfo_service = googleapiclient.discovery.build("oauth2", "v2", credentials=credentials)
        user_info = userinfo_service.userinfo().get().execute()
        session["user_info"] = user_info

        # Store user_id for easy access (prefer email, fallback to Google ID)
        if "email" in user_info:
            session["user_id"] = user_info["email"]
        elif "id" in user_info:
            session["user_id"] = f"google_id_{user_info['id']}"

        # TODO: Migrate any recipes created during anonymous session to this user
        # old_session_id = session.get('session_id')
        # if old_session_id:
        #     from utils.session_utils import migrate_session_to_user
        #     migrate_session_to_user(old_session_id, session['user_id'])

    except Exception as e:
        abort(500, description=f"Authentication failed: {str(e)}")

    return redirect(url_for("recipes.index"))


@auth_bp.route("/profile")
@login_required
def profile():
    return render_template("profile.html", user_info=session.get("user_info"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("recipes.index"))


def credentials_to_dict(credentials):
    # Intentionally excludes client_secret — it must not be stored in the
    # session cookie. Credential reconstruction reads the secret from the
    # environment at token-refresh time via GOOGLE_CLIENT_SECRET.
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "scopes": credentials.scopes,
    }
