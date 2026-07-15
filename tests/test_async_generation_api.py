"""Tests for the Pub/Sub-backed generation API endpoints."""

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402


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
    client = app.test_client()
    with client.session_transaction() as session:
        session["session_id"] = "guest-1"
    return client


def _make_recipe(*, status="ready", data=None, guest_session_id="guest-1"):
    recipe_id = str(uuid.uuid4())
    recipe = Recipe(
        id=recipe_id,
        user_id=None,
        guest_session_id=guest_session_id,
        name="Test Recipe",
        status=status,
        data=data or {"id": recipe_id, "name": "Test Recipe"},
    )
    db.session.add(recipe)
    db.session.commit()
    return recipe_id


def test_generate_recipe_creates_pending_row_and_queues_message(app, client):
    with patch("services.pubsub_service.publish_message") as publish:
        response = client.post(
            "/api/generate",
            json={"prompt": "A bright summer pasta", "model": "models/test-model"},
        )

    assert response.status_code == 202
    body = response.get_json()
    with app.app_context():
        recipe = db.session.get(Recipe, body["recipe_id"])
        assert recipe is not None
        assert recipe.status == "generating"
        assert recipe.guest_session_id == "guest-1"
    publish.assert_called_once_with(
        "recipe-generation",
        {
            "recipe_id": body["recipe_id"],
            "prompt": "A bright summer pasta",
            "model": "models/test-model",
            "user_id": None,
            "guest_session_id": "guest-1",
        },
    )


def test_generate_recipe_marks_pending_row_error_when_publish_fails(app, client):
    with patch(
        "services.pubsub_service.publish_message",
        side_effect=RuntimeError("Pub/Sub unavailable"),
    ):
        response = client.post("/api/generate", json={"prompt": "A lentil stew"})

    assert response.status_code == 500
    with app.app_context():
        recipe = db.session.query(Recipe).one()
        assert recipe.status == "error"


def test_generate_image_queues_message_for_owned_recipe(app, client):
    with app.app_context():
        recipe_id = _make_recipe()

    with patch("services.pubsub_service.publish_message") as publish:
        response = client.post(
            "/api/generate_image",
            json={"recipe_id": recipe_id, "force_regenerate": True},
        )

    assert response.status_code == 202
    assert response.get_json() == {"status": "generating_image"}
    publish.assert_called_once_with(
        "image-generation",
        {
            "recipe_id": recipe_id,
            "user_id": None,
            "guest_session_id": "guest-1",
            "force_regenerate": True,
        },
    )


def test_generate_image_returns_existing_image_without_requeue(app, client):
    with app.app_context():
        recipe_id = _make_recipe(
            data={
                "name": "Test Recipe",
                "ai_image_data": "encoded-image",
                "ai_image_url": "/api/recipes/test/image",
            }
        )

    with patch("services.pubsub_service.publish_message") as publish:
        response = client.post("/api/generate_image", json={"recipe_id": recipe_id})

    assert response.status_code == 200
    assert response.get_json() == {"image_url": "/api/recipes/test/image"}
    publish.assert_not_called()


def test_generate_image_reports_publish_failure(app, client):
    with app.app_context():
        recipe_id = _make_recipe()

    with patch(
        "services.pubsub_service.publish_message",
        side_effect=RuntimeError("Pub/Sub unavailable"),
    ):
        response = client.post("/api/generate_image", json={"recipe_id": recipe_id})

    assert response.status_code == 500
    assert response.get_json() == {"error": "Failed to queue image generation"}


def test_recipe_status_returns_owned_recipe(app, client):
    with app.app_context():
        recipe_id = _make_recipe(status="generating")

    response = client.get(f"/api/recipes/{recipe_id}/status")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "generating"
    assert body["recipe"]["name"] == "Test Recipe"


def test_recipe_status_hides_other_guest_session(app, client):
    with app.app_context():
        recipe_id = _make_recipe(guest_session_id="guest-2")

    response = client.get(f"/api/recipes/{recipe_id}/status")

    assert response.status_code == 404
