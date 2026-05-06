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
from unittest.mock import patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402


@pytest.fixture
def app():
    os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
    os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
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
