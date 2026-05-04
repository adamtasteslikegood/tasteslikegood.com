"""Tests for the public SSR routes (TAS-2718).

Covers:
- /r/<slug> renders only published recipes, 404 for private / missing
- /browse paginates and only lists is_public=True recipes
- /browse uses eager loading (joinedload) to avoid N+1 on Recipe.user
- /api/recipes/<id>/image is readable without ownership when is_public=True
"""

import base64
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event

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


def _make_recipe(name, slug, *, public=True, owner=None, data=None):
    return Recipe(
        id=str(uuid.uuid4()),
        user_id=owner.id if owner else None,
        name=name,
        slug=slug,
        is_public=public,
        data=data or {"name": name, "description": f"{name} description"},
    )


def test_show_public_recipe_renders_html(app, client):
    with app.app_context():
        recipe = _make_recipe("Thai Peanut Noodles", "thai-peanut-noodles")
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/thai-peanut-noodles")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Thai Peanut Noodles" in body
    assert "Thai Peanut Noodles description" in body


def test_show_public_recipe_404_when_missing(client):
    resp = client.get("/r/does-not-exist")
    assert resp.status_code == 404


def test_show_public_recipe_404_when_private(app, client):
    with app.app_context():
        recipe = _make_recipe("Secret Sauce", "secret-sauce", public=False)
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/secret-sauce")
    assert resp.status_code == 404


def test_browse_lists_only_public_recipes(app, client):
    with app.app_context():
        db.session.add_all(
            [
                _make_recipe("Public One", "public-one"),
                _make_recipe("Public Two", "public-two"),
                _make_recipe("Private", "private", public=False),
            ]
        )
        db.session.commit()

    resp = client.get("/browse")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Public One" in body
    assert "Public Two" in body
    assert "Private" not in body


def test_browse_paginates(app, client):
    """Page size is 20; a page=2 request should render only the older rows."""
    with app.app_context():
        for idx in range(25):
            db.session.add(_make_recipe(f"Recipe {idx:02d}", f"recipe-{idx:02d}"))
        db.session.commit()

    first = client.get("/browse?page=1")
    assert first.status_code == 200
    first_body = first.get_data(as_text=True)
    assert "Page 1 of 2" in first_body

    second = client.get("/browse?page=2")
    assert second.status_code == 200
    second_body = second.get_data(as_text=True)
    assert "Page 2 of 2" in second_body


def test_browse_uses_joinedload_and_avoids_n_plus_one(app, client):
    """Eagerly loading Recipe.user keeps queries constant regardless of row count."""
    with app.app_context():
        owner = User(email="chef@example.com", name="Chef One")
        db.session.add(owner)
        db.session.commit()
        for idx in range(5):
            db.session.add(
                _make_recipe(f"Owned {idx}", f"owned-{idx}", owner=owner)
            )
        db.session.commit()

    select_count = 0

    @event.listens_for(db.engine, "before_cursor_execute")
    def _count(_conn, _cursor, statement, _params, _ctx, _exec):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    try:
        resp = client.get("/browse")
    finally:
        event.remove(db.engine, "before_cursor_execute", _count)

    assert resp.status_code == 200
    # Expect a small, fixed number of SELECTs — count(*) + recipes+user join.
    # Anything above 5 means eager loading regressed and rows are loading users one-by-one.
    assert select_count <= 5, f"expected ≤5 SELECTs, got {select_count} (N+1 regression)"


def test_public_recipe_image_served_without_session(app, client):
    """A public recipe's image should be fetchable by anyone."""
    png_bytes = b"\x89PNG\r\n\x1a\nfake"
    with app.app_context():
        recipe = _make_recipe(
            "Public With Image",
            "public-with-image",
            data={
                "name": "Public With Image",
                "ai_image_data": base64.b64encode(png_bytes).decode("ascii"),
            },
        )
        db.session.add(recipe)
        db.session.commit()
        recipe_id = recipe.id

    resp = client.get(f"/api/recipes/{recipe_id}/image")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data == png_bytes


def test_private_recipe_image_still_requires_ownership(app, client):
    png_bytes = b"\x89PNG\r\n\x1a\nsecret"
    with app.app_context():
        recipe = _make_recipe(
            "Private With Image",
            "private-with-image",
            public=False,
            data={
                "name": "Private With Image",
                "ai_image_data": base64.b64encode(png_bytes).decode("ascii"),
            },
        )
        db.session.add(recipe)
        db.session.commit()
        recipe_id = recipe.id

    resp = client.get(f"/api/recipes/{recipe_id}/image")
    assert resp.status_code == 404
