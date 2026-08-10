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

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

_COOKBOOK_NAME_MAX = 200  # Cookbook.name is String(200)


def _dedupe_cookbook_name(name, taken):
    """Return ``name`` if free under this owner, else a suffixed variant.

    Mirrors migration b7e2a9c4d1f8's policy ("Name (2)", "Name (3)", …) and
    trims the base so the suffixed result still fits ``Cookbook.name``.
    """
    if name not in taken:
        return name
    n = 2
    while True:
        tag = f" ({n})"
        candidate = name[: max(0, _COOKBOOK_NAME_MAX - len(tag))] + tag
        if candidate not in taken:
            return candidate
        n += 1


def _recipe_identity_keys(recipe):
    """Return the slugs that identify which public recipe a row was saved from.

    Mirrors the client-side duplicate check (INV-1) in the cookbook SPA
    (``src/services/ssr-entry.service.ts``), which matches a candidate against
    ``r.sourceSlug === slug || r.slug === slug``. Both columns are consulted for
    the same reason: a row saved from ``/r/<slug>`` carries the public slug in
    ``source_slug``, while a row that *is* the published copy carries it in
    ``slug``. Empty values never match — ``None == None`` must not make two
    unrelated locally-authored recipes look like the same recipe.
    """
    return {value for value in (recipe.source_slug, recipe.slug) if value}


def _merge_guest_session_into_user(user, guest_session_id, max_retries=3):
    """Reassign a guest session's recipes and cookbooks to an authenticated user.

    Recipes are carried over **one row at a time, through a duplicate check**
    (KAN-186). A recipe saved during a guest session frequently duplicates one
    the account already owns: the user opens a public recipe, clicks "add to
    cookbook" before auth resolves, then logs in. Reassigning unconditionally
    silently created a second row, and publish-time confirmation (INV-3) is the
    last line of defense rather than the first — by the time it fires the
    duplicate already exists. Guest rows that match an owned recipe are dropped
    in favour of the copy the account already has, which is what a duplicate
    authenticated save does today.

    Two guards on that deletion, both deliberate:

    * A **public** guest row is reassigned rather than deleted. Deleting it
      would take a live public page down; a duplicate row is the lesser harm
      and remains visible to the user to resolve.
    * Cookbook membership is **remapped, not dropped**. ``Cookbook.recipe_ids``
      is a JSON list of ids, so deleting a row without rewriting the lists that
      reference it would merge a cookbook full of dangling ids.

    Cookbook names are unique per owner (``uq_cookbook_user_name``), so a guest
    cookbook whose name already exists under the target user would collide on
    reassignment and roll back the entire merge — silently orphaning the guest's
    rows under the now-authenticated session. Rename such guest cookbooks with a
    numeric suffix instead, so every row is preserved.

    Reading the occupied names and committing the rename is a check-then-act
    sequence: a concurrent create/merge for the same owner could claim a name in
    the window and make the commit hit the unique index. Retry on
    ``IntegrityError`` (re-reading the occupied names each attempt) so a lost
    race re-resolves instead of leaving the session unmerged. Commits on success
    and raises after exhausting retries so the caller can roll back and log.
    """
    from extensions import db
    from models import Cookbook, Recipe

    for attempt in range(max_retries):
        try:
            owned_by_key = {}
            for owned in Recipe.query.filter_by(user_id=user.id).all():
                for key in _recipe_identity_keys(owned):
                    # First writer wins: if the account somehow already holds two
                    # rows for the same public recipe, resolve to one of them
                    # rather than picking arbitrarily on each merge.
                    owned_by_key.setdefault(key, owned.id)

            guest_recipes = Recipe.query.filter_by(
                user_id=None, guest_session_id=guest_session_id
            ).all()

            # Guest recipe id -> the already-owned recipe id it resolves to.
            remapped = {}
            for recipe in guest_recipes:
                existing_id = next(
                    (
                        owned_by_key[key]
                        for key in _recipe_identity_keys(recipe)
                        if key in owned_by_key
                    ),
                    None,
                )
                if existing_id is not None and not recipe.is_public:
                    remapped[recipe.id] = existing_id
                    db.session.delete(recipe)
                    continue

                if existing_id is not None:
                    # Reached only by a PUBLIC guest row that duplicates an
                    # owned recipe — the exemption in the branch above.
                    #
                    # No current path can produce that row. Guests cannot
                    # publish: the SPA replaces the publish toggle with a
                    # "log in to publish" link, and the server does not rely on
                    # that — _gate_is_public() forces is_public=False whenever
                    # user_id is None, on create and update alike. Rows that
                    # predate the gate were reassigned or unpublished by
                    # migration e91b47a2c5d3 (2026-07-07).
                    #
                    # So this is a legacy-data guard, NOT a live conflict with
                    # KAN-213's uq_recipe_user_source_slug. Stated plainly
                    # because the reverse was claimed on review: if such a row
                    # somehow existed, reassigning it would raise IntegrityError
                    # and roll back the ENTIRE merge, orphaning the guest's
                    # recipes and cookbooks at the moment of login. Clearing
                    # source_slug takes it out of the partial index's coverage
                    # so a single legacy row cannot cost someone their data.
                    #
                    # Usually nothing of value is lost: the row is kept
                    # because it is a published page in its own right, so its
                    # own `slug` identifies it from here on — UNLESS that slug
                    # is itself what matched `existing_id` (see the KAN-223
                    # check below), in which case clearing source_slug alone
                    # doesn't remove the collision.
                    #
                    # Clear the MIRRORED BLOB KEY TOO, not just the column.
                    # `source_slug` mirrors `data['sourceSlug']`, and
                    # update_recipe rebuilds the blob as
                    # {**(recipe.data or {}), **recipe_data} before restaging
                    # `recipe.source_slug = data.get('sourceSlug')`. Clearing
                    # only the column means the next ordinary partial PUT (say
                    # {"name": "..."}) pulls the stale value back out of the
                    # untouched blob and writes it to the column — resurrecting
                    # the duplicate and making the row un-editable behind a 409.
                    # Caught by Codex review on PR #273; reproduced by
                    # test_clearing_the_column_survives_a_later_partial_update.
                    recipe.source_slug = None
                    if recipe.data and "sourceSlug" in recipe.data:
                        del recipe.data["sourceSlug"]

                    # KAN-223: the clear above only removes the collision if
                    # it came through `source_slug`. `_recipe_identity_keys()`
                    # matches on `slug` too, and clearing `source_slug`
                    # doesn't touch that — so if THIS row's own `slug` is what
                    # matched `existing_id` (not its `source_slug`), the
                    # post-clear identity (COALESCE(NULL, recipe.slug) ==
                    # recipe.slug) is exactly the value that collided in the
                    # first place. Reassigning it would still raise
                    # IntegrityError and roll back the ENTIRE merge over one
                    # legacy row, taking every other guest recipe and
                    # cookbook down with it — check first, and leave this one
                    # row under its guest session instead.
                    if recipe.slug and recipe.slug in owned_by_key:
                        logger.warning(
                            "KAN-213/KAN-223: guest recipe %s left under its "
                            "guest session at login — its slug %s still "
                            "collides with an owned recipe after clearing "
                            "source_slug (legacy public-row path; should be "
                            "unreachable via _gate_is_public)",
                            sanitize_log_value(recipe.id),
                            sanitize_log_value(recipe.slug),
                        )
                        continue

                recipe.user_id = user.id
                recipe.guest_session_id = None
                for key in _recipe_identity_keys(recipe):
                    owned_by_key.setdefault(key, recipe.id)

            guest_cookbooks = Cookbook.query.filter_by(
                user_id=None, guest_session_id=guest_session_id
            ).all()
            if guest_cookbooks:
                taken = {
                    row.name
                    for row in db.session.query(Cookbook.name).filter_by(user_id=user.id).all()
                }
                for cb in guest_cookbooks:
                    if remapped:
                        cb.recipe_ids = _remap_recipe_ids(cb.recipe_ids, remapped)
                    cb.name = _dedupe_cookbook_name(cb.name, taken)
                    cb.user_id = user.id
                    cb.guest_session_id = None
                    taken.add(cb.name)

            db.session.commit()
            return
        except IntegrityError:
            # Rollback reverts the uncommitted recipe reassignment too, so the
            # next attempt re-reads guest rows and occupied names from scratch.
            db.session.rollback()
            if attempt == max_retries - 1:
                raise


def _remap_recipe_ids(recipe_ids, remapped):
    """Point a cookbook's recipe list at surviving rows, preserving order.

    De-duplicates as it goes: a guest cookbook that held both the guest copy and
    the account's existing copy must not end up listing the survivor twice.
    """
    result = []
    for recipe_id in recipe_ids or []:
        resolved = remapped.get(recipe_id, recipe_id)
        if resolved not in result:
            result.append(resolved)
    return result


def credentials_to_dict(credentials):
    """Convert credentials object to a JSON-serializable dictionary.

    Intentionally excludes client_secret — it must not be stored in the
    session cookie. Token refresh reads the secret from GOOGLE_CLIENT_SECRET.
    """
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
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

        # Do NOT pass include_granted_scopes="true". We ask for the full scope
        # set up front (no incremental auth). Bundling previously-granted
        # scopes into the response trips oauthlib's scope-mismatch check.
        authorization_url, state = flow.authorization_url(access_type="offline")
        session["state"] = state
        # google-auth-oauthlib auto-generates a PKCE code_verifier and
        # embeds code_challenge in the auth URL. The callback must present
        # the same verifier at token exchange or Google rejects with
        # invalid_grant "Missing code verifier".
        if getattr(flow, "code_verifier", None):
            session["code_verifier"] = flow.code_verifier

        return jsonify({"authorization_url": authorization_url, "state": state}), 200

    except Exception as e:
        logger.exception("OAuth login failed: %s", e)
        return jsonify({"error": "Failed to initiate login"}), 500


@auth_api_bp.route("/callback", methods=["GET"])
def api_callback():  # noqa: C901
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
        # Restore PKCE verifier stashed during /api/auth/login.
        code_verifier = session.get("code_verifier")
        if code_verifier:
            flow.code_verifier = code_verifier
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
        from models import User

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

        # Redirect to frontend (Angular) with a real HTTP 302.
        # Must NOT be an inline <script> redirect: Express fronts this response
        # with a Helmet CSP of `script-src 'self'`, which blocks inline scripts,
        # so a `<script>window.location…</script>` body never executes and the
        # browser is left stranded on a blank callback page. A 302 Location
        # redirect carries no script for CSP to block and preserves the
        # Set-Cookie session header. (regression: #3109 enabled scoped CSP)
        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:3000")
        return redirect(f"{frontend_url}?auth=success")

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
