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
    recipe = db_recipe_repository.create_recipe(_recipe_data(is_public=True), user_id=user.id)
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
    recipe = db_recipe_repository.create_recipe(_recipe_data(is_public=True), user_id=user.id)
    assert recipe is not None
    assert recipe.is_public is True


def test_guest_create_without_flag_still_works(app):
    """REGRESSION: plain guest saves (no is_public key) are unaffected."""
    recipe = db_recipe_repository.create_recipe(_recipe_data(), user_id=None, guest_session_id="g1")
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


def test_user_put_updates_publish_columns(app, user):
    db_recipe_repository.create_recipe(_recipe_data(is_public=False), user_id=user.id)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user.id

    response = client.put(
        "/api/recipes/r-1",
        json=_recipe_data(slug="published-chili", is_public=True),
    )

    assert response.status_code == 200
    assert response.json["slug"] == "published-chili"
    assert response.json["is_public"] is True
    recipe = db.session.get(Recipe, "r-1")
    assert recipe.slug == "published-chili"
    assert recipe.is_public is True
    assert recipe.data["slug"] == "published-chili"
    assert recipe.data["is_public"] is True

    response = client.put(
        "/api/recipes/r-1",
        json=_recipe_data(slug="private-chili", is_public=False),
    )

    assert response.status_code == 200
    assert response.json["slug"] == "private-chili"
    assert response.json["is_public"] is False
    db.session.refresh(recipe)
    assert recipe.slug == "private-chili"
    assert recipe.is_public is False
    assert recipe.data["slug"] == "private-chili"
    assert recipe.data["is_public"] is False


def test_guest_publish_attempt_is_logged(app, caplog):
    with caplog.at_level("WARNING"):
        db_recipe_repository.create_recipe(
            _recipe_data(is_public=True), user_id=None, guest_session_id="g1"
        )
    assert any("publish" in rec.message.lower() for rec in caplog.records)


# ─── Publish slug gate: public rows must carry a usable slug ──────────────


def test_publish_without_slug_derives_from_name(app, user):
    """PR #152 review: PUT with is_public=true and no slug must not persist
    a public row with slug=NULL (browsable but unlinkable via /r/<slug>)."""
    data = _recipe_data(recipe_id="r-1", name="Spicy Chili!")
    del data["slug"]
    recipe = db_recipe_repository.create_recipe(data, user_id=user.id)
    assert recipe is not None

    updated = db_recipe_repository.update_recipe(
        "r-1", {**data, "is_public": True}, user_id=user.id
    )
    assert updated is not None
    assert updated.slug == "spicy-chili"
    assert updated.data["slug"] == "spicy-chili"
    assert updated.is_public is True


def test_publish_create_without_slug_derives_from_name(app, user):
    data = _recipe_data(recipe_id="r-1", name="Spicy Chili!", is_public=True)
    del data["slug"]
    recipe = db_recipe_repository.create_recipe(data, user_id=user.id)
    assert recipe is not None
    assert recipe.slug == "spicy-chili"
    assert recipe.data["slug"] == "spicy-chili"


def test_publish_update_without_slug_keeps_existing_slug(app, user):
    db_recipe_repository.create_recipe(
        _recipe_data(slug="chili-classic", is_public=True), user_id=user.id
    )
    data = _recipe_data(is_public=True)
    del data["slug"]
    updated = db_recipe_repository.update_recipe("r-1", data, user_id=user.id)
    assert updated is not None
    assert updated.slug == "chili-classic"
    assert updated.data["slug"] == "chili-classic"


def test_publish_slug_collision_gets_suffix(app, user):
    db_recipe_repository.create_recipe(
        _recipe_data(recipe_id="r-1", is_public=True), user_id=user.id
    )
    data = _recipe_data(recipe_id="r-2", is_public=True)
    del data["slug"]
    second = db_recipe_repository.create_recipe(data, user_id=user.id)
    assert second is not None
    assert second.slug == "chili-2"


def test_publish_provided_slug_is_sanitized(app, user):
    recipe = db_recipe_repository.create_recipe(
        _recipe_data(slug="Chili Con Carne!", is_public=True), user_id=user.id
    )
    assert recipe is not None
    assert recipe.slug == "chili-con-carne"
    assert recipe.data["slug"] == "chili-con-carne"


def test_publish_with_unusable_slug_and_name_raises(app, user):
    with pytest.raises(db_recipe_repository.RecipeSlugError):
        db_recipe_repository.create_recipe(
            _recipe_data(name="!!!", slug="***", is_public=True), user_id=user.id
        )


def test_put_publish_with_unusable_slug_returns_400(app, user):
    slugless = _recipe_data(name="Chili")
    del slugless["slug"]
    db_recipe_repository.create_recipe(slugless, user_id=user.id)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user.id

    response = client.put(
        "/api/recipes/r-1",
        json=_recipe_data(name="!!!", slug="***", is_public=True),
    )

    assert response.status_code == 400
    assert "slug" in response.json["error"].lower()
    # Nothing was persisted: the row is still private.
    recipe = db.session.get(Recipe, "r-1")
    assert recipe.is_public is False


def test_put_publish_without_slug_returns_derived_slug(app, user):
    db_recipe_repository.create_recipe(_recipe_data(name="Chili"), user_id=user.id)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user.id

    payload = _recipe_data(name="Chili", is_public=True)
    del payload["slug"]
    response = client.put("/api/recipes/r-1", json=payload)

    assert response.status_code == 200
    assert response.json["slug"] == "chili"
    assert response.json["is_public"] is True


def test_post_publish_with_unusable_slug_returns_400(app, user):
    """PR #152 review: the POST error mapping needs route-level coverage too,
    not just the repository-level RecipeSlugError assertion."""
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user.id

    response = client.post(
        "/api/recipes",
        json=_recipe_data(name="!!!", slug="***", is_public=True),
    )

    assert response.status_code == 400
    assert "slug" in response.json["error"].lower()
    # Nothing was persisted.
    assert db.session.get(Recipe, "r-1") is None


def test_publish_slug_truncated_to_column_limit(app, user):
    """PR #152 review: Recipe.slug is String(255); an unbounded payload slug
    must be capped instead of blowing up at commit on PostgreSQL."""
    recipe = db_recipe_repository.create_recipe(
        _recipe_data(slug="x" * 300, is_public=True), user_id=user.id
    )
    assert recipe is not None
    assert recipe.slug == "x" * 255


def test_publish_slug_suffix_respects_column_limit(app, user):
    """A collision suffix on a max-length base must not push past 255."""
    db_recipe_repository.create_recipe(
        _recipe_data(recipe_id="r-1", slug="x" * 300, is_public=True), user_id=user.id
    )
    second = db_recipe_repository.create_recipe(
        _recipe_data(recipe_id="r-2", slug="x" * 300, is_public=True), user_id=user.id
    )
    assert second is not None
    assert len(second.slug) == 255
    assert second.slug == "x" * 253 + "-2"


def test_publish_slug_race_retries_with_next_suffix(app, user, monkeypatch):
    """PR #152 review: two concurrent publications can both probe a slug as
    free; the loser's commit hits the unique index. The repository must retry
    with the next suffix instead of surfacing a failed create."""
    real_resolve = db_recipe_repository._resolve_public_slug
    state = {"raced": False}

    def racing_resolve(data, recipe_id, current_slug=None, skip=frozenset()):
        slug = real_resolve(data, recipe_id, current_slug, skip=skip)
        if not state["raced"]:
            state["raced"] = True
            # The "other writer" claims the probed slug before our commit.
            db.session.add(
                Recipe(
                    id="racer",
                    user_id=None,
                    name="Racer",
                    slug=slug,
                    is_public=False,
                    data={"id": "racer", "name": "Racer"},
                )
            )
            db.session.commit()
        return slug

    monkeypatch.setattr(db_recipe_repository, "_resolve_public_slug", racing_resolve)

    recipe = db_recipe_repository.create_recipe(_recipe_data(is_public=True), user_id=user.id)
    assert recipe is not None
    assert recipe.slug == "chili-2"
    assert recipe.data["slug"] == "chili-2"


def test_publish_slug_double_race_uses_next_suffix(app, user, monkeypatch):
    """PR #152 review: losing two races in a row must yield chili-3, not
    chili-2-2 — each retry resolves from the original payload, not from the
    previous attempt's already-suffixed slug."""
    real_resolve = db_recipe_repository._resolve_public_slug
    state = {"races": 0}

    def racing_resolve(data, recipe_id, current_slug=None, skip=frozenset()):
        slug = real_resolve(data, recipe_id, current_slug, skip=skip)
        if state["races"] < 2:
            state["races"] += 1
            # The "other writer" claims the probed slug before our commit.
            db.session.add(
                Recipe(
                    id=f"racer-{state['races']}",
                    user_id=None,
                    name="Racer",
                    slug=slug,
                    is_public=False,
                    data={"id": f"racer-{state['races']}", "name": "Racer"},
                )
            )
            db.session.commit()
        return slug

    monkeypatch.setattr(db_recipe_repository, "_resolve_public_slug", racing_resolve)

    recipe = db_recipe_repository.create_recipe(_recipe_data(is_public=True), user_id=user.id)
    assert recipe is not None
    assert recipe.slug == "chili-3"
    assert recipe.data["slug"] == "chili-3"


def test_private_update_without_slug_unaffected(app, user):
    """Private rows never trip the slug gate (guest saves send no slug)."""
    db_recipe_repository.create_recipe(_recipe_data(name="Chili"), user_id=user.id)
    data = _recipe_data(name="Chili v2")
    del data["slug"]
    updated = db_recipe_repository.update_recipe("r-1", data, user_id=user.id)
    assert updated is not None
    assert updated.is_public is False


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
