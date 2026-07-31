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

import logging
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


# ─── Which refusal: one 409 was carrying three situations ────────────────
#
# The message says "a different account or guest session", which reads as final.
# Two of the three are recoverable by the user right now, and only the server has
# seen the stored row — so only the server can say which fired. These tests pin
# the discrimination, not new behaviour: every case below still refuses.


def test_foreign_account_row_reports_other_account(app, owner, intruder):
    """A real, different account owns it. Final — the user cannot fix this."""
    db_recipe_repository.create_recipe(_recipe_data(), user_id=owner.id)

    with pytest.raises(db_recipe_repository.RecipeOwnershipError) as excinfo:
        db_recipe_repository.create_recipe(_recipe_data(), user_id=intruder.id)

    assert excinfo.value.code == db_recipe_repository.OWNERSHIP_CODE_OTHER_ACCOUNT


def test_other_guest_session_reports_other_guest_session(app):
    """RCP-61's stale tab: both guests, different sessions.

    The user very likely owns this row under a session the page no longer holds,
    so 'log in and try again' is the honest remedy. Reporting this as
    OTHER_ACCOUNT would tell them to give up on their own recipe.
    """
    db_recipe_repository.create_recipe(_recipe_data(), guest_session_id="guest-a")

    with pytest.raises(db_recipe_repository.RecipeOwnershipError) as excinfo:
        db_recipe_repository.create_recipe(_recipe_data(), guest_session_id="guest-b")

    assert excinfo.value.code == db_recipe_repository.OWNERSHIP_CODE_OTHER_GUEST_SESSION


def test_orphaned_guest_row_reports_orphaned_guest_row(app, owner):
    """KAN-155's known-incomplete case, called out in #256 as still failing.

    An unclaimed guest row (user_id NULL) and an AUTHENTICATED caller. Distinct
    from OTHER_GUEST_SESSION precisely because the caller is logged in: this is
    the row the login-merge should have claimed, and it is the case the
    ownership-repair policy will act on. It must not be indistinguishable from
    someone else's live guest session.
    """
    db_recipe_repository.create_recipe(_recipe_data(), guest_session_id="orphan-session")

    with pytest.raises(db_recipe_repository.RecipeOwnershipError) as excinfo:
        db_recipe_repository.create_recipe(_recipe_data(), user_id=owner.id)

    assert excinfo.value.code == db_recipe_repository.OWNERSHIP_CODE_ORPHANED_GUEST_ROW


def test_the_three_codes_are_distinct(app):
    """A discriminator that collapses to one value discriminates nothing."""
    codes = {
        db_recipe_repository.OWNERSHIP_CODE_OTHER_ACCOUNT,
        db_recipe_repository.OWNERSHIP_CODE_OTHER_GUEST_SESSION,
        db_recipe_repository.OWNERSHIP_CODE_ORPHANED_GUEST_ROW,
    }
    assert len(codes) == 3


def test_route_sends_the_code_alongside_the_message(app, owner, intruder):
    """The code must reach the client — the repository knowing it is not enough."""
    db_recipe_repository.create_recipe(_recipe_data(), user_id=owner.id)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = intruder.id

    resp = client.post("/api/recipes", json=_recipe_data(name="Hijacked"))
    body = resp.get_json()

    assert resp.status_code == 409
    assert body["code"] == db_recipe_repository.OWNERSHIP_CODE_OTHER_ACCOUNT
    # The prose stays the fallback for any client that does not read `code`.
    assert body["error"] == db_recipe_repository.RECIPE_OWNERSHIP_ERROR


def test_the_refusal_log_carries_the_discriminator(app, owner, caplog):
    """The log is the ONLY place the repair-policy question can be answered.

    KAN-155's open item is reassign-at-login-merge vs one-off backfill, and that
    choice depends on how often ORPHANED_GUEST_ROW fires against OTHER_ACCOUNT in
    production. There is no staging environment (KAN-182), so prod logs are the
    only observation channel. Before this, all three refusals logged the same
    "Recipe ID collision" string and the data simply did not exist.
    """
    db_recipe_repository.create_recipe(_recipe_data(), guest_session_id="orphan-session")

    with caplog.at_level(logging.WARNING, logger=db_recipe_repository.__name__):
        with pytest.raises(db_recipe_repository.RecipeOwnershipError):
            db_recipe_repository.create_recipe(_recipe_data(), user_id=owner.id)

    assert db_recipe_repository.OWNERSHIP_CODE_ORPHANED_GUEST_ROW in caplog.text
    # Not merely "a code appeared" — the RIGHT one. A log that always says
    # OTHER_ACCOUNT would pass a weaker assertion and answer nothing.
    assert db_recipe_repository.OWNERSHIP_CODE_OTHER_ACCOUNT not in caplog.text


def test_every_code_still_refuses_and_writes_nothing(app, owner):
    """INV-4 is unchanged by the split. Narrowing the signal must not soften
    the decision — the row is untouched in all three cases."""
    db_recipe_repository.create_recipe(_recipe_data(), guest_session_id="orphan-session")

    with pytest.raises(db_recipe_repository.RecipeOwnershipError):
        db_recipe_repository.create_recipe(
            _recipe_data(name="Hijacked", is_public=True), user_id=owner.id
        )

    db.session.expire_all()
    row = Recipe.query.filter_by(id="r-owned").first()
    assert row is not None
    assert row.name == "English Breakfast"
    assert row.is_public is False
    assert row.user_id is None
    assert row.guest_session_id == "orphan-session"
