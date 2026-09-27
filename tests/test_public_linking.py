"""Internal linking and snippet hygiene on the public SSR pages (KAN-273).

Covers:
- "More vegan recipes": ranked by shared tags, newest on ties, public only,
  never the page itself, rendered as real anchors
- BreadcrumbList JSON-LD + the visible trail
- recipeCategory / recipeCuisine derived from tags; Vegan as suitableForDiet
- title suffix dropped when it would push the title past 60 characters
- meta description cut at a sentence (or word) boundary; JSON-LD keeps it whole
- /r/<slug>/ → 301 → /r/<slug>
- /browse: count in the title, CollectionPage + ItemList, og:image
"""

import html
import json
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from blueprints.public_bp import _meta_description, _page_title  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402

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


def _add(slug, *, tags=(), public=True, days=0, name=None, description=None, **extra):
    name = name or slug.replace("-", " ").title()
    recipe = Recipe(
        id=str(uuid.uuid4()),
        name=name,
        slug=slug,
        is_public=public,
        data={
            "name": name,
            "description": description or f"{name} description.",
            "tags": list(tags),
            **extra,
        },
        created_at=BASE + timedelta(days=days),
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


def _related_slugs(body: str) -> list[str]:
    start = body.find('<section class="public-related"')
    if start == -1:
        return []
    section = body[start : body.index("</section>", start)]
    return re.findall(r'<a href="/r/([^"]+)">', section)


# ── related recipes ──────────────────────────────────────────────────────────


def test_related_recipes_rank_by_shared_tags_then_newest(app, client):
    with app.app_context():
        _add("tofu-stir-fry", tags=["Dinner", "Tofu", "Quick"])
        _add("tofu-dinner-bowl", tags=["tofu", "dinner"], days=1)
        _add("any-dinner", tags=["dinner"], days=2)
        _add("newest-dessert", tags=["dessert"], days=9)
        _add("older-dessert", tags=["dessert"], days=3)
        _add("private-tofu-dinner", tags=["tofu", "dinner", "quick"], public=False, days=5)

    body = client.get("/r/tofu-stir-fry").get_data(as_text=True)
    assert _related_slugs(body) == [
        "tofu-dinner-bowl",  # 2 shared (case-insensitive)
        "any-dinner",  # 1 shared
        "newest-dessert",  # 0 shared, newer
        "older-dessert",
    ]


def test_related_recipes_cap_at_six_and_exclude_the_page(app, client):
    with app.app_context():
        _add("the-page", tags=["dinner"])
        for index in range(9):
            _add(f"dinner-{index}", tags=["dinner"], days=index + 1)

    slugs = _related_slugs(client.get("/r/the-page").get_data(as_text=True))
    assert len(slugs) == 6
    assert "the-page" not in slugs
    assert slugs[0] == "dinner-8"  # newest first among equal scores


def test_related_cards_use_small_image_variants(app, client):
    with app.app_context():
        _add("page-with-photos", tags=["dinner"])
        related_id = _add(
            "photo-dinner", tags=["dinner"], days=1, ai_image_gcs="gs://bucket/photo.png"
        )

    body = client.get("/r/page-with-photos").get_data(as_text=True)
    start = body.find('<section class="public-related"')
    assert f'src="/api/recipes/{related_id}/image?w=400' in body[start:]


def test_no_related_section_when_the_catalog_has_one_recipe(app, client):
    with app.app_context():
        _add("lonely-soup", tags=["soup"])

    body = client.get("/r/lonely-soup").get_data(as_text=True)
    assert "public-related" not in body


# ── breadcrumbs + structured data ────────────────────────────────────────────


def test_breadcrumb_json_ld_and_visible_trail(app, client):
    with app.app_context():
        _add("crumb-pie")

    body = client.get("/r/crumb-pie").get_data(as_text=True)
    crumbs = _json_ld(body, "BreadcrumbList")["itemListElement"]
    assert [(c["position"], c["name"], c["item"]) for c in crumbs] == [
        (1, "Home", "http://localhost/"),
        (2, "Browse", "http://localhost/browse"),
        (3, "Crumb Pie", "http://localhost/r/crumb-pie"),
    ]
    nav = re.search(r'<nav class="public-eyebrow public-breadcrumb".*?</nav>', body, re.S)
    assert nav
    assert '<a href="http://localhost/">Home</a>' in nav.group(0)
    assert '<a href="http://localhost/browse">Browse</a>' in nav.group(0)


def test_category_and_cuisine_come_from_tags(app, client):
    with app.app_context():
        _add("taco-night", tags=["Dinner", "Mexican", "tacos", "brunch"])

    recipe_ld = _json_ld(client.get("/r/taco-night").get_data(as_text=True), "Recipe")
    assert recipe_ld["recipeCategory"] == ["Dinner", "Breakfast"]
    assert recipe_ld["recipeCuisine"] == ["Mexican"]
    assert recipe_ld["suitableForDiet"] == "https://schema.org/VeganDiet"


def test_unmapped_tags_omit_category_and_cuisine(app, client):
    with app.app_context():
        _add("mystery-dish", tags=["comfort food"])

    recipe_ld = _json_ld(client.get("/r/mystery-dish").get_data(as_text=True), "Recipe")
    assert "recipeCategory" not in recipe_ld
    assert "recipeCuisine" not in recipe_ld


# ── titles + descriptions ────────────────────────────────────────────────────


def test_page_title_keeps_suffix_only_when_it_fits():
    assert _page_title("Vegan Cornbread") == "Vegan Cornbread · TastesLikeGood"
    long_name = "Vegan Double Chocolate Chip Cookies with Baked-On Chocolate Drizzle"
    assert _page_title(long_name) == long_name
    assert len(_page_title("x" * 43)) == 60


def test_long_recipe_title_drops_suffix_on_the_page(app, client):
    long_name = "Vegan Double Chocolate Chip Cookies with Baked-On Chocolate Drizzle"
    with app.app_context():
        _add("long-cookies", name=long_name)

    body = html.unescape(client.get("/r/long-cookies").get_data(as_text=True))
    assert f"<title>{long_name}</title>" in body
    assert f'<meta property="og:title" content="{long_name}">' in body


def test_meta_description_cuts_at_last_sentence_before_the_limit():
    text = (
        "A crisp, golden cornbread with a tender crumb and a hint of maple sweetness. "
        "Bake it in a cast-iron skillet for the best crust. "
        "Serve warm with vegan butter, chili, or a big bowl of greens and beans."
    )
    assert _meta_description(text) == (
        "A crisp, golden cornbread with a tender crumb and a hint of maple sweetness. "
        "Bake it in a cast-iron skillet for the best crust."
    )


def test_meta_description_falls_back_to_a_word_boundary():
    text = "word " * 60
    cut = _meta_description(text)
    assert len(cut) <= 156
    assert cut.endswith("word…")


def test_short_description_is_untouched():
    assert _meta_description("  Short   and sweet. ") == "Short and sweet."


def test_page_meta_is_trimmed_but_json_ld_keeps_the_full_description(app, client):
    long_description = ("This stew is hearty and warming. " * 8).strip()
    with app.app_context():
        _add("long-stew", description=long_description)

    body = client.get("/r/long-stew").get_data(as_text=True)
    meta = re.search(r'<meta name="description" content="([^"]*)">', body).group(1)
    assert len(html.unescape(meta)) <= 155
    assert _json_ld(body, "Recipe")["description"] == long_description


# ── trailing slash ───────────────────────────────────────────────────────────


def test_trailing_slash_recipe_url_redirects_to_canonical(app, client):
    with app.app_context():
        _add("slash-soup")

    resp = client.get("/r/slash-soup/")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "http://localhost/r/slash-soup"


# ── /browse ──────────────────────────────────────────────────────────────────


def test_browse_title_description_and_collection_json_ld(app, client):
    with app.app_context():
        _add("first-soup", days=1)
        newest_id = _add("newest-soup", days=2, ai_image_gcs="gs://bucket/newest.png")
        _add("hidden-soup", public=False, days=3)

    body = client.get("/browse").get_data(as_text=True)
    assert "<title>Browse 2 Vegan Recipes with Photos · TastesLikeGood</title>" in body
    description = html.unescape(
        re.search(r'<meta name="description" content="([^"]*)">', body).group(1)
    )
    assert "AI-generated vegan recipes" in description
    assert len(description) <= 160

    collection = _json_ld(body, "CollectionPage")
    items = collection["mainEntity"]["itemListElement"]
    assert [(i["position"], i["url"]) for i in items] == [
        (1, "http://localhost/r/newest-soup"),
        (2, "http://localhost/r/first-soup"),
    ]
    og_image = re.search(r'<meta property="og:image" content="([^"]+)">', body).group(1)
    assert og_image.startswith(f"http://localhost/api/recipes/{newest_id}/image")
    assert '<meta name="twitter:card" content="summary_large_image">' in body


def test_browse_without_photos_has_no_og_image(app, client):
    with app.app_context():
        _add("plain-soup")

    body = client.get("/browse").get_data(as_text=True)
    assert "og:image" not in body
    assert '<meta name="twitter:card" content="summary">' in body


def test_browse_later_pages_say_which_page(app, client):
    with app.app_context():
        for index in range(21):
            _add(f"soup-{index}", days=index)

    body = client.get("/browse?page=2").get_data(as_text=True)
    assert "<title>Vegan Recipes with Photos, Page 2 of 2 · TastesLikeGood</title>" in body
