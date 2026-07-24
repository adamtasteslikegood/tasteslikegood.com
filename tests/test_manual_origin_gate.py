"""Tests for the origin column and manual-entry publish gate (KAN-140).

Covers:
- Manually entered recipes cannot be published (400 on create and update)
- origin is settable while NULL, immutable once set (no relabel laundering)
- Unknown origin labels are dropped, not stored
- origin is exposed in to_dict and the list endpoint
"""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402


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


@pytest.fixture
def logged_in(client, app):
    user = User(email="owner@example.com", name="Owner")
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
    return user


class TestManualPublishGate:
    def test_create_manual_and_public_returns_400(self, client, logged_in):
        resp = client.post(
            "/api/recipes",
            json={"name": "Handwritten", "origin": "manual", "is_public": True},
        )

        assert resp.status_code == 400
        assert "Manually entered" in resp.get_json()["error"]
        assert Recipe.query.count() == 0

    def test_create_manual_private_persists_origin(self, client, logged_in):
        resp = client.post("/api/recipes", json={"name": "Handwritten", "origin": "manual"})

        assert resp.status_code == 201
        body = resp.get_json()
        assert body["origin"] == "manual"
        assert db.session.get(Recipe, body["id"]).origin == "manual"

    def test_publishing_a_manual_recipe_later_returns_400(self, client, logged_in):
        created = client.post(
            "/api/recipes", json={"name": "Handwritten", "origin": "manual"}
        ).get_json()

        resp = client.put(f"/api/recipes/{created['id']}", json={"is_public": True})

        assert resp.status_code == 400
        assert "Manually entered" in resp.get_json()["error"]
        row = db.session.get(Recipe, created["id"])
        assert row.is_public is False

    def test_manual_origin_cannot_be_laundered_to_generated(self, client, logged_in):
        created = client.post(
            "/api/recipes", json={"name": "Handwritten", "origin": "manual"}
        ).get_json()

        relabel = client.put(
            f"/api/recipes/{created['id']}", json={"origin": "generated", "notes": "tweak"}
        )
        assert relabel.status_code == 200
        assert relabel.get_json()["origin"] == "manual"

        publish = client.put(f"/api/recipes/{created['id']}", json={"is_public": True})
        assert publish.status_code == 400

    def test_generated_recipe_still_publishes(self, client, logged_in):
        resp = client.post(
            "/api/recipes",
            json={"name": "AI Chili", "origin": "generated", "is_public": True},
        )

        assert resp.status_code == 201
        body = resp.get_json()
        assert body["is_public"] is True
        assert body["origin"] == "generated"

    def test_legacy_null_origin_still_publishes(self, client, logged_in):
        resp = client.post("/api/recipes", json={"name": "Old Row", "is_public": True})

        assert resp.status_code == 201
        assert resp.get_json()["is_public"] is True
        assert resp.get_json()["origin"] is None

    def test_unknown_origin_label_is_dropped(self, client, logged_in):
        resp = client.post("/api/recipes", json={"name": "Weird", "origin": "admin"})

        assert resp.status_code == 201
        assert resp.get_json()["origin"] is None

    def test_null_origin_can_adopt_manual_label(self, client, logged_in):
        recipe = Recipe(
            id=str(uuid.uuid4()),
            user_id=logged_in.id,
            name="Legacy Manual",
            data={"name": "Legacy Manual"},
        )
        db.session.add(recipe)
        db.session.commit()

        resp = client.put(f"/api/recipes/{recipe.id}", json={"origin": "manual"})

        assert resp.status_code == 200
        assert resp.get_json()["origin"] == "manual"

    def test_list_endpoint_exposes_origin(self, client, logged_in):
        client.post("/api/recipes", json={"name": "Handwritten", "origin": "manual"})

        resp = client.get("/api/recipes")

        assert resp.status_code == 200
        (entry,) = resp.get_json()["recipes"]
        assert entry["origin"] == "manual"
