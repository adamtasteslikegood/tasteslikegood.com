"""Regression tests for auth_api_bp OAuth scope handling.

Background: Google bundles previously-granted scopes (e.g. cloud-platform
from a prior consent) into the token response and may reorder the scope
list. oauthlib raises on any scope set/order mismatch by default, which
fails the callback for returning users.

Two complementary defenses, both verified here:
1. /api/auth/login does NOT request include_granted_scopes — we ask for
   the full scope set up front, so there's no reason to opt into Google's
   incremental-auth bundling behavior.
2. OAUTHLIB_RELAX_TOKEN_SCOPE=1 is set at module import time so any
   residual mismatch (returning user with stale grant) is tolerated.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    flask_app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SECRET_KEY="test-secret",
        SERVER_NAME="testserver",
    )
    yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def test_oauthlib_relax_token_scope_env_set_at_import():
    import blueprints.auth_api_bp  # noqa: F401

    assert os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1"


def test_login_does_not_request_include_granted_scopes(client):
    with patch(
        "blueprints.auth_api_bp.Flow.authorization_url",
        return_value=(
            "https://accounts.google.com/o/oauth2/auth?state=x",
            "test-state",
        ),
    ) as mock_auth_url:
        resp = client.get("/api/auth/login")

    assert resp.status_code == 200, resp.data
    assert mock_auth_url.called
    _, kwargs = mock_auth_url.call_args
    assert "include_granted_scopes" not in kwargs, (
        "include_granted_scopes must not be requested — it causes Google to "
        "bundle previously-granted scopes (e.g. cloud-platform) into the "
        "response, which trips oauthlib's scope-mismatch check."
    )
    assert kwargs.get("access_type") == "offline"


@pytest.fixture
def db_app():
    """App with tables created — the callback persists a User row."""
    os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
    os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
    flask_app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SECRET_KEY="test-secret",
        SERVER_NAME="testserver",
    )
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


def test_callback_returns_302_redirect_not_inline_script(db_app):
    """The OAuth callback must redirect with a real HTTP 302, never an inline
    <script> body.

    Regression: PR #3109 turned on a Helmet CSP of `script-src 'self'` in the
    Express layer that fronts this endpoint. That CSP blocks inline scripts, so
    the previous `<script>window.location.href=…</script>` success response
    never executed — users were stranded on a blank callback page. A 302
    Location redirect carries no script for CSP to block.
    """
    client = db_app.test_client()

    with client.session_transaction() as sess:
        sess["state"] = "test-state"
        sess["code_verifier"] = "test-verifier"

    fake_credentials = SimpleNamespace(
        token="test-token",
        refresh_token="test-refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="test-client-id",
        scopes=["openid", "email", "profile"],
    )
    fake_flow = MagicMock()
    fake_flow.credentials = fake_credentials

    userinfo = {"id": "google-123", "email": "chef@example.com", "name": "Chef"}
    fake_userinfo_service = MagicMock()
    fake_userinfo_service.userinfo.return_value.get.return_value.execute.return_value = userinfo

    with (
        patch("blueprints.auth_api_bp.Flow.from_client_config", return_value=fake_flow),
        patch(
            "blueprints.auth_api_bp.googleapiclient.discovery.build",
            return_value=fake_userinfo_service,
        ),
        patch.dict(os.environ, {"FRONTEND_URL": "https://tasteslikegood.org"}),
    ):
        resp = client.get("/api/auth/callback?state=test-state&code=test-code")

    assert resp.status_code == 302, resp.data
    assert resp.headers["Location"] == "https://tasteslikegood.org?auth=success"
    # The failure mode we are guarding against: a 200 body containing an inline
    # script that the CSP would refuse to execute.
    assert b"<script" not in resp.data
