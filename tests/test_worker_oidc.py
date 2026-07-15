"""OIDC authorization tests for Pub/Sub worker push endpoints."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from blueprints import worker_api_bp as worker_module  # noqa: E402
from extensions import db  # noqa: E402


@pytest.fixture
def app():
    app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        WTF_CSRF_ENABLED=False,
    )
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _enforce_pubsub_auth(monkeypatch):
    monkeypatch.setattr(worker_module, "PUBSUB_AUTH_OPTIONAL", False)


def _headers():
    return {
        "Authorization": "Bearer signed-token",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "flask-backend.example",
    }


def test_worker_fails_closed_without_invoker_service_account(client, monkeypatch):
    monkeypatch.setattr(worker_module, "PUBSUB_INVOKER_SA", None)

    response = client.post("/api/worker/recipe", headers=_headers(), json={})

    assert response.status_code == 503


def test_worker_requires_bearer_token(client, monkeypatch):
    monkeypatch.setattr(
        worker_module, "PUBSUB_INVOKER_SA", "pubsub@example.iam.gserviceaccount.com"
    )

    response = client.post("/api/worker/recipe", json={})

    assert response.status_code == 401


def test_worker_rejects_invalid_oidc_token(client, monkeypatch):
    monkeypatch.setattr(
        worker_module, "PUBSUB_INVOKER_SA", "pubsub@example.iam.gserviceaccount.com"
    )
    with patch.object(
        worker_module.id_token,
        "verify_oauth2_token",
        side_effect=ValueError("invalid audience"),
    ):
        response = client.post("/api/worker/recipe", headers=_headers(), json={})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"email": "other@example.iam.gserviceaccount.com", "email_verified": True},
        {"email": "pubsub@example.iam.gserviceaccount.com", "email_verified": False},
    ],
)
def test_worker_rejects_wrong_or_unverified_identity(client, monkeypatch, claims):
    monkeypatch.setattr(
        worker_module, "PUBSUB_INVOKER_SA", "pubsub@example.iam.gserviceaccount.com"
    )
    with patch.object(worker_module.id_token, "verify_oauth2_token", return_value=claims):
        response = client.post("/api/worker/recipe", headers=_headers(), json={})

    assert response.status_code == 403


def test_worker_verifies_token_for_exact_external_endpoint(client, monkeypatch):
    invoker = "pubsub@example.iam.gserviceaccount.com"
    monkeypatch.setattr(worker_module, "PUBSUB_INVOKER_SA", invoker)
    with patch.object(
        worker_module.id_token,
        "verify_oauth2_token",
        return_value={"email": invoker, "email_verified": True},
    ) as verify:
        response = client.post("/api/worker/recipe", headers=_headers(), json={})

    assert response.status_code == 400
    assert verify.call_count == 1
    args, kwargs = verify.call_args
    assert args[0] == "signed-token"
    assert kwargs["audience"] == "https://flask-backend.example/api/worker/recipe"
