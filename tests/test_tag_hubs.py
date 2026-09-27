"""Curated tag hub pages /browse/tag/<slug> (KAN-274).

Covers:
- hub definitions: tag normalization, aliases, intro length, unique slugs
- a hub page: 200, self-canonical, index when >= 3 recipes, newest first,
  public only, CollectionPage + BreadcrumbList
- a thin hub (< 3 recipes) is noindex, left out of the sitemap and of links
- unknown hubs 404; trailing slash 301s
- hubs in the sitemap, on /browse, and in a recipe's breadcrumb
"""

import json
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
import blueprints.public_bp as public_module  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from services.tag_hubs import TAG_HUBS, hubs_for_tags, normalize_tag  # noqa: E402

BASE = datetime(2026, 9, 1, 12, 0, 0)


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


def _add(slug, tags, *, public=True, days=0, recipe_id=None, **extra):
    recipe = Recipe(
        id=recipe_id or str(uuid.uuid4()),
        name=slug.replace("-", " ").title(),
        slug=slug,
        is_public=public,
        data={
            "name": slug,
            "description": f"{slug} description.",
            "tags": list(tags),
            **extra,
        },
        created_at=BASE + timedelta(days=days),
        updated_at=BASE + timedelta(days=days),
    )
    db.session.add(recipe)
    db.session.commit()
    return recipe.id


def _json_ld(body: str, type_name: str) -> dict:
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', body, re.S):
        data: dict = json.loads(block)
        if data.get("@type") == type_name:
            return data
    raise AssertionError(f"no {type_name} JSON-LD")


# ── definitions ──────────────────────────────────────────────────────────────


def test_normalize_tag_folds_case_hyphens_and_spacing():
    assert normalize_tag("  Gluten-Free ") == "gluten free"
    assert normalize_tag("Tex-Mex") == "tex mex"
    assert normalize_tag("Comfort   Food") == "comfort food"


def test_aliases_merge_related_tags_into_one_hub():
    assert [hub.slug for hub in hubs_for_tags(["Brunch"])] == ["breakfast"]
    assert [hub.slug for hub in hubs_for_tags(["tacos"])] == ["mexican"]
    assert [hub.slug for hub in hubs_for_tags(["Italian"])] == ["pasta"]
    assert [hub.slug for hub in hubs_for_tags(["High-Protein"])] == ["high-protein"]
    assert hubs_for_tags(["vegan", "plant based", 7, None]) == []


def test_hub_definitions_are_unique_and_have_real_intros():
    slugs = [hub.slug for hub in TAG_HUBS]
    assert len(slugs) == len(set(slugs))
    for hub in TAG_HUBS:
        assert 80 <= len(hub.intro.split()) <= 100, hub.slug
        assert "vegan" in hub.title.lower(), hub.slug
        assert re.fullmatch(r"[a-z]+(?:-[a-z]+)*", hub.slug), hub.slug


# ── the hub page ─────────────────────────────────────────────────────────────


def test_hub_page_lists_public_members_newest_first(app, client):
    with app.app_context():
        _add("old-stew", ["Dinner"], days=1)
        _add(
            "new-curry",
            ["dinner", "spicy"],
            days=5,
            ai_image_gcs="gs://bucket/new-curry.png",
        )
        _add("mid-pie", ["main course"], days=3)
        _add("private-roast", ["dinner"], public=False, days=9)
        _add("a-cookie", ["dessert"], days=7)

    resp = client.get("/browse/tag/dinner")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<h1>Vegan Dinner Recipes</h1>" in body
    assert '<meta name="robots" content="index,follow">' in body
    assert '<link rel="canonical" href="http://localhost/browse/tag/dinner">' in body
    assert '<meta property="og:image:alt" content="New Curry">' in body
    assert '<meta name="twitter:image:alt" content="New Curry">' in body
    cards = re.findall(r'<li class="public-browse-item">\s*<a href="/r/([^"]+)"', body)
    assert cards == ["new-curry", "mid-pie", "old-stew"]

    collection = _json_ld(body, "CollectionPage")
    assert collection["description"] == next(h.intro for h in TAG_HUBS if h.slug == "dinner")
    items = collection["mainEntity"]["itemListElement"]
    assert [item["url"] for item in items] == [
        "http://localhost/r/new-curry",
        "http://localhost/r/mid-pie",
        "http://localhost/r/old-stew",
    ]
    crumbs = _json_ld(body, "BreadcrumbList")["itemListElement"]
    assert [crumb["name"] for crumb in crumbs] == ["Home", "Browse", "Vegan Dinner Recipes"]


def test_hub_page_breaks_created_at_ties_by_id(app, client):
    with app.app_context():
        for suffix, slug in (
            ("001", "first-dinner"),
            ("002", "second-dinner"),
            ("003", "third-dinner"),
        ):
            _add(
                slug,
                ["dinner"],
                recipe_id=f"00000000-0000-0000-0000-000000000{suffix}",
            )

    body = client.get("/browse/tag/dinner").get_data(as_text=True)
    cards = re.findall(r'<li class="public-browse-item">\s*<a href="/r/([^"]+)"', body)
    assert cards == ["third-dinner", "second-dinner", "first-dinner"]


def test_hub_refetch_rechecks_visibility(app, client, monkeypatch):
    with app.app_context():
        leaked_id = _add("unpublished-dinner", ["dinner"], days=9)
        for index in range(2):
            _add(f"visible-dinner-{index}", ["dinner"], days=index)

        # Preserve an indexable three-member snapshot, then simulate a concurrent
        # unpublish before the route hydrates the selected full Recipe rows.
        catalog_snapshot = public_module._catalog_tag_rows()
        leaked = db.session.get(Recipe, leaked_id)
        leaked.is_public = False
        db.session.commit()

    monkeypatch.setattr(public_module, "_catalog_tag_rows", lambda: catalog_snapshot)
    response = client.get("/browse/tag/dinner")
    body = response.get_data(as_text=True)

    assert "Unpublished Dinner" not in body
    items = _json_ld(body, "CollectionPage")["mainEntity"]["itemListElement"]
    assert all(not item["url"].endswith("/r/unpublished-dinner") for item in items)
    assert '<meta name="robots" content="noindex,follow">' in body
    assert response.headers["X-Robots-Tag"] == "noindex, follow"


def test_hub_refetch_rechecks_membership(app, client, monkeypatch):
    with app.app_context():
        moved_id = _add("retagged-dinner", ["dinner"], days=9)
        for index in range(2):
            _add(f"visible-dinner-{index}", ["dinner"], days=index)

        # Preserve an indexable three-member snapshot, then simulate a concurrent
        # retag before the route hydrates the selected full Recipe rows.
        catalog_snapshot = public_module._catalog_tag_rows()
        moved = db.session.get(Recipe, moved_id)
        moved_data = dict(moved.data or {})
        moved_data["tags"] = ["dessert"]
        moved.data = moved_data
        db.session.commit()

    monkeypatch.setattr(public_module, "_catalog_tag_rows", lambda: catalog_snapshot)
    response = client.get("/browse/tag/dinner")
    body = response.get_data(as_text=True)

    assert "Retagged Dinner" not in body
    items = _json_ld(body, "CollectionPage")["mainEntity"]["itemListElement"]
    assert all(not item["url"].endswith("/r/retagged-dinner") for item in items)
    assert '<meta name="robots" content="noindex,follow">' in body
    assert response.headers["X-Robots-Tag"] == "noindex, follow"


def test_thin_hub_is_noindex_and_unlinked(app, client):
    with app.app_context():
        _add("only-pancake", ["breakfast"])
        _add("only-waffle", ["brunch"])
        for index in range(3):
            _add(f"dinner-{index}", ["dinner"], days=index)

    response = client.get("/browse/tag/breakfast")
    hub = response.get_data(as_text=True)
    assert '<meta name="robots" content="noindex,follow">' in hub
    assert response.headers["X-Robots-Tag"] == "noindex, follow"

    sitemap = client.get("/sitemap.xml").get_data(as_text=True)
    assert "/browse/tag/breakfast" not in sitemap
    assert "<loc>http://localhost/browse/tag/dinner</loc>" in sitemap

    browse = client.get("/browse").get_data(as_text=True)
    assert "/browse/tag/breakfast" not in browse
    assert 'href="http://localhost/browse/tag/dinner"' in browse


def test_unknown_hub_is_404(client):
    assert client.get("/browse/tag/not-a-hub").status_code == 404


def test_hub_trailing_slash_redirects(client):
    resp = client.get("/browse/tag/dinner/")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "http://localhost/browse/tag/dinner"


def test_hub_trailing_slash_redirect_preserves_allowlisted_query_params(client):
    resp = client.get(
        "/browse/tag/dinner/?utm_source=email&utm_campaign=fall&save=recipe-123&next=/admin"
    )
    assert resp.status_code == 301
    assert resp.headers["Location"] == (
        "http://localhost/browse/tag/dinner" "?utm_source=email&utm_campaign=fall&save=recipe-123"
    )


def test_unknown_hub_trailing_slash_is_404_directly(client):
    # A 301 → 404 chain wastes crawl budget and shows up in Search Console as
    # a redirect error; an unknown slug should 404 in one hop regardless of
    # the trailing slash.
    assert client.get("/browse/tag/not-a-hub/").status_code == 404


def test_sitemap_hub_lastmod_is_newest_member(app, client):
    with app.app_context():
        for index in range(3):
            _add(f"soup-{index}", ["dinner"], days=index * 2)

    sitemap = client.get("/sitemap.xml").get_data(as_text=True)
    entry = re.search(r"<loc>http://localhost/browse/tag/dinner</loc><lastmod>([^<]+)", sitemap)
    assert entry and entry.group(1) == (BASE + timedelta(days=4)).date().isoformat()


# ── hubs in the recipe breadcrumb ────────────────────────────────────────────


def test_recipe_breadcrumb_runs_through_its_first_indexable_hub(app, client):
    with app.app_context():
        for index in range(3):
            _add(f"brunch-{index}", ["brunch", "dinner"], days=index)

    body = client.get("/r/brunch-0").get_data(as_text=True)
    crumbs = _json_ld(body, "BreadcrumbList")["itemListElement"]
    assert [(crumb["name"], crumb["item"]) for crumb in crumbs] == [
        ("Home", "http://localhost/"),
        ("Browse", "http://localhost/browse"),
        ("Vegan Breakfast Recipes", "http://localhost/browse/tag/breakfast"),
        ("Brunch 0", "http://localhost/r/brunch-0"),
    ]
    assert '<a href="http://localhost/browse/tag/breakfast">Vegan Breakfast Recipes</a>' in body


def test_recipe_breadcrumb_skips_a_thin_hub(app, client):
    with app.app_context():
        _add("lonely-pancake", ["breakfast"])

    body = client.get("/r/lonely-pancake").get_data(as_text=True)
    names = [c["name"] for c in _json_ld(body, "BreadcrumbList")["itemListElement"]]
    assert names == ["Home", "Browse", "Lonely Pancake"]
