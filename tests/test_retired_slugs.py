"""KAN-288: a published recipe's /r/<slug> is never reassigned to another recipe.

Everything goes through the API (KAN-213 lesson: an ORM-built row can "prove"
a state the product cannot reach), except the migration backfill, which runs
the real SQL on a bare connection.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app
from extensions import db
from models import Recipe, RetiredSlug
from models.user import User

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "b7e2f0c4d9a1_retire_published_slugs.py"
)


@pytest.fixture
def app():
    app = create_app(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _user(email):
    user = User(email=email, name=email.split("@")[0])
    db.session.add(user)
    db.session.commit()
    return user


def _client_for(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
    return client


@pytest.fixture
def adam(app):
    return _client_for(app, _user("adam@example.com"))


@pytest.fixture
def other(app):
    return _client_for(app, _user("other@example.com"))


def _publish(client, recipe_id, name="Zucchini Poppers"):
    resp = client.post("/api/recipes", json={"id": recipe_id, "name": name, "is_public": True})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def _unpublish(client, recipe_id):
    resp = client.put(f"/api/recipes/{recipe_id}", json={"is_public": False})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _unpublish_and_delete(client, recipe_id):
    _unpublish(client, recipe_id)
    resp = client.delete(f"/api/recipes/{recipe_id}")
    assert resp.status_code == 200, resp.get_json()


def test_deleting_a_published_recipe_is_refused_with_409(app, adam):
    created = _publish(adam, "zp-1")

    resp = adam.delete("/api/recipes/zp-1")

    assert resp.status_code == 409
    assert "Unpublish it before deleting it" in resp.get_json()["error"]
    row = db.session.get(Recipe, "zp-1")
    assert row is not None and row.is_public and row.slug == created["slug"]
    assert db.session.get(RetiredSlug, created["slug"]) is None


def test_deleted_slug_answers_410_and_is_never_given_to_another_recipe(app, adam, other):
    slug = _publish(adam, "zp-1")["slug"]
    assert slug == "zucchini-poppers"
    _unpublish_and_delete(adam, "zp-1")

    retired = db.session.get(RetiredSlug, slug)
    assert retired is not None and retired.recipe_id is None, "a delete retires for good"

    assert _publish(other, "zp-other")["slug"] == "zucchini-poppers-2"

    client = app.test_client()
    page = client.get(f"/r/{slug}")
    assert page.status_code == 410
    assert b"This recipe was removed" in page.data
    assert client.get(f"/api/recipes/public/{slug}").status_code == 410
    sitemap = client.get("/sitemap.xml").get_data(as_text=True)
    assert f"/r/{slug}<" not in sitemap
    assert "/r/zucchini-poppers-2<" in sitemap


def test_restoring_a_deleted_recipe_cannot_reclaim_its_slug(app, adam):
    """The bin's restore re-POSTs the same id and slug; the URL stays retired."""
    slug = _publish(adam, "zp-1")["slug"]
    _unpublish_and_delete(adam, "zp-1")

    resp = adam.post(
        "/api/recipes",
        json={"id": "zp-1", "name": "Zucchini Poppers", "slug": slug, "is_public": True},
    )

    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["slug"] == "zucchini-poppers-2"
    assert app.test_client().get(f"/r/{slug}").status_code == 410


def test_deleting_a_renamed_recipe_permanently_retires_every_alias(app, adam):
    _publish(adam, "zp-1")
    renamed = adam.put("/api/recipes/zp-1", json={"slug": "zucchini-poppers-deluxe"})
    assert renamed.status_code == 200, renamed.get_json()
    _unpublish_and_delete(adam, "zp-1")

    assert db.session.get(RetiredSlug, "zucchini-poppers").recipe_id is None
    assert db.session.get(RetiredSlug, "zucchini-poppers-deluxe").recipe_id is None

    restored = adam.post(
        "/api/recipes",
        json={
            "id": "zp-1",
            "name": "Zucchini Poppers",
            "slug": "zucchini-poppers",
            "is_public": True,
        },
    )

    assert restored.status_code == 201, restored.get_json()
    assert restored.get_json()["slug"] == "zucchini-poppers-2"
    public = app.test_client()
    assert public.get("/r/zucchini-poppers").status_code == 410
    assert public.get("/r/zucchini-poppers-deluxe").status_code == 410


def test_private_row_carrying_a_retired_slug_cannot_publish_under_it(app, adam, other):
    """A private row stores any payload slug unvalidated; publishing must re-check it."""
    slug = _publish(adam, "zp-1")["slug"]
    _unpublish_and_delete(adam, "zp-1")

    resp = other.post("/api/recipes", json={"id": "sneaky", "name": "Something else", "slug": slug})
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["slug"] is None, "a private row must not occupy a retired alias"

    resp = other.put("/api/recipes/sneaky", json={"is_public": True})

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["slug"] != slug


def test_unpublished_recipe_is_404_not_410(app, adam):
    """Unpublishing is reversible, so its URL is 'not here', not 'gone'."""
    slug = _publish(adam, "zp-1")["slug"]
    _unpublish(adam, "zp-1")

    assert app.test_client().get(f"/r/{slug}").status_code == 404
    assert db.session.get(RetiredSlug, slug) is None


def test_never_published_recipe_retires_nothing_on_delete(app, adam):
    resp = adam.post("/api/recipes", json={"id": "draft", "name": "Draft", "slug": "draft-slug"})
    assert resp.status_code == 201
    assert resp.get_json()["first_published_at"] is None

    assert adam.delete("/api/recipes/draft").status_code == 200

    assert db.session.get(RetiredSlug, "draft-slug") is None
    assert app.test_client().get("/r/draft-slug").status_code == 404


def test_first_published_at_is_exposed_and_survives_unpublish(app, adam):
    """KAN-289's irreversible-delete warning keys on this, not on slug presence."""
    first = _publish(adam, "zp-1")["first_published_at"]
    assert first is not None

    assert _unpublish(adam, "zp-1")["first_published_at"] == first
    listed = adam.get("/api/recipes").get_json()["recipes"]
    assert next(recipe for recipe in listed if recipe["id"] == "zp-1")["first_published_at"] == first


def test_renamed_slug_301s_to_the_same_recipe_and_can_be_reclaimed(app, adam, other):
    _publish(adam, "zp-1")

    resp = adam.put("/api/recipes/zp-1", json={"slug": "zucchini-poppers-deluxe"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["slug"] == "zucchini-poppers-deluxe"

    old = app.test_client().get("/r/zucchini-poppers?utm_source=pin")
    assert old.status_code == 301
    assert old.headers["Location"].endswith("/r/zucchini-poppers-deluxe?utm_source=pin")
    assert _publish(other, "zp-other")["slug"] == "zucchini-poppers-2"

    resp = adam.put("/api/recipes/zp-1", json={"slug": "zucchini-poppers"})
    assert resp.get_json()["slug"] == "zucchini-poppers", "a renamed recipe may take its slug back"


def test_migration_backfill_marks_slugged_rows_and_retires_orphaned_source_slugs(app):
    spec = importlib.util.spec_from_file_location("kan288_migration", _MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    db.session.add_all(
        [
            Recipe(id="live", name="Live", slug="live", data={}),
            Recipe(id="renamed", name="Renamed", slug="renamed-now", data={}),
            Recipe(id="draft", name="Draft", data={}),
            Recipe(id="copy", name="Copy", source_slug="deleted-source", data={}),
            Recipe(id="copy2", name="Copy", source_slug="live", data={}),
            Recipe(
                id="renamed-copy",
                name="Copy",
                source_slug="renamed-old",
                source_recipe_id="renamed",
                data={},
            ),
        ]
    )
    db.session.commit()

    with db.engine.begin() as conn:
        migration._backfill(conn)
        migration._backfill(conn)  # idempotent

    db.session.expire_all()
    assert db.session.get(Recipe, "live").first_published_at is not None
    assert db.session.get(Recipe, "draft").first_published_at is None
    assert db.session.get(RetiredSlug, "deleted-source").recipe_id is None
    assert db.session.get(RetiredSlug, "renamed-old").recipe_id == "renamed"
    assert db.session.get(RetiredSlug, "live") is None
