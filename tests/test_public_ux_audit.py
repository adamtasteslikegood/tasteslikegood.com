"""Tests for the cookbook #3164 public UX / dead-links audit fixes.

Covers:
- image endpoint content-type matches the served bytes (JPEG-as-PNG fix)
- anonymous public-image responses carry no Set-Cookie / CORS / Vary headers
  and keep Cache-Control: public (CDN-cacheable)
- _recipe_image_url ignores a stale ai_image_url with no persisted bytes
  (no og:image / hero <img> instead of a dead 404 URL)
- 404/500 error pages render from the public base with .org branding and
  CTAs that exist in production (/browse, /#kitchen)
- scripts/unpublish_slugs.py is idempotent and reversible
"""

import base64
import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from scripts.unpublish_slugs import unpublish_slugs  # noqa: E402

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg-payload"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
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


def _make_recipe(name, slug, *, public=True, data=None):
    return Recipe(
        id=str(uuid.uuid4()),
        user_id=None,
        name=name,
        slug=slug,
        is_public=public,
        data=data or {"name": name, "description": f"{name} description"},
    )


def _add_public_image_recipe(app, image_bytes=JPEG_BYTES):
    with app.app_context():
        recipe = _make_recipe(
            "Imageful",
            f"imageful-{uuid.uuid4().hex[:8]}",
            data={
                "name": "Imageful",
                "ai_image_data": base64.b64encode(image_bytes).decode("ascii"),
            },
        )
        db.session.add(recipe)
        db.session.commit()
        return recipe.id


# ── image endpoint hygiene ────────────────────────────────────────────


def test_image_content_type_matches_jpeg_bytes(app, client):
    recipe_id = _add_public_image_recipe(app, JPEG_BYTES)

    resp = client.get(f"/api/recipes/{recipe_id}/image")

    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"
    assert resp.data == JPEG_BYTES


def test_image_content_type_matches_png_bytes(app, client):
    recipe_id = _add_public_image_recipe(app, PNG_BYTES)

    resp = client.get(f"/api/recipes/{recipe_id}/image")

    assert resp.status_code == 200
    assert resp.mimetype == "image/png"


def test_public_image_response_is_shared_cacheable(app, client):
    """No Set-Cookie, no Vary, no CORS headers, Cache-Control: public."""
    recipe_id = _add_public_image_recipe(app)

    resp = client.get(
        f"/api/recipes/{recipe_id}/image",
        headers={"Origin": "https://www.tasteslikegood.org"},
    )

    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=86400"
    assert "Set-Cookie" not in resp.headers
    assert "Vary" not in resp.headers
    assert "Access-Control-Allow-Origin" not in resp.headers
    assert "Access-Control-Allow-Credentials" not in resp.headers


def test_cors_still_applies_to_other_api_endpoints(app, client):
    """The CORS strip is scoped to the image endpoint only."""
    with app.app_context():
        recipe = _make_recipe("Json Public", "json-public")
        db.session.add(recipe)
        db.session.commit()

    resp = client.get(
        "/api/recipes/public/json-public",
        headers={"Origin": "https://www.tasteslikegood.org"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.tasteslikegood.org"


# ── stale ai_image_url guard ──────────────────────────────────────────


def test_stale_ai_image_url_without_bytes_emits_no_og_image_or_hero(app, client):
    with app.app_context():
        recipe = _make_recipe(
            "Ghost Image",
            "ghost-image",
            data={
                "name": "Ghost Image",
                "description": "ai_image_url recorded but bytes never persisted.",
                "ai_image_url": "/api/recipes/00000000-dead-dead-dead-000000000000/image",
            },
        )
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/ghost-image")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "og:image" not in body
    assert "public-recipe-image-wrap" not in body  # no hero <img>
    assert '<meta name="twitter:card" content="summary">' in body


def test_stale_ai_image_url_falls_back_to_stock_image(app, client):
    with app.app_context():
        recipe = _make_recipe(
            "Stocky",
            "stocky",
            data={
                "name": "Stocky",
                "ai_image_url": "/api/recipes/00000000-dead-dead-dead-000000000000/image",
                "stock_image_url": "https://img.example/stock.jpg",
            },
        )
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/stocky")
    body = resp.get_data(as_text=True)

    assert '<meta property="og:image" content="https://img.example/stock.jpg">' in body


def test_browse_card_ignores_stale_ai_image_url(app, client):
    with app.app_context():
        db.session.add(
            _make_recipe(
                "Ghost Card",
                "ghost-card",
                data={
                    "name": "Ghost Card",
                    "ai_image_url": "/api/recipes/00000000-dead-dead-dead-000000000000/image",
                },
            )
        )
        db.session.commit()

    resp = client.get("/browse")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Ghost Card" in body
    assert "00000000-dead-dead-dead-000000000000" not in body


# ── error pages ───────────────────────────────────────────────────────


def test_404_page_renders_from_public_base_with_org_branding(client):
    resp = client.get("/r/this-slug-does-not-exist")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 404
    assert "TastesLikeGood.org" in body
    assert "TastesLikeGood.com" not in body
    assert 'href="/browse"' in body
    assert 'href="/#kitchen"' in body
    # Legacy dev-only routes must not be offered as CTAs.
    assert "/generate_recipe" not in body
    # Public-base chrome proves the re-parenting.
    assert "public-shell" in body


def test_500_page_renders_from_public_base_with_org_branding(app):
    from flask import abort  # noqa: F401

    @app.route("/test/boom")
    def boom():
        raise RuntimeError("boom")

    client = app.test_client()
    resp = client.get("/test/boom")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 500
    assert "TastesLikeGood.org" in body
    assert "TastesLikeGood.com" not in body
    assert 'href="/browse"' in body
    assert "/generate_recipe" not in body


# ── unpublish script ──────────────────────────────────────────────────


def test_unpublish_slugs_is_idempotent_and_reversible(app):
    with app.app_context():
        db.session.add_all(
            [
                _make_recipe("Junk One", "junk-one"),
                _make_recipe("Junk Two", "junk-two"),
                _make_recipe("Keeper", "keeper"),
            ]
        )
        db.session.commit()

    unpublished, already, missing = unpublish_slugs(app, ["junk-one", "junk-two", "nope"])
    assert sorted(unpublished) == ["junk-one", "junk-two"]
    assert already == []
    assert missing == ["nope"]

    # Second run: nothing left to do, still safe.
    unpublished2, already2, missing2 = unpublish_slugs(app, ["junk-one", "junk-two"])
    assert unpublished2 == []
    assert sorted(already2) == ["junk-one", "junk-two"]
    assert missing2 == []

    with app.app_context():
        assert Recipe.query.filter_by(slug="junk-one").one().is_public is False
        assert Recipe.query.filter_by(slug="keeper").one().is_public is True
        # Reversible: flip back and the recipe is public again.
        recipe = Recipe.query.filter_by(slug="junk-one").one()
        recipe.is_public = True
        db.session.commit()
        assert Recipe.query.filter_by(slug="junk-one").one().is_public is True


def test_unpublish_slugs_syncs_the_data_blob(app):
    # The recipes API returns recipe.data verbatim and a later full save
    # writes data["is_public"] back to the column, so the blob must be
    # unpublished together with the column or the recipe silently
    # republishes on the next save.
    with app.app_context():
        db.session.add(
            _make_recipe(
                "Blob Junk",
                "blob-junk",
                data={"name": "Blob Junk", "is_public": True},
            )
        )
        db.session.commit()

    unpublish_slugs(app, ["blob-junk"])

    with app.app_context():
        recipe = Recipe.query.filter_by(slug="blob-junk").one()
        assert recipe.is_public is False
        assert recipe.data["is_public"] is False


def test_unpublished_recipes_leave_browse_and_sitemap(app, client):
    with app.app_context():
        db.session.add(_make_recipe("Junk Three", "junk-three"))
        db.session.commit()

    assert client.get("/r/junk-three").status_code == 200

    unpublish_slugs(app, ["junk-three"])

    assert client.get("/r/junk-three").status_code == 404
    assert "junk-three" not in client.get("/browse").get_data(as_text=True)
    assert b"junk-three" not in client.get("/sitemap.xml").data
