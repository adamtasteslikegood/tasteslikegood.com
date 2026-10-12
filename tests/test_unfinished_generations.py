"""KAN-381: a row that never finished generating is not one of the owner's recipes."""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import cache, db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402
from repositories import db_recipe_repository  # noqa: E402

STATUSES = ("ready", "generating_image", "generating", "processing", "error")
LISTED = {"ready", "generating_image"}


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr("config.VALKEY_HOST", None)
    monkeypatch.setattr("config.REDIS_URL", None)
    app = create_app(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    with app.app_context():
        db.create_all()
        cache.clear()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _row(status, *, user_id=None, guest_session_id=None):
    name = "Generating..." if status not in LISTED else f"Stew ({status})"
    db.session.add(
        Recipe(
            id=str(uuid.uuid4()),
            user_id=user_id,
            guest_session_id=guest_session_id,
            name=name,
            status=status,
            data={"name": name, "ingredients": [], "instructions": []},
        )
    )


def _login(client, **session):
    with client.session_transaction() as sess:
        sess.update(session)


def test_list_and_count_leave_out_unfinished_generations(app, client):
    user = User(email="cook@example.com", name="Cook")
    db.session.add(user)
    db.session.commit()
    for status in STATUSES:
        _row(status, user_id=user.id)
    db.session.commit()
    _login(client, user_id=user.id)

    listed = client.get("/api/recipes").get_json()["recipes"]

    assert sorted(recipe["name"] for recipe in listed) == sorted(
        f"Stew ({status})" for status in LISTED
    )
    assert client.get("/api/recipes/stats").get_json()["total_recipes"] == len(LISTED)


def test_guest_list_leaves_them_out_too(app):
    for status in STATUSES:
        _row(status, guest_session_id="guest-1")
    db.session.commit()

    listed = db_recipe_repository.get_user_recipes(None, "guest-1")

    assert {recipe.status for recipe in listed} == LISTED
    assert db_recipe_repository.count_user_recipes(None, "guest-1") == len(LISTED)


def test_an_unfinished_row_is_still_readable_by_id(app, client):
    """The status poll and the worker read the placeholder by id; only the list hides it."""
    user = User(email="cook@example.com", name="Cook")
    db.session.add(user)
    db.session.commit()
    _row("generating", user_id=user.id)
    db.session.commit()
    recipe_id = Recipe.query.one().id
    _login(client, user_id=user.id)

    resp = client.get(f"/api/recipes/{recipe_id}/status")

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "generating"
