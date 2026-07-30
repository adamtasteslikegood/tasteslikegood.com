"""KAN-155 — the ownership refusal must be legible, and must stay a refusal.

Two tests, and the second is the load-bearing one.

``create_recipe()`` refuses to write when the target row is owned by another
account or guest session. That refusal is **correct** — Adam walked the full
save → publish → unpublish → republish cycle in production on 2026-07-29 and
confirmed it (KAN-181, INV-4: "the refusal is correct, do not loosen it"). What
was broken is that the refusal returned a bare ``None``, which the route turned
into ``500 {"error": "Failed to create recipe"}``, which the SPA turned into
"Publishing failed to sync to the server. Check your connection and try again."
The user was told their network was at fault for a deliberate permission
decision.

So the fix changes the **signal**, not the **decision**:

* ``test_foreign_account_row_is_still_refused`` pins the decision. It passes
  before and after the fix and **fails if ``same_owner`` is ever loosened**.
  Without this test, "publish now succeeds" and "publish is now correct" are
  indistinguishable, and the obvious way to make KAN-155's symptom disappear is
  to ship a privilege escalation on recipe rows.
* ``test_ownership_refusal_is_distinguishable_from_server_error`` pins the
  signal — a distinct exception and a 409, never a 500.
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
def owner(app):
    u = User(email="owner@example.com", name="Owner")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def intruder(app):
    u = User(email="someone-else@example.com", name="Someone Else")
    db.session.add(u)
    db.session.commit()
    return u


def _recipe_data(recipe_id="r-owned", **overrides):
    data = {"id": recipe_id, "name": "English Breakfast", "slug": "english-breakfast"}
    data.update(overrides)
    return data


# ─── The decision: must not change ───────────────────────────────────────


def test_foreign_account_row_is_still_refused(app, owner, intruder):
    """KAN-181 INV-4. A different authenticated account cannot write the row.

    This is the test that must keep passing. If a future change relaxes
    ``same_owner`` so that KAN-155's toast goes away, this fails — which is the
    entire point. A green suite without this assertion cannot tell "publish
    works" apart from "publish works on other people's recipes".
    """
    created = db_recipe_repository.create_recipe(_recipe_data(), user_id=owner.id)
    assert created is not None
    assert created.user_id == owner.id

    with pytest.raises(db_recipe_repository.RecipeOwnershipError):
        db_recipe_repository.create_recipe(
            _recipe_data(name="Hijacked", is_public=True), user_id=intruder.id
        )

    # The refusal is not cosmetic: nothing was written. Read the row straight
    # off the model rather than through get_recipe_by_id(), which scopes by
    # owner and would mask a write by returning None for the wrong reason.
    db.session.expire_all()
    row = Recipe.query.filter_by(id="r-owned").first()
    assert row is not None
    assert row.user_id == owner.id
    assert row.name == "English Breakfast"
    assert row.is_public is False


def test_guest_session_row_is_refused_for_a_different_guest(app):
    """Same decision on the guest path — a guest cannot write another guest's row."""
    created = db_recipe_repository.create_recipe(
        _recipe_data(recipe_id="r-guest"), user_id=None, guest_session_id="session-a"
    )
    assert created is not None

    with pytest.raises(db_recipe_repository.RecipeOwnershipError):
        db_recipe_repository.create_recipe(
            _recipe_data(recipe_id="r-guest", name="Hijacked"),
            user_id=None,
            guest_session_id="session-b",
        )


def test_owner_can_still_write_their_own_row(app, owner):
    """The refusal must not have become indiscriminate."""
    db_recipe_repository.create_recipe(_recipe_data(), user_id=owner.id)
    updated = db_recipe_repository.create_recipe(
        _recipe_data(name="English Breakfast, revised"), user_id=owner.id
    )
    assert updated is not None
    assert updated.name == "English Breakfast, revised"


# ─── The signal: this is what KAN-155 changes ────────────────────────────


def test_ownership_refusal_is_distinguishable_from_server_error(app, owner, intruder):
    """Fails on pre-KAN-155 code, where the refusal was a bare ``None``.

    ``RecipeOwnershipError`` must also survive ``create_recipe``'s own
    ``except Exception`` handler — that handler swallows into ``return None``,
    and a new exception type that is not on the re-raise allowlist would be
    silently converted back into the very failure mode this fixes.
    """
    db_recipe_repository.create_recipe(_recipe_data(), user_id=owner.id)

    with pytest.raises(db_recipe_repository.RecipeOwnershipError) as excinfo:
        db_recipe_repository.create_recipe(_recipe_data(), user_id=intruder.id)

    # Fixed string — no exception internals may reach a client
    # (CodeQL py/stack-trace-exposure), same rule as the sibling errors.
    assert str(excinfo.value) == db_recipe_repository.RECIPE_OWNERSHIP_ERROR


def test_route_answers_409_not_500(app, owner, intruder):
    """The route must not report a deliberate refusal as an internal failure."""
    db_recipe_repository.create_recipe(_recipe_data(), user_id=owner.id)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = intruder.id

    resp = client.post("/api/recipes", json=_recipe_data(name="Hijacked"))

    assert resp.status_code == 409, (
        "A row owned by another account is a conflict, not a server error. "
        "500 here is what made the SPA blame the user's connection."
    )
    assert resp.get_json()["error"] == db_recipe_repository.RECIPE_OWNERSHIP_ERROR
