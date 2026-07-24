"""Tests for the is_canonical lock and source_slug column (KAN-139 / cookbook #3217).

Covers:
- Canonical recipes reject unpublish, slug change, and delete with 400
- Canonical recipes still accept content edits
- is_canonical is never writable through the API
- source_slug is persisted from the payload's sourceSlug on create/update,
  retained across partial updates, and exposed in API responses
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
def user(app):
    u = User(email="owner@example.com", name="Owner")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def logged_in(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
    return user


def _make_canonical(owner, slug="vegan-cornbread"):
    recipe = Recipe(
        id=str(uuid.uuid4()),
        user_id=owner.id,
        name="Vegan Cornbread",
        slug=slug,
        is_public=True,
        is_canonical=True,
        data={"name": "Vegan Cornbread", "slug": slug, "is_public": True},
    )
    db.session.add(recipe)
    db.session.commit()
    return recipe


class TestCanonicalLock:
    def test_unpublish_canonical_returns_400_and_row_stays_public(self, client, logged_in):
        recipe = _make_canonical(logged_in)

        resp = client.put(f"/api/recipes/{recipe.id}", json={"is_public": False})

        assert resp.status_code == 400
        assert "canonical" in resp.get_json()["error"]
        row = db.session.get(Recipe, recipe.id)
        assert row.is_public is True
        assert row.slug == "vegan-cornbread"

    def test_slug_change_on_canonical_returns_400(self, client, logged_in):
        recipe = _make_canonical(logged_in)

        resp = client.put(f"/api/recipes/{recipe.id}", json={"slug": "vegan-cornbread-new"})

        assert resp.status_code == 400
        assert db.session.get(Recipe, recipe.id).slug == "vegan-cornbread"

    def test_delete_canonical_returns_400_and_row_survives(self, client, logged_in):
        recipe = _make_canonical(logged_in)

        resp = client.delete(f"/api/recipes/{recipe.id}")

        assert resp.status_code == 400
        assert db.session.get(Recipe, recipe.id) is not None

    def test_content_edit_on_canonical_is_allowed(self, client, logged_in):
        recipe = _make_canonical(logged_in)

        resp = client.put(
            f"/api/recipes/{recipe.id}",
            json={"description": "Now with maple butter.", "is_public": True},
        )

        assert resp.status_code == 200
        row = db.session.get(Recipe, recipe.id)
        assert row.data["description"] == "Now with maple butter."
        assert row.is_public is True
        assert row.slug == "vegan-cornbread"
        assert row.is_canonical is True

    def test_echoed_matching_slug_does_not_trip_the_guard(self, client, logged_in):
        # The SPA PUTs its full blob back, including the slug it loaded.
        recipe = _make_canonical(logged_in)

        resp = client.put(
            f"/api/recipes/{recipe.id}",
            json={"name": "Vegan Cornbread", "slug": "vegan-cornbread", "is_public": True},
        )

        assert resp.status_code == 200

    def test_delete_non_canonical_still_works(self, client, logged_in):
        recipe = Recipe(
            id=str(uuid.uuid4()),
            user_id=logged_in.id,
            name="Scratch Pad",
            data={"name": "Scratch Pad"},
        )
        db.session.add(recipe)
        db.session.commit()

        resp = client.delete(f"/api/recipes/{recipe.id}")

        assert resp.status_code == 200
        assert db.session.get(Recipe, recipe.id) is None

    def test_api_cannot_set_is_canonical_on_create(self, client, logged_in):
        resp = client.post(
            "/api/recipes",
            json={"name": "Impostor", "is_canonical": True},
        )

        assert resp.status_code == 201
        body = resp.get_json()
        assert body["is_canonical"] is False
        assert db.session.get(Recipe, body["id"]).is_canonical is False

    def test_api_cannot_clear_is_canonical_on_update(self, client, logged_in):
        recipe = _make_canonical(logged_in)

        resp = client.put(
            f"/api/recipes/{recipe.id}",
            json={"is_canonical": False, "description": "sneaky"},
        )

        assert resp.status_code == 200
        row = db.session.get(Recipe, recipe.id)
        assert row.is_canonical is True
        assert row.data["is_canonical"] is True


class TestSourceSlug:
    def test_source_slug_persisted_on_create(self, client, logged_in):
        resp = client.post(
            "/api/recipes",
            json={"name": "Saved Copy", "sourceSlug": "vegan-cornbread"},
        )

        assert resp.status_code == 201
        body = resp.get_json()
        assert body["source_slug"] == "vegan-cornbread"
        assert db.session.get(Recipe, body["id"]).source_slug == "vegan-cornbread"

    def test_partial_update_without_source_slug_keeps_column(self, client, logged_in):
        created = client.post(
            "/api/recipes",
            json={"name": "Saved Copy", "sourceSlug": "vegan-cornbread"},
        ).get_json()

        resp = client.put(
            f"/api/recipes/{created['id']}",
            json={"description": "tweaked"},
        )

        assert resp.status_code == 200
        assert resp.get_json()["source_slug"] == "vegan-cornbread"

    def test_list_endpoint_exposes_canonical_and_source_slug(self, client, logged_in):
        client.post("/api/recipes", json={"name": "Saved Copy", "sourceSlug": "vegan-cornbread"})

        resp = client.get("/api/recipes")

        assert resp.status_code == 200
        (entry,) = resp.get_json()["recipes"]
        assert entry["source_slug"] == "vegan-cornbread"
        assert entry["is_canonical"] is False
