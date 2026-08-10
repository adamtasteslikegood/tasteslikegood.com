"""RCP-74: Saved copies cannot be published.

A recipe saved from a public page (source_slug IS NOT NULL) must not be
publishable by the saver. The guard lives in _gate_is_public() and returns
403 at the API level.
"""

import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app
from extensions import db
from models.recipe import Recipe
from models.user import User
from repositories import db_recipe_repository


@pytest.fixture
def app():
    app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def author(app):
    u = User(email="author@example.com", name="Author")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def saver(app):
    u = User(email="saver@example.com", name="Saver")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def published_recipe(app, author):
    """A published recipe that can be the source for saved copies."""
    recipe = db_recipe_repository.create_recipe(
        {"id": "orig-1", "name": "Chili", "is_public": True},
        user_id=author.id,
    )
    assert recipe is not None
    assert recipe.is_public is True
    return recipe


# ─── Repository guard: saved copy cannot publish ────────────────────────


def test_saved_copy_create_publish_raises(app, saver, published_recipe):
    """Creating a saved copy with is_public=True raises SavedCopyPublishError."""
    with pytest.raises(db_recipe_repository.SavedCopyPublishError):
        db_recipe_repository.create_recipe(
            {
                "id": "copy-1",
                "name": "Chili",
                "sourceSlug": published_recipe.slug,
                "is_public": True,
                "origin": "saved",
            },
            user_id=saver.id,
        )


def test_saved_copy_create_unpublished_ok(app, saver, published_recipe):
    """Creating a saved copy with is_public=False succeeds normally."""
    recipe = db_recipe_repository.create_recipe(
        {
            "id": "copy-2",
            "name": "Chili",
            "sourceSlug": published_recipe.slug,
            "is_public": False,
            "origin": "saved",
        },
        user_id=saver.id,
    )
    assert recipe is not None
    assert recipe.is_public is False
    assert recipe.source_slug == published_recipe.slug


def test_saved_copy_update_publish_raises(app, saver, published_recipe):
    """Updating a saved copy to is_public=True raises SavedCopyPublishError."""
    recipe = db_recipe_repository.create_recipe(
        {
            "id": "copy-3",
            "name": "Chili",
            "sourceSlug": published_recipe.slug,
            "is_public": False,
            "origin": "saved",
        },
        user_id=saver.id,
    )
    assert recipe is not None

    with pytest.raises(db_recipe_repository.SavedCopyPublishError):
        db_recipe_repository.update_recipe(
            "copy-3",
            {"is_public": True},
            user_id=saver.id,
        )


def test_original_publish_still_works(app, author):
    """An original recipe (no source_slug) can still be published normally."""
    recipe = db_recipe_repository.create_recipe(
        {"id": "orig-2", "name": "Tacos", "is_public": True},
        user_id=author.id,
    )
    assert recipe is not None
    assert recipe.is_public is True


# ─── KAN-221: author/saver columns ──────────────────────────────────────


def test_original_sets_author(app, author):
    """An original recipe sets user_id_author = user_id, user_id_saved_to = None."""
    recipe = db_recipe_repository.create_recipe(
        {"id": "orig-3", "name": "Soup"},
        user_id=author.id,
    )
    assert recipe is not None
    assert recipe.user_id_author == author.id
    assert recipe.user_id_saved_to is None


def test_saved_copy_sets_author_and_saver(app, author, saver, published_recipe):
    """A saved copy sets user_id_author to source author, user_id_saved_to to saver."""
    recipe = db_recipe_repository.create_recipe(
        {
            "id": "copy-4",
            "name": "Chili",
            "sourceSlug": published_recipe.slug,
            "is_public": False,
            "origin": "saved",
        },
        user_id=saver.id,
    )
    assert recipe is not None
    assert recipe.user_id_author == author.id
    assert recipe.user_id_saved_to == saver.id


def test_saved_copy_orphan_author_is_none(app, saver):
    """A saved copy from a deleted source has user_id_author = None."""
    recipe = db_recipe_repository.create_recipe(
        {
            "id": "copy-5",
            "name": "Mystery",
            "sourceSlug": "deleted-recipe",
            "is_public": False,
            "origin": "saved",
        },
        user_id=saver.id,
    )
    assert recipe is not None
    assert recipe.user_id_author is None
    assert recipe.user_id_saved_to == saver.id


# ─── API layer: 403 ─────────────────────────────────────────────────────


def test_api_saved_copy_publish_returns_403(app, saver, published_recipe):
    """The API returns 403 when trying to publish a saved copy."""
    # First create the saved copy (unpublished)
    recipe = db_recipe_repository.create_recipe(
        {
            "id": "copy-6",
            "name": "Chili",
            "sourceSlug": published_recipe.slug,
            "is_public": False,
            "origin": "saved",
        },
        user_id=saver.id,
    )
    assert recipe is not None

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = saver.id

    resp = client.put(
        f"/api/recipes/{recipe.id}",
        json={"is_public": True},
    )
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["error"] == db_recipe_repository.SAVED_COPY_PUBLISH_ERROR
