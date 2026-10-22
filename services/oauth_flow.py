"""
Google OAuth 2.0 PKCE/state flow, user profile persistence, and guest-session merge helpers.

Extracted from ``blueprints.auth_api_bp`` (KAN-463) so the blueprint module stays a thin
HTTP route and session facade while OAuth client configuration, token exchange,
profile normalization, and anonymous-to-authenticated cookbook/recipe migration
live in a cohesive service module.
"""

import logging
import os
import sys
from collections.abc import Mapping
from typing import Any, TypeVar, cast

import googleapiclient.discovery
from google_auth_oauthlib.flow import Flow
from sqlalchemy.exc import IntegrityError

from utils.cache_utils import invalidate_identity
from utils.log_sanitizer import sanitize_log_value

logger = logging.getLogger(__name__)

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

_COOKBOOK_NAME_MAX = 200  # Cookbook.name is String(200)

_T = TypeVar("_T")


def _bp_attr(name: str, default: _T) -> _T:
    """Resolve ``name`` from ``blueprints.auth_api_bp`` when loaded so test monkeypatches apply."""
    bp_mod = sys.modules.get("blueprints.auth_api_bp")
    return cast(_T, getattr(bp_mod, name, default)) if bp_mod is not None else default


def _dedupe_cookbook_name(name: str, taken: set[str]) -> str:
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


def _recipe_identity_keys(recipe: Any) -> set[str]:
    """Return the slugs that identify which public recipe a row was saved from.

    Mirrors the client-side duplicate check (INV-1) in the cookbook SPA
    (``src/services/ssr-entry.service.ts``), which matches a candidate against
    ``r.sourceSlug === slug || r.slug === slug``. Both columns are consulted for
    the same reason: a row saved from ``/r/<slug>`` carries the public slug in
    ``source_slug``, while a row that *is* the published copy carries it in
    ``slug``. Empty values never match — ``None == None`` must not make two
    unrelated locally-authored recipes look like the same recipe.
    """
    return {value for value in (recipe.source_recipe_id, recipe.source_slug, recipe.slug) if value}


def _remap_recipe_ids(recipe_ids: list[str] | None, remapped: Mapping[str, str]) -> list[str]:
    """Point a cookbook's recipe list at surviving rows, preserving order.

    De-duplicates as it goes: a guest cookbook that held both the guest copy and
    the account's existing copy must not end up listing the survivor twice.
    """
    result: list[str] = []
    for recipe_id in recipe_ids or []:
        resolved = remapped.get(recipe_id, recipe_id)
        if resolved not in result:
            result.append(resolved)
    return result


def _merge_guest_session_into_user(user: Any, guest_session_id: str, max_retries: int = 3) -> None:
    """Reassign a guest session's recipes and cookbooks to an authenticated user.

    Recipes are carried over **one row at a time, through a duplicate check**
    (KAN-186). Guest rows that match an owned recipe are dropped in favour of
    the copy the account already has, except public guest rows which are
    reassigned (or left under the guest session if their own slug still collides
    after clearing ``source_slug``, KAN-223). Cookbook membership is remapped
    and colliding cookbook names receive a numeric suffix, retrying on
    ``IntegrityError`` if a concurrent write claims a name mid-merge.
    """
    from extensions import db
    from models import Cookbook, Recipe

    identity_keys_fn = _bp_attr("_recipe_identity_keys", _recipe_identity_keys)
    remap_ids_fn = _bp_attr("_remap_recipe_ids", _remap_recipe_ids)
    dedupe_name_fn = _bp_attr("_dedupe_cookbook_name", _dedupe_cookbook_name)
    invalidate_fn = _bp_attr("invalidate_identity", invalidate_identity)
    sanitize_fn = _bp_attr("sanitize_log_value", sanitize_log_value)

    for attempt in range(max_retries):
        try:
            owned_by_key: dict[str, str] = {}
            for owned in Recipe.query.filter_by(user_id=user.id).all():
                for key in identity_keys_fn(owned):
                    owned_by_key.setdefault(key, owned.id)

            guest_recipes = Recipe.query.filter_by(
                user_id=None, guest_session_id=guest_session_id
            ).all()

            remapped: dict[str, str] = {}
            for recipe in guest_recipes:
                existing_id = next(
                    (owned_by_key[key] for key in identity_keys_fn(recipe) if key in owned_by_key),
                    None,
                )
                if existing_id is not None and not recipe.is_public:
                    remapped[recipe.id] = existing_id
                    db.session.delete(recipe)
                    continue

                if existing_id is not None:
                    # Legacy public guest row guard (KAN-213 / KAN-223): clear both
                    # source_slug column and mirrored JSON blob key so a later
                    # partial update does not resurrect the duplicate.
                    recipe.source_slug = None
                    if recipe.data and "sourceSlug" in recipe.data:
                        del recipe.data["sourceSlug"]

                    if recipe.slug and recipe.slug in owned_by_key:
                        logger.warning(
                            "KAN-213/KAN-223: guest recipe %s left under its "
                            "guest session at login — its slug %s still "
                            "collides with an owned recipe after clearing "
                            "source_slug (legacy public-row path; should be "
                            "unreachable via _gate_is_public)",
                            sanitize_fn(recipe.id),
                            sanitize_fn(recipe.slug),
                        )
                        remapped[recipe.id] = owned_by_key[recipe.slug]
                        continue

                recipe.user_id = user.id
                recipe.guest_session_id = None
                # KAN-221: carry author/saver columns through login-merge.
                if recipe.source_slug is not None or recipe.source_recipe_id is not None:
                    recipe.user_id_saved_to = user.id
                    if recipe.user_id_author is None:
                        source = None
                        if recipe.source_recipe_id is not None:
                            source = db.session.get(Recipe, recipe.source_recipe_id)
                        if source is None and recipe.source_slug is not None:
                            source = Recipe.query.filter(
                                Recipe.slug == recipe.source_slug,
                                Recipe.is_public.is_(True),
                                Recipe.id != recipe.id,
                            ).first()
                        recipe.user_id_author = source.user_id if source is not None else None
                else:
                    if recipe.user_id_author is None:
                        recipe.user_id_author = user.id
                for key in identity_keys_fn(recipe):
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
                        cb.recipe_ids = remap_ids_fn(cb.recipe_ids, remapped)
                    cb.name = dedupe_name_fn(cb.name, taken)
                    cb.user_id = user.id
                    cb.guest_session_id = None
                    taken.add(cb.name)

            # KAN-151: capture ids BEFORE the commit.
            merged_recipe_ids = [r.id for r in guest_recipes]
            merged_cookbook_ids = [cb.id for cb in guest_cookbooks]

            db.session.commit()

            invalidate_fn(user.id, None, merged_recipe_ids, merged_cookbook_ids)
            invalidate_fn(None, guest_session_id, merged_recipe_ids, merged_cookbook_ids)
            return
        except IntegrityError:
            db.session.rollback()
            if attempt == max_retries - 1:
                raise


def credentials_to_dict(credentials: Any) -> dict[str, Any]:
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


def build_oauth_client_config(redirect_uri: str) -> dict[str, dict[str, Any]]:
    """Build Google OAuth web client config from environment variables."""
    return {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def has_oauth_credentials(client_config: Mapping[str, Mapping[str, Any]]) -> bool:
    """Return True when both Google OAuth client_id and client_secret are configured."""
    web_cfg = client_config.get("web", {})
    return bool(web_cfg.get("client_id") and web_cfg.get("client_secret"))


def build_login_authorization_url(
    client_config: Mapping[str, Any],
    redirect_uri: str,
    prompt: str | None = None,
) -> tuple[str, str, str | None]:
    """Create the OAuth Flow and return ``(authorization_url, state, code_verifier)``."""
    flow_cls = _bp_attr("Flow", Flow)
    scopes = _bp_attr("SCOPES", SCOPES)
    flow = flow_cls.from_client_config(client_config, scopes=scopes)
    flow.redirect_uri = redirect_uri

    # Do NOT pass include_granted_scopes="true". KAN-344 forwards only
    # prompt="select_account" when explicitly requested for account switching.
    auth_params: dict[str, str] = {"access_type": "offline"}
    if prompt == "select_account":
        auth_params["prompt"] = "select_account"
    authorization_url, state = flow.authorization_url(**auth_params)
    code_verifier = getattr(flow, "code_verifier", None)
    return authorization_url, state, code_verifier


def exchange_oauth_callback_tokens(
    client_config: Mapping[str, Any],
    state: str,
    redirect_uri: str,
    authorization_response: str,
    code_verifier: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Exchange OAuth callback code for credentials and fetch Google userinfo."""
    flow_cls = _bp_attr("Flow", Flow)
    scopes = _bp_attr("SCOPES", SCOPES)
    creds_fn = _bp_attr("credentials_to_dict", credentials_to_dict)
    discovery_mod = _bp_attr("googleapiclient", googleapiclient).discovery

    flow = flow_cls.from_client_config(client_config, scopes=scopes, state=state)
    flow.redirect_uri = redirect_uri
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=authorization_response)
    credentials = flow.credentials
    creds_dict = creds_fn(credentials)

    userinfo_service = discovery_mod.build("oauth2", "v2", credentials=credentials)
    user_info = userinfo_service.userinfo().get().execute()
    return creds_dict, user_info


def upsert_authenticated_user(user_info: Mapping[str, Any]) -> tuple[Any, str | None]:
    """Find or create a ``User`` row from Google userinfo without committing yet.

    Returns ``(user, error_message)``. If ``email`` is missing for a new user,
    returns ``(None, "Email is required for registration")``.
    """
    from extensions import db
    from models import User

    google_id = user_info.get("id")
    email = user_info.get("email")
    name = user_info.get("name")

    user = User.query.filter_by(google_id=google_id).first()
    if not user and email:
        user = User.query.filter_by(email=email).first()

    if user:
        user.name = name or user.name
        user.google_id = google_id
        if email and not user.email:
            user.email = email
        return user, None

    if not email:
        return None, "Email is required for registration"

    user = User(email=email, name=name, google_id=google_id)
    db.session.add(user)
    return user, None


def build_frontend_auth_redirect_url() -> str:
    """Return the post-login frontend redirect URL (302 target, CSP-safe)."""
    frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:3000")
    return f"{frontend_url}?auth=success"
