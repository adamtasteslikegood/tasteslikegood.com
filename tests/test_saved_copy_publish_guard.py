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


# ─── KAN-221: the guard keys on PERSISTED provenance, which is not ──────
# ─── client-clearable (Copilot B1 on PR #279) ───────────────────────────


def _client_for(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
    return client


def _save_copy_via_api(app, user, source_slug, name="Chili"):
    """Create a saved copy the way the SPA does — through the API, never the
    ORM (KAN-213 lesson: an ORM-built row can 'prove' a state the product
    cannot reach)."""
    client = _client_for(app, user)
    resp = client.post(
        "/api/recipes",
        json={"name": name, "sourceSlug": source_slug, "origin": "saved"},
    )
    assert resp.status_code == 201, resp.get_json()
    return client, resp.get_json()


def test_put_null_source_slug_cannot_bypass_the_guard_or_wipe_provenance(
    app, saver, published_recipe
):
    """THE bypass this re-key exists to close.

    While the guard keyed on the payload, ``PUT {"is_public": true,
    "sourceSlug": null}`` wiped the provenance and published the copy in one
    call. It must now 403, and BOTH provenance columns must survive intact.
    """
    client, created = _save_copy_via_api(app, saver, published_recipe.slug)

    resp = client.put(
        f"/api/recipes/{created['id']}",
        json={"is_public": True, "sourceSlug": None},
    )

    assert resp.status_code == 403, (
        f"expected 403, got {resp.status_code} — a null sourceSlug in the "
        "payload must not blind a guard keyed on the persisted columns"
    )
    row = db.session.get(Recipe, created["id"])
    assert row.source_slug == published_recipe.slug, "source_slug was wiped by the refused PUT"
    assert row.source_recipe_id == published_recipe.id, "source_recipe_id was wiped"
    assert row.is_public is False
    assert row.data.get("sourceSlug") == published_recipe.slug, "blob must stay honest too"


def test_put_null_source_slug_alone_cannot_clear_provenance(app, saver, published_recipe):
    """Even without publishing, a client PUT can never null persisted provenance.

    Otherwise the bypass just becomes two calls: clear first, publish second.
    """
    client, created = _save_copy_via_api(app, saver, published_recipe.slug)

    resp = client.put(
        f"/api/recipes/{created['id']}",
        json={"name": "Renamed", "sourceSlug": None},
    )

    assert resp.status_code == 200
    row = db.session.get(Recipe, created["id"])
    assert row.source_slug == published_recipe.slug
    assert row.source_recipe_id == published_recipe.id

    # And the two-call publish attempt still fails.
    resp = client.put(f"/api/recipes/{created['id']}", json={"is_public": True})
    assert resp.status_code == 403


def test_saved_copy_resolves_source_recipe_id_at_create(app, saver, published_recipe):
    """The stable provenance key is resolved server-side when the copy is saved."""
    _, created = _save_copy_via_api(app, saver, published_recipe.slug)

    row = db.session.get(Recipe, created["id"])
    assert row.source_recipe_id == published_recipe.id
    assert row.source_slug == published_recipe.slug


def test_source_deleted_copy_stays_blocked(app, author, saver, published_recipe):
    """Locked rule: deleting the source does not free its copies for publishing."""
    client, created = _save_copy_via_api(app, saver, published_recipe.slug)

    assert db_recipe_repository.delete_recipe(published_recipe.id, user_id=author.id)

    resp = client.put(f"/api/recipes/{created['id']}", json={"is_public": True})
    assert resp.status_code == 403, "a copy whose source is gone must stay unpublishable"
    row = db.session.get(Recipe, created["id"])
    assert row.is_public is False
    assert row.source_slug == published_recipe.slug, "the pointer text survives the delete"


def test_pre_guard_published_copy_cannot_republish_after_unpublish(app, saver, published_recipe):
    """Rows published BEFORE the guard existed are covered too.

    The guard keys on persisted provenance, so a legacy is_public=True saved
    copy that gets unpublished can never come back — no author exception, no
    payload trick. Built via the ORM deliberately: the API can no longer
    create this row, which is the point of testing it.
    """
    legacy = Recipe(
        id="legacy-published-copy",
        user_id=saver.id,
        name="Chili",
        slug="chili-2",
        source_slug=published_recipe.slug,
        source_recipe_id=published_recipe.id,
        is_public=True,
        data={"name": "Chili", "sourceSlug": published_recipe.slug, "is_public": True},
    )
    db.session.add(legacy)
    db.session.commit()

    client = _client_for(app, saver)
    assert client.put(f"/api/recipes/{legacy.id}", json={"is_public": False}).status_code == 200

    resp = client.put(f"/api/recipes/{legacy.id}", json={"is_public": True})
    assert resp.status_code == 403
    row = db.session.get(Recipe, legacy.id)
    assert row.is_public is False
    assert row.source_recipe_id == published_recipe.id


# ─── KAN-221: locked guest→user merge rules (Copilot B2 on PR #279) ─────


def _guest_client(app, session_id="guest-merge-session"):
    client = app.test_client()
    with client.session_transaction() as sess:
        # The app keys the guest scope on session["session_id"]
        # (utils/session_utils.get_or_create_session_id).
        sess["session_id"] = session_id
    return client


def test_merge_guest_saved_copy_sets_author_from_source_and_saver_to_new_user(
    app, author, saver, published_recipe
):
    """Locked rule: guest-SAVED copy -> author = source owner, saved_to = new user."""
    from blueprints.auth_api_bp import _merge_guest_session_into_user

    guest = _guest_client(app)
    resp = guest.post(
        "/api/recipes",
        json={"name": "Chili", "sourceSlug": published_recipe.slug, "origin": "saved"},
    )
    assert resp.status_code == 201
    recipe_id = resp.get_json()["id"]
    row = db.session.get(Recipe, recipe_id)
    assert row.user_id_saved_to is None, "precondition: guests have no user id to save to"

    _merge_guest_session_into_user(saver, "guest-merge-session")

    row = db.session.get(Recipe, recipe_id)
    assert row.user_id == saver.id
    assert row.user_id_author == author.id, "author must be the SOURCE row's owner"
    assert row.user_id_saved_to == saver.id, "saver must be the user logging in"
    assert row.source_recipe_id == published_recipe.id


def test_merge_guest_generated_original_sets_author_and_saver_to_new_user(app, saver):
    """Locked rule: guest-GENERATED original -> author = saved_to = new user."""
    from blueprints.auth_api_bp import _merge_guest_session_into_user

    guest = _guest_client(app)
    resp = guest.post(
        "/api/recipes",
        json={"name": "Guest Soup", "origin": "generated"},
    )
    assert resp.status_code == 201
    recipe_id = resp.get_json()["id"]
    row = db.session.get(Recipe, recipe_id)
    assert row.user_id_author is None, "precondition: guest originals have no author id"

    _merge_guest_session_into_user(saver, "guest-merge-session")

    row = db.session.get(Recipe, recipe_id)
    assert row.user_id == saver.id
    assert row.user_id_author == saver.id, "the guest IS the author — the new user claims it"
    assert row.user_id_saved_to == saver.id
