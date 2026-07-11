"""Publishing accountability gate (eng-review 3A/D8).

Only authenticated (OAuth) users may publish: the repository forces
``is_public=False`` for guest writes regardless of the request payload, and
the data migration reassigns-or-unpublishes pre-existing guest-owned public
rows so /r/<slug> pages backed by an accountable owner stay live.
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
from scripts.gate_guest_public_recipes import run_gate


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
def user(app):
    u = User(email="owner@example.com", name="Owner")
    db.session.add(u)
    db.session.commit()
    return u


def _recipe_data(recipe_id="r-1", **overrides):
    data = {"id": recipe_id, "name": "Chili", "slug": "chili"}
    data.update(overrides)
    return data


# ─── Repository gate: create path ────────────────────────────────────────


def test_guest_create_cannot_publish(app):
    recipe = db_recipe_repository.create_recipe(
        _recipe_data(is_public=True), user_id=None, guest_session_id="g1"
    )
    assert recipe is not None
    assert recipe.is_public is False
    # The JSON blob must agree with the column, or later readers disagree.
    assert recipe.data["is_public"] is False


def test_user_create_can_publish(app, user):
    recipe = db_recipe_repository.create_recipe(
        _recipe_data(is_public=True), user_id=user.id
    )
    assert recipe is not None
    assert recipe.is_public is True
    assert recipe.data["is_public"] is True


def test_guest_upsert_cannot_flip_public(app):
    """REGRESSION (upsert branch): the Angular toggle persists via POST upsert."""
    db_recipe_repository.create_recipe(
        _recipe_data(is_public=False), user_id=None, guest_session_id="g1"
    )
    recipe = db_recipe_repository.create_recipe(
        _recipe_data(is_public=True), user_id=None, guest_session_id="g1"
    )
    assert recipe is not None
    assert recipe.is_public is False
    assert recipe.data["is_public"] is False


def test_user_upsert_can_flip_public(app, user):
    """REGRESSION: authenticated publish via the same upsert branch still works."""
    db_recipe_repository.create_recipe(_recipe_data(is_public=False), user_id=user.id)
    recipe = db_recipe_repository.create_recipe(
        _recipe_data(is_public=True), user_id=user.id
    )
    assert recipe is not None
    assert recipe.is_public is True


def test_guest_create_without_flag_still_works(app):
    """REGRESSION: plain guest saves (no is_public key) are unaffected."""
    recipe = db_recipe_repository.create_recipe(
        _recipe_data(), user_id=None, guest_session_id="g1"
    )
    assert recipe is not None
    assert recipe.is_public is False
    assert recipe.name == "Chili"


# ─── Repository gate: update path ────────────────────────────────────────


def test_guest_update_blob_sanitized(app):
    """REGRESSION (update path): PUT never wrote the column, but a guest must
    not be able to smuggle is_public=true into the persisted JSON blob."""
    db_recipe_repository.create_recipe(
        _recipe_data(is_public=False), user_id=None, guest_session_id="g1"
    )
    recipe = db_recipe_repository.update_recipe(
        "r-1", _recipe_data(is_public=True), user_id=None, guest_session_id="g1"
    )
    assert recipe is not None
    assert recipe.is_public is False
    assert recipe.data["is_public"] is False


def test_guest_publish_attempt_is_logged(app, caplog):
    with caplog.at_level("WARNING"):
        db_recipe_repository.create_recipe(
            _recipe_data(is_public=True), user_id=None, guest_session_id="g1"
        )
    assert any("publish" in rec.message.lower() for rec in caplog.records)


# ─── Data migration logic (scripts/gate_guest_public_recipes.py) ─────────


def _add_row(recipe_id, slug, is_public, user_id=None, guest_session_id=None):
    r = Recipe(
        id=recipe_id,
        user_id=user_id,
        guest_session_id=guest_session_id,
        name=slug,
        slug=slug,
        is_public=is_public,
        # Pre-gate rows carried the flag inside the blob too.
        data={"id": recipe_id, "name": slug, "is_public": is_public},
    )
    db.session.add(r)
    db.session.commit()
    return r


def test_gate_unpublishes_guest_rows_without_email(app, user):
    _add_row("g-pub", "guest-pub", True, guest_session_id="g1")
    _add_row("u-pub", "user-pub", True, user_id=user.id)

    summary = run_gate(db.session, reassign_email=None)

    gated = Recipe.query.get("g-pub")
    assert gated.is_public is False
    assert gated.data["is_public"] is False  # blob agrees with the column
    assert Recipe.query.get("u-pub").is_public is True  # untouched
    assert summary == {"found": 1, "reassigned": 0, "unpublished": 1}


def test_gate_reassigns_guest_rows_to_email(app, user):
    _add_row("g-pub", "guest-pub", True, guest_session_id="g1")

    summary = run_gate(db.session, reassign_email="owner@example.com")

    row = Recipe.query.get("g-pub")
    assert row.is_public is True  # page stays live
    assert row.user_id == user.id  # now accountable
    assert row.guest_session_id is None
    assert summary == {"found": 1, "reassigned": 1, "unpublished": 0}


def test_gate_unknown_email_aborts(app):
    _add_row("g-pub", "guest-pub", True, guest_session_id="g1")

    with pytest.raises(RuntimeError):
        run_gate(db.session, reassign_email="nobody@example.com")

    # Nothing changed — the migration (and therefore the deploy) aborts.
    assert Recipe.query.get("g-pub").is_public is True


def test_gate_noop_when_no_guest_public_rows(app, user):
    _add_row("u-pub", "user-pub", True, user_id=user.id)

    summary = run_gate(db.session, reassign_email=None)

    assert summary == {"found": 0, "reassigned": 0, "unpublished": 0}
    assert Recipe.query.get("u-pub").is_public is True
