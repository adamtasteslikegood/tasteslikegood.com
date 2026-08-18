"""Tests for the public SSR routes (TAS-2718).

Covers:
- /r/<slug> renders only published recipes, 404 for private / missing
- /r/<slug> emits SEO metadata + Recipe JSON-LD for public recipe pages
- /browse paginates and only lists is_public=True recipes
- /browse uses eager loading (joinedload) to avoid N+1 on Recipe.user
- /sitemap.xml lists only public routes
- /api/recipes/<id>/image is readable without ownership when is_public=True
"""

import base64
import html
import re
import sys
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import event

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from blueprints.public_bp import _format_ingredient, _safe_minutes  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    # _public_base_url() reads FRONTEND_URL per-request; a developer's .env
    # (e.g. http://localhost:8080) must not leak into the canonical-URL and
    # sitemap assertions. These tests pin the request-derived base.
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


def _make_recipe(name, slug, *, public=True, owner=None, data=None):
    return Recipe(
        id=str(uuid.uuid4()),
        user_id=owner.id if owner else None,
        name=name,
        slug=slug,
        is_public=public,
        data=data or {"name": name, "description": f"{name} description"},
    )


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ([], "cup lentils"),
        ([1], "1 cup lentils"),
        ([1, 2], "1\u20132 cup lentils"),
    ],
)
def test_format_ingredient_handles_all_amount_array_lengths(amount, expected):
    assert _format_ingredient({"amount": amount, "units": "cup", "name": "lentils"}) == expected


@pytest.mark.parametrize("value", ["Infinity", "-Infinity", float("inf"), float("-inf")])
def test_safe_minutes_rejects_infinite_values(value):
    assert _safe_minutes(value) is None


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
    assert '<script defer src="/static/js/public.js"></script>' in body
    assert "document.querySelectorAll('[data-open-kitchen]')" not in body

    public_js = (Path(__file__).resolve().parent.parent / "static/js/public.js").read_text(
        encoding="utf-8"
    )
    assert 'event.key === "Escape"' in public_js
    assert "lastFocused.focus()" in public_js
    assert 'event.key !== "Tab"' in public_js


def test_show_public_recipe_includes_seo_meta_and_json_ld(app, client):
    png_bytes = b"\x89PNG\r\n\x1a\nseo"
    with app.app_context():
        recipe = _make_recipe(
            "Thai Peanut Noodles",
            "thai-peanut-noodles",
            data={
                "name": "Thai Peanut Noodles",
                "description": "Creamy noodles with a spicy peanut sauce.",
                "prepTime": 15,
                "cookTime": 20,
                "servings": 4,
                "tags": ["vegan", "noodles"],
                "ingredients": {
                    "dry": [
                        {
                            "name": "Rice noodles",
                            "amount": 12,
                            "units": "oz",
                        }
                    ]
                },
                "instructions": [
                    {"step": 1, "description": "Boil the noodles."},
                    {"step": 2, "description": "Whisk the sauce."},
                ],
                "ai_image_data": base64.b64encode(png_bytes).decode("ascii"),
            },
        )
        db.session.add(recipe)
        db.session.commit()
        recipe_id = recipe.id

    resp = client.get("/r/thai-peanut-noodles")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '<link rel="canonical" href="http://localhost/r/thai-peanut-noodles">' in body
    assert '<meta property="og:title" content="Thai Peanut Noodles · TastesLikeGood">' in body
    assert (
        f'<meta property="og:image" '
        f'content="http://localhost/api/recipes/{recipe_id}/image">' in body
    )
    assert '<meta name="twitter:card" content="summary_large_image">' in body
    assert 'type="application/ld+json"' in body
    assert '"@type": "Recipe"' in body
    assert '"prepTime": "PT15M"' in body
    assert '"cookTime": "PT20M"' in body
    assert '"totalTime": "PT35M"' in body
    assert "Save to your cookbook" in body
    assert "Save to Pinterest" in body


def test_pinterest_button_hidden_when_recipe_has_no_image(app, client):
    # A recipe with no fetchable image (no ai_image_gcs/ai_image_data/stock)
    # must not render a "Save to Pinterest" link — pinning it would create a
    # broken pin whose media 404s.
    with app.app_context():
        recipe = _make_recipe(
            "Imageless Stew",
            "imageless-stew",
            data={"name": "Imageless Stew", "description": "No picture yet."},
        )
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/imageless-stew")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Save to Pinterest" not in body
    # The cookbook CTA is unconditional and must still be present.
    assert "Save to your cookbook" in body


def _pinterest_media_param(body: str) -> str:
    match = re.search(r'href="(https://www\.pinterest\.com/pin/create/button/\?[^"]+)"', body)
    assert match, "no Pinterest share link in page"
    href = html.unescape(match.group(1))
    return parse_qs(urlsplit(href).query)["media"][0]


@pytest.mark.parametrize(
    "image_field, expected_media",
    [
        (
            {"ai_image_data": base64.b64encode(b"\x89PNGpin").decode("ascii")},
            "endpoint",
        ),
        ({"ai_image_gcs": "gs://bucket/recipe/v1.png"}, "endpoint"),
        ({"stock_image_url": "https://img.example/stock.jpg"}, "https://img.example/stock.jpg"),
        (
            # Stored bytes win over whatever ai_image_url claims — the pin
            # media must be the URL the gate actually verified.
            {
                "ai_image_data": base64.b64encode(b"\x89PNGpin").decode("ascii"),
                "ai_image_url": "https://cdn.example/old.png",
            },
            "endpoint",
        ),
    ],
)
def test_pinterest_button_shown_when_recipe_has_image(app, client, image_field, expected_media):
    with app.app_context():
        recipe = _make_recipe(
            "Pinnable Pie",
            "pinnable-pie",
            data={"name": "Pinnable Pie", "description": "Has a photo.", **image_field},
        )
        db.session.add(recipe)
        db.session.commit()
        recipe_id = recipe.id

    resp = client.get("/r/pinnable-pie")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Save to Pinterest" in body
    if expected_media == "endpoint":
        expected_media = f"http://localhost/api/recipes/{recipe_id}/image"
    assert _pinterest_media_param(body) == expected_media


def test_pinterest_media_ignores_stale_ai_url_when_stock_exists(app, client):
    # Regression (Copilot review on #202): a stale ai_image_url with no
    # stored bytes next to a valid stock image passed the gate via the stock
    # URL but shipped the dead ai_image_url as the pin media. The media must
    # be the stock URL — the value that made the recipe pinnable.
    with app.app_context():
        recipe = _make_recipe(
            "Stale Link Curry",
            "stale-link-curry",
            data={
                "name": "Stale Link Curry",
                "description": "AI image bytes were never persisted.",
                "ai_image_url": "/api/recipes/00000000-dead-dead-dead-000000000000/image",
                "stock_image_url": "https://img.example/stock.jpg",
            },
        )
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/stale-link-curry")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Save to Pinterest" in body
    assert _pinterest_media_param(body) == "https://img.example/stock.jpg"


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
    assert '<link rel="canonical" href="http://localhost/browse">' in body


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
    assert '<link rel="canonical" href="http://localhost/browse?page=2">' in second_body


def test_browse_uses_joinedload_and_avoids_n_plus_one(app, client):
    """Eagerly loading Recipe.user keeps queries constant regardless of row count."""
    with app.app_context():
        for idx in range(5):
            owner = User(email=f"chef-{idx}@example.com", name=f"Chef {idx}")
            db.session.add(owner)
            db.session.flush()
            db.session.add(_make_recipe(f"Owned {idx}", f"owned-{idx}", owner=owner))
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


def test_partial_recipe_omits_blank_metadata_and_formats_amount_ranges(app, client):
    with app.app_context():
        recipe = _make_recipe(
            "Flexible Pantry Soup",
            "flexible-pantry-soup",
            data={
                "name": "Flexible Pantry Soup",
                "ingredients": {
                    "soup": [
                        {"amount": [1], "units": "cup", "name": "lentils"},
                        {"amount": [1, 2], "units": "tbsp", "name": "lemon juice"},
                    ]
                },
            },
        )
        db.session.add(recipe)
        db.session.commit()

    body = client.get("/r/flexible-pantry-soup").get_data(as_text=True)

    assert "Prep m" not in body
    assert "Cook m" not in body
    assert "Serves </li>" not in body
    assert "1–</span>" not in body
    assert "1–2" in body


def test_public_recipe_filters_malformed_tags_from_json_ld(app, client):
    with app.app_context():
        recipe = _make_recipe(
            "Tagged Soup",
            "tagged-soup",
            data={
                "name": "Tagged Soup",
                "tags": ["vegan", 7, None, {"bad": "shape"}, " soup "],
            },
        )
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/tagged-soup")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '"keywords": "vegan, soup"' in body
    assert '<span class="public-tag">vegan</span>' in body
    assert '<span class="public-tag">soup</span>' in body
    assert '<span class="public-tag">7</span>' not in body

    payload = client.get("/api/recipes/public/tagged-soup").get_json()
    assert payload["tags"] == ["vegan", "soup"]


@pytest.mark.parametrize("tags", ["not a list", 42, {"bad": "shape"}])
def test_public_recipe_ignores_non_list_tags(app, client, tags):
    with app.app_context():
        recipe = _make_recipe(
            "Malformed Tags",
            "malformed-tags",
            data={"name": "Malformed Tags", "tags": tags},
        )
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/malformed-tags")

    assert resp.status_code == 200
    assert "public-recipe-tags" not in resp.get_data(as_text=True)
    assert client.get("/api/recipes/public/malformed-tags").get_json()["tags"] == []


@pytest.mark.parametrize(
    "ingredients",
    [
        [{"name": "not grouped"}],
        {"main": ["not an ingredient", None]},
    ],
)
def test_public_recipe_ignores_malformed_ingredient_shapes(app, client, ingredients):
    with app.app_context():
        recipe = _make_recipe(
            "Malformed Ingredients",
            "malformed-ingredients",
            data={
                "name": "Malformed Ingredients",
                "ingredients": ingredients,
            },
        )
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/malformed-ingredients")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "public-recipe-ingredients" not in body
    assert '"recipeIngredient"' not in body


@pytest.mark.parametrize(
    "instructions",
    [
        "not a list",
        {"step": "not a list"},
        42,
        [None, 7, {"description": ""}],
    ],
)
def test_public_recipe_ignores_malformed_instruction_shapes(app, client, instructions):
    with app.app_context():
        recipe = _make_recipe(
            "Malformed Instructions",
            "malformed-instructions",
            data={
                "name": "Malformed Instructions",
                "instructions": instructions,
            },
        )
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/malformed-instructions")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "public-recipe-instructions" not in body
    assert '"recipeInstructions"' not in body

    payload = client.get("/api/recipes/public/malformed-instructions").get_json()
    assert payload["instructions"] == []


def test_sitemap_lists_only_public_routes(app, client):
    with app.app_context():
        db.session.add_all(
            [
                _make_recipe("Public One", "public-one"),
                _make_recipe("Public Two", "public-two"),
                _make_recipe("Private", "private", public=False),
            ]
        )
        db.session.commit()

    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert resp.mimetype == "application/xml"
    body = resp.get_data(as_text=True)
    assert "http://localhost/" in body
    assert "http://localhost/browse" in body
    assert "http://localhost/r/public-one" in body
    assert "http://localhost/r/public-two" in body
    assert "http://localhost/r/private" not in body


def test_sitemap_selects_only_slug_and_timestamps(app, client):
    with app.app_context():
        db.session.add(
            _make_recipe(
                "Large Public Image",
                "large-public-image",
                data={"name": "Large Public Image", "ai_image_data": "x" * 100_000},
            )
        )
        db.session.commit()

    statements = []

    @event.listens_for(db.engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _params, _ctx, _exec):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    try:
        resp = client.get("/sitemap.xml")
    finally:
        event.remove(db.engine, "before_cursor_execute", _capture)

    assert resp.status_code == 200
    recipe_queries = [statement for statement in statements if "FROM recipe" in statement]
    assert recipe_queries
    assert all("recipe.data" not in statement for statement in recipe_queries)


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
    assert resp.headers["Cache-Control"] == "public, max-age=86400"


def test_public_recipe_image_uses_stored_versioned_gcs_uri(app, client):
    gcs_uri = "gs://recipe-images/images/recipe-id/lease-token.png"
    png_bytes = b"\x89PNG\r\n\x1a\ngcs"
    with app.app_context():
        recipe = _make_recipe(
            "Public GCS Image",
            "public-gcs-image",
            data={
                "name": "Public GCS Image",
                "ai_image_gcs": gcs_uri,
                "ai_image_url": "/api/recipes/recipe-id/image",
            },
        )
        db.session.add(recipe)
        db.session.commit()
        recipe_id = recipe.id

    with (
        patch("blueprints.generation_api_bp.GCS_BUCKET_NAME", "recipe-images"),
        patch(
            "services.gcs_service.download_image",
            return_value=png_bytes,
        ) as download,
    ):
        resp = client.get(f"/api/recipes/{recipe_id}/image")

    assert resp.status_code == 200
    assert resp.data == png_bytes
    download.assert_called_once_with("recipe-images", recipe_id, gcs_uri)


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


def test_cached_private_recipe_image_still_requires_ownership(app, client, monkeypatch):
    png_bytes = b"\x89PNG\r\n\x1a\ncached-secret"
    with app.app_context():
        recipe = _make_recipe(
            "Cached Private Image",
            "cached-private-image",
            public=False,
            data={"name": "Cached Private Image"},
        )
        db.session.add(recipe)
        db.session.commit()
        recipe_id = recipe.id

    monkeypatch.setattr("blueprints.generation_api_bp.safe_get", lambda _key: png_bytes)

    resp = client.get(f"/api/recipes/{recipe_id}/image")
    assert resp.status_code == 404


def test_cached_private_recipe_image_is_not_stored_by_clients(app, client, monkeypatch):
    png_bytes = b"\x89PNG\r\n\x1a\ncached-secret"
    with app.app_context():
        owner = User(email="image-owner@example.com", name="Image Owner")
        db.session.add(owner)
        db.session.commit()
        recipe = _make_recipe(
            "Owned Cached Image",
            "owned-cached-image",
            public=False,
            owner=owner,
            data={"name": "Owned Cached Image"},
        )
        db.session.add(recipe)
        db.session.commit()
        owner_id = owner.id
        recipe_id = recipe.id

    with client.session_transaction() as flask_session:
        flask_session["user_id"] = owner_id

    monkeypatch.setattr("blueprints.generation_api_bp.safe_get", lambda _key: png_bytes)

    resp = client.get(f"/api/recipes/{recipe_id}/image")
    assert resp.status_code == 200
    assert resp.data == png_bytes
    assert resp.headers["Cache-Control"] == "private, no-store"


def test_public_recipe_json_returns_save_payload(app, client):
    with app.app_context():
        recipe = _make_recipe(
            "Thai Peanut Noodles",
            "thai-peanut-noodles",
            data={
                "name": "Thai Peanut Noodles",
                "description": "Creamy noodles",
                "prepTime": 10,
                "cookTime": 20,
                "servings": 4,
                "ingredients": {"wet": [], "dry": [], "other": []},
                "instructions": ["Boil noodles"],
                "tags": ["thai"],
                "stock_image_url": "https://img.example/stock.jpg",
            },
        )
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/api/recipes/public/thai-peanut-noodles")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    payload = resp.get_json()
    assert payload["name"] == "Thai Peanut Noodles"
    assert payload["description"] == "Creamy noodles"
    assert payload["instructions"] == ["Boil noodles"]
    assert payload["stock_image_url"] == "https://img.example/stock.jpg"


def test_public_recipe_json_404_for_private_or_missing(app, client):
    with app.app_context():
        db.session.add(_make_recipe("Secret Stew", "secret-stew", public=False))
        db.session.commit()

    assert client.get("/api/recipes/public/secret-stew").status_code == 404
    assert client.get("/api/recipes/public/never-existed").status_code == 404


def test_public_recipe_page_links_save_cta_to_spa_save_url(app, client):
    with app.app_context():
        recipe = _make_recipe("Thai Peanut Noodles", "thai-peanut-noodles")
        db.session.add(recipe)
        db.session.commit()

    resp = client.get("/r/thai-peanut-noodles")
    body = resp.get_data(as_text=True)
    assert "/?save=thai-peanut-noodles#kitchen" in body


# ---------------------------------------------------------------------------
# KAN-215: saved-copy image fallback from source recipe
# ---------------------------------------------------------------------------


def test_saved_copy_inherits_source_ai_image_via_fallback(app, client):
    """A published saved copy with no image of its own falls back to the
    source recipe's AI image (via source_recipe_id) on /r/<slug>."""
    with app.app_context():
        source = _make_recipe(
            "Original Curry",
            "original-curry",
            data={
                "name": "Original Curry",
                "description": "Has an AI image.",
                "ai_image_gcs": "gs://bucket/curry/v1.png",
            },
        )
        db.session.add(source)
        db.session.flush()

        copy = Recipe(
            id=str(uuid.uuid4()),
            name="Original Curry",
            slug="my-original-curry-copy",
            is_public=True,
            source_slug="original-curry",
            source_recipe_id=source.id,
            data={"name": "Original Curry", "description": "Saved copy."},
        )
        db.session.add(copy)
        db.session.commit()
        source_id = source.id

    resp = client.get("/r/my-original-curry-copy")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The og:image and hero should use the source's image endpoint
    expected_url = f"/api/recipes/{source_id}/image"
    assert expected_url in body


def test_saved_copy_inherits_source_stock_image_via_fallback(app, client):
    """A published saved copy falls back to the source's stock_image_url."""
    with app.app_context():
        source = _make_recipe(
            "Stock Photo Stew",
            "stock-photo-stew",
            data={
                "name": "Stock Photo Stew",
                "description": "Has a stock image.",
                "stock_image_url": "https://img.example/stew.jpg",
            },
        )
        db.session.add(source)
        db.session.flush()

        copy = Recipe(
            id=str(uuid.uuid4()),
            name="Stock Photo Stew",
            slug="my-stock-stew-copy",
            is_public=True,
            source_slug="stock-photo-stew",
            source_recipe_id=source.id,
            data={"name": "Stock Photo Stew", "description": "Saved copy."},
        )
        db.session.add(copy)
        db.session.commit()

    resp = client.get("/r/my-stock-stew-copy")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "https://img.example/stew.jpg" in body


def test_saved_copy_with_own_image_does_not_fall_back(app, client):
    """A copy that already has its own image must use it, not the source's."""
    with app.app_context():
        source = _make_recipe(
            "Source Dish",
            "source-dish",
            data={
                "name": "Source Dish",
                "description": "Source.",
                "stock_image_url": "https://img.example/source.jpg",
            },
        )
        db.session.add(source)
        db.session.flush()

        copy = Recipe(
            id=str(uuid.uuid4()),
            name="Source Dish",
            slug="my-source-dish-copy",
            is_public=True,
            source_slug="source-dish",
            source_recipe_id=source.id,
            data={
                "name": "Source Dish",
                "description": "Copy with own image.",
                "stock_image_url": "https://img.example/copy-own.jpg",
            },
        )
        db.session.add(copy)
        db.session.commit()

    resp = client.get("/r/my-source-dish-copy")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "https://img.example/copy-own.jpg" in body
    assert "https://img.example/source.jpg" not in body


def test_saved_copy_falls_back_via_source_slug_when_no_fk(app, client):
    """Legacy copies with source_slug but no source_recipe_id still resolve."""
    with app.app_context():
        source = _make_recipe(
            "Legacy Source",
            "legacy-source",
            data={
                "name": "Legacy Source",
                "description": "Has image.",
                "ai_image_gcs": "gs://bucket/legacy/v1.png",
            },
        )
        db.session.add(source)
        db.session.flush()

        copy = Recipe(
            id=str(uuid.uuid4()),
            name="Legacy Source",
            slug="legacy-copy",
            is_public=True,
            source_slug="legacy-source",
            source_recipe_id=None,  # FK never backfilled
            data={"name": "Legacy Source", "description": "Legacy copy."},
        )
        db.session.add(copy)
        db.session.commit()
        source_id = source.id

    resp = client.get("/r/legacy-copy")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    expected_url = f"/api/recipes/{source_id}/image"
    assert expected_url in body


def test_saved_copy_no_fallback_when_source_deleted(app, client):
    """When the source recipe is gone, the copy shows no image (not crash)."""
    with app.app_context():
        copy = Recipe(
            id=str(uuid.uuid4()),
            name="Orphaned Copy",
            slug="orphaned-copy",
            is_public=True,
            source_slug="deleted-source",
            source_recipe_id=None,  # source deleted, FK set NULL
            data={"name": "Orphaned Copy", "description": "Source gone."},
        )
        db.session.add(copy)
        db.session.commit()

    resp = client.get("/r/orphaned-copy")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # No image shown, page still renders
    assert "Orphaned Copy" in body


def test_saved_copy_no_fallback_when_source_unpublished(app, client):
    """A private source's image must NOT leak into a public copy's og:image.

    If the source is unpublished after copies were made, _resolve_source_for_image
    must return None — otherwise the copy's page emits a /api/recipes/<source>/image
    URL that 404s for anonymous visitors (and leaks a stock_image_url for a
    private recipe).
    """
    with app.app_context():
        source = Recipe(
            id=str(uuid.uuid4()),
            name="Now-Private Source",
            slug="now-private-source",
            is_public=False,  # unpublished after the copy was saved
            data={
                "name": "Now-Private Source",
                "description": "Was public, now private.",
                "ai_image_gcs": "gs://bucket/private/img.png",
                "stock_image_url": "https://img.example/private-stock.jpg",
            },
        )
        db.session.add(source)
        db.session.flush()

        copy = Recipe(
            id=str(uuid.uuid4()),
            name="Now-Private Source",
            slug="copy-of-now-private",
            is_public=True,
            source_slug="now-private-source",
            source_recipe_id=source.id,  # FK resolves, but source is private
            data={"name": "Now-Private Source", "description": "Copy."},
        )
        db.session.add(copy)
        db.session.commit()
        source_id = source.id

    resp = client.get("/r/copy-of-now-private")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The source's image endpoint must NOT appear
    assert f"/api/recipes/{source_id}/image" not in body
    # The source's stock_image_url must NOT leak either
    assert "https://img.example/private-stock.jpg" not in body
    # Page still renders the recipe content
    assert "Now-Private Source" in body


def test_save_flow_copies_stock_image_url_from_source(app):
    """The repository's stage_new path copies stock_image_url from the source.

    Exercises the save-time copy in db_recipe_repository.create_recipe rather
    than just the read-time fallback. Per KAN-215: stock_image_url is safe to
    copy (external URL, not deleted on regeneration) unlike ai_image_gcs.
    """
    from repositories import db_recipe_repository

    with app.app_context():
        # Create a public source with a stock image
        source = Recipe(
            id="src-stock-001",
            name="Stock Source",
            slug="stock-source",
            is_public=True,
            data={
                "name": "Stock Source",
                "description": "Has stock image.",
                "stock_image_url": "https://img.example/stock-original.jpg",
            },
        )
        db.session.add(source)
        db.session.commit()

        # Save a copy via the repository (the real save flow)
        copy_data = {
            "id": "copy-stock-001",
            "name": "Stock Source",
            "sourceSlug": "stock-source",
        }
        owner = User(email="saver@example.com", name="Saver")
        db.session.add(owner)
        db.session.commit()

        recipe = db_recipe_repository.create_recipe(copy_data, user_id=owner.id)

        assert recipe is not None
        # The copy's data blob must now contain the source's stock_image_url
        assert recipe.data.get("stock_image_url") == "https://img.example/stock-original.jpg"
        # The source_recipe_id FK must be set
        assert recipe.source_recipe_id == "src-stock-001"
