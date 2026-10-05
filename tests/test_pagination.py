"""Numbered SSR pagination on /browse and the tag hubs (KAN-296).

Covers:
- the page-number window (first, last, current ± 2, ellipsis for longer runs)
- page boundaries on both listings: first, last, last + 1, and an empty listing
- every page is self-canonical; page 1 is the bare URL, never ?page=1
- ?page=1 301s to the bare URL (carrying utm_*), malformed ?page= is a 404
- the CollectionPage ItemList lists exactly the recipes on that page
- a hub's short last page stays indexable
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
from blueprints.public_bp import BROWSE_PAGE_SIZE, HUB_PAGE_SIZE, _page_numbers  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402

BASE = datetime(2026, 9, 1, 12, 0, 0)
CARD = re.compile(r'<li class="public-browse-item">\s*<a href="/r/([^"]+)"')
MALFORMED_PAGES = ["0", "-1", "abc", "2.5", "02", "", " 2", "1e1", "9999999999"]


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


def _seed(count, prefix, tags=("dinner",)):
    """``count`` public recipes; ``<prefix>-0`` is the oldest, so the newest is listed first."""
    for index in range(count):
        slug = f"{prefix}-{index}"
        db.session.add(
            Recipe(
                id=str(uuid.uuid4()),
                name=slug.replace("-", " ").title(),
                slug=slug,
                is_public=True,
                data={"name": slug, "description": f"{slug} description.", "tags": list(tags)},
                created_at=BASE + timedelta(minutes=index),
                updated_at=BASE + timedelta(minutes=index),
            )
        )
    db.session.commit()


def _newest_first(count, prefix):
    return [f"{prefix}-{index}" for index in reversed(range(count))]


def _json_ld(body, type_name):
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', body, re.S):
        data = json.loads(block)
        if data.get("@type") == type_name:
            return data
    raise AssertionError(f"no {type_name} JSON-LD")


def _item_list_slugs(body):
    items = _json_ld(body, "CollectionPage")["mainEntity"]["itemListElement"]
    assert [item["position"] for item in items] == list(range(1, len(items) + 1))
    return [item["url"].rsplit("/r/", 1)[1] for item in items]


def _pagination_nav(body):
    match = re.search(r'<nav class="public-browse-pagination".*?</nav>', body, re.S)
    assert match, "no pagination nav"
    return match.group(0)


# ── the page-number window ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("page", "total_pages", "expected"),
    [
        (1, 1, [1]),
        (1, 5, [1, 2, 3, 4, 5]),
        (1, 10, [1, 2, 3, None, 10]),
        (4, 10, [1, 2, 3, 4, 5, 6, None, 10]),
        (5, 10, [1, 2, 3, 4, 5, 6, 7, None, 10]),
        (6, 12, [1, None, 4, 5, 6, 7, 8, None, 12]),
        (9, 10, [1, None, 7, 8, 9, 10]),
        (10, 10, [1, None, 8, 9, 10]),
    ],
)
def test_page_numbers_window(page, total_pages, expected):
    assert _page_numbers(page, total_pages) == expected


# ── /browse ──────────────────────────────────────────────────────────────────


def test_browse_page_boundaries(app, client):
    total = 2 * BROWSE_PAGE_SIZE + 5
    _seed(total, "soup")
    newest = _newest_first(total, "soup")

    first = client.get("/browse")
    assert first.status_code == 200
    body = first.get_data(as_text=True)
    assert '<link rel="canonical" href="http://localhost/browse">' in body
    assert CARD.findall(body) == newest[:BROWSE_PAGE_SIZE]
    assert _item_list_slugs(body) == newest[:BROWSE_PAGE_SIZE]
    nav = _pagination_nav(body)
    assert 'rel="prev"' not in nav
    assert '<span class="public-page-current" aria-current="page">1</span>' in nav
    assert '<a href="/browse?page=2" aria-label="Page 2">2</a>' in nav
    assert '<a href="/browse?page=3" aria-label="Page 3">3</a>' in nav
    assert '<a href="/browse?page=2" class="public-page-step" rel="next">' in nav

    middle = client.get("/browse?page=2")
    assert middle.status_code == 200
    body = middle.get_data(as_text=True)
    assert '<link rel="canonical" href="http://localhost/browse?page=2">' in body
    assert _item_list_slugs(body) == newest[BROWSE_PAGE_SIZE : 2 * BROWSE_PAGE_SIZE]
    assert _json_ld(body, "CollectionPage")["url"] == "http://localhost/browse?page=2"
    nav = _pagination_nav(body)
    # Page 1 is linked as the bare /browse, both from Previous and from "1".
    assert '<a href="/browse" class="public-page-step" rel="prev">' in nav
    assert '<a href="/browse" aria-label="Page 1">1</a>' in nav
    assert "page=1" not in nav

    last = client.get("/browse?page=3")
    assert last.status_code == 200
    body = last.get_data(as_text=True)
    assert '<link rel="canonical" href="http://localhost/browse?page=3">' in body
    assert CARD.findall(body) == newest[2 * BROWSE_PAGE_SIZE :]
    assert _item_list_slugs(body) == newest[2 * BROWSE_PAGE_SIZE :]
    assert _json_ld(body, "CollectionPage")["mainEntity"]["numberOfItems"] == 5
    nav = _pagination_nav(body)
    assert 'rel="next"' not in nav
    assert "Page 3 of 3" in nav

    assert client.get("/browse?page=4").status_code == 404


def test_browse_single_page_has_no_pagination_nav(app, client):
    _seed(3, "solo")
    body = client.get("/browse").get_data(as_text=True)
    assert "public-browse-pagination" not in body
    assert client.get("/browse?page=2").status_code == 404


def test_browse_empty_catalog(app, client):
    resp = client.get("/browse")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No public recipes yet" in body
    assert "public-browse-pagination" not in body
    assert client.get("/browse?page=2").status_code == 404


def test_browse_page_one_redirects_to_bare_url(app, client):
    _seed(BROWSE_PAGE_SIZE + 1, "redir")
    resp = client.get("/browse?page=1")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "http://localhost/browse"

    resp = client.get("/browse?page=1&utm_source=newsletter&next=https://evil.example")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "http://localhost/browse?utm_source=newsletter"


@pytest.mark.parametrize("raw", MALFORMED_PAGES)
def test_browse_malformed_page_is_404(app, client, raw):
    _seed(BROWSE_PAGE_SIZE + 1, "bad")
    assert client.get("/browse", query_string={"page": raw}).status_code == 404


def test_browse_ignores_other_query_params(app, client):
    """Only ``page``, ``sort`` and ``tag`` are read; anything else is left alone."""
    _seed(2, "other")
    resp = client.get("/browse?ref=home")
    assert resp.status_code == 200
    assert CARD.findall(resp.get_data(as_text=True)) == _newest_first(2, "other")


# ── /browse?sort&tag (KAN-298) ───────────────────────────────────────────────

BROWSE_CANONICAL = '<link rel="canonical" href="http://localhost/browse">'


def test_browse_sort_orders_the_listing(app, client):
    _seed(3, "sorted")
    oldest_first = [f"sorted-{index}" for index in range(3)]

    for query, expected in [
        ("", _newest_first(3, "sorted")),
        ("?sort=newest", _newest_first(3, "sorted")),
        ("?sort=oldest", oldest_first),
        # An unknown sort is not an error: the default order, still a view.
        ("?sort=sideways", _newest_first(3, "sorted")),
    ]:
        resp = client.get(f"/browse{query}")
        assert resp.status_code == 200, query
        assert CARD.findall(resp.get_data(as_text=True)) == expected, query


def test_browse_sort_by_name_ignores_case(app, client):
    _seed(1, "zucchini")
    for name, slug in [("apple Crumble", "apple"), ("Banana Bread", "banana")]:
        db.session.add(
            Recipe(id=str(uuid.uuid4()), name=name, slug=slug, is_public=True, data={"name": name})
        )
    db.session.commit()

    body = client.get("/browse?sort=name").get_data(as_text=True)
    assert CARD.findall(body) == ["apple", "banana", "zucchini-0"]
    assert '<option value="name" selected>' in body


def test_browse_tag_narrows_the_listing(app, client):
    _seed(2, "sweet", tags=("Dessert", "baking"))
    _seed(2, "savoury", tags=("dinner",))

    resp = client.get("/browse", query_string={"tag": "  DESSERT "})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert CARD.findall(body) == _newest_first(2, "sweet")
    assert 'name="tag" value="dessert"' in body
    assert "Showing 2 of 2" in body

    # A tag is matched whole, never as a substring of another tag.
    assert CARD.findall(client.get("/browse?tag=dess").get_data(as_text=True)) == []


def test_browse_tag_matches_multiword_tags_whatever_their_spacing(app, client):
    _seed(1, "comfort", tags=("Comfort   Food ",))
    _seed(1, "plain", tags=("comfort",))
    for raw in ["comfort food", "  Comfort    FOOD "]:
        body = client.get("/browse", query_string={"tag": raw}).get_data(as_text=True)
        assert CARD.findall(body) == ["comfort-0"], raw


def test_browse_tag_rechecks_membership_after_loading(app, client, monkeypatch):
    """A recipe retagged after the catalog snapshot is not rendered in the view."""
    _seed(2, "stay", tags=("dinner",))
    _seed(1, "moved", tags=("dinner",))
    snapshot = public_module._catalog_tag_rows()
    moved = Recipe.query.filter_by(slug="moved-0").one()
    moved.data = {**moved.data, "tags": ["dessert"]}
    db.session.commit()

    monkeypatch.setattr(public_module, "_catalog_tag_rows", lambda: snapshot)
    body = client.get("/browse?tag=dinner").get_data(as_text=True)
    assert CARD.findall(body) == _newest_first(2, "stay")
    items = _json_ld(body, "CollectionPage")["mainEntity"]["itemListElement"]
    assert all(not item["url"].endswith("/r/moved-0") for item in items)


def test_browse_tag_with_no_match_renders_an_empty_page_one(app, client):
    _seed(1, "lonely")
    resp = client.get("/browse?tag=nope")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert CARD.findall(body) == []
    assert "No published recipes are tagged" in body
    assert client.get("/browse?tag=nope&page=2").status_code == 404


@pytest.mark.parametrize(
    "query",
    [
        "sort=name",
        "tag=dinner",
        "sort=oldest&tag=dinner",
        "sort=sideways",
        "tag=",
        "sort=name&page=2",
    ],
)
def test_filtered_browse_is_canonical_to_bare_browse(app, client, query):
    """Every ?sort / ?tag view, on any page, points at /browse (SEO audit row 14)."""
    _seed(BROWSE_PAGE_SIZE + 1, "view")
    resp = client.get(f"/browse?{query}")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert BROWSE_CANONICAL in body
    assert body.count('rel="canonical"') == 1
    assert '<meta property="og:url" content="http://localhost/browse">' in body


def test_unfiltered_pages_and_hubs_keep_their_own_canonical(app, client):
    _seed(BROWSE_PAGE_SIZE + 1, "plain", tags=("breakfast",))
    body = client.get("/browse?page=2").get_data(as_text=True)
    assert '<link rel="canonical" href="http://localhost/browse?page=2">' in body

    hub = client.get("/browse/tag/breakfast").get_data(as_text=True)
    assert '<link rel="canonical" href="http://localhost/browse/tag/breakfast">' in hub
    # A hub is not a filtered view: ?sort and ?tag change nothing on it.
    hub = client.get("/browse/tag/breakfast?sort=name&tag=dinner").get_data(as_text=True)
    assert '<link rel="canonical" href="http://localhost/browse/tag/breakfast">' in hub


def test_filtered_page_links_keep_the_view(app, client):
    _seed(BROWSE_PAGE_SIZE + 1, "keep")
    body = client.get("/browse?sort=oldest&tag=Dinner&junk=1").get_data(as_text=True)
    assert 'href="/browse?sort=oldest&amp;tag=dinner&amp;page=2"' in body
    assert "junk" not in body

    page_two = client.get("/browse?sort=oldest&tag=dinner&page=2")
    assert page_two.status_code == 200
    body = page_two.get_data(as_text=True)
    assert CARD.findall(body) == [f"keep-{BROWSE_PAGE_SIZE}"]
    # Page 1 of a view is the view without ?page, never the unfiltered /browse.
    assert 'href="/browse?sort=oldest&amp;tag=dinner"' in body


def test_filtered_page_one_redirect_keeps_the_view(app, client):
    _seed(1, "redirview")
    resp = client.get("/browse?sort=name&tag=dinner&page=1&next=https://evil.example")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "http://localhost/browse?sort=name&tag=dinner"


def test_browse_tag_is_escaped_and_bounded(app, client):
    _seed(1, "safe")
    body = client.get("/browse", query_string={"tag": '"><script>x</script>'}).get_data(
        as_text=True
    )
    assert "<script>x</script>" not in body
    assert client.get("/browse", query_string={"tag": "a" * 500}).status_code == 200


# ── tag hubs ─────────────────────────────────────────────────────────────────


def test_hub_page_boundaries(app, client):
    total = 2 * HUB_PAGE_SIZE + 1
    _seed(total, "stew")
    newest = _newest_first(total, "stew")

    first = client.get("/browse/tag/dinner")
    assert first.status_code == 200
    body = first.get_data(as_text=True)
    assert '<link rel="canonical" href="http://localhost/browse/tag/dinner">' in body
    assert "<title>Vegan Dinner Recipes · TastesLikeGood</title>" in body
    assert CARD.findall(body) == newest[:HUB_PAGE_SIZE]
    assert _item_list_slugs(body) == newest[:HUB_PAGE_SIZE]
    nav = _pagination_nav(body)
    assert '<a href="/browse/tag/dinner?page=2" aria-label="Page 2">2</a>' in nav

    middle = client.get("/browse/tag/dinner?page=2")
    assert middle.status_code == 200
    body = middle.get_data(as_text=True)
    canonical = "http://localhost/browse/tag/dinner?page=2"
    assert f'<link rel="canonical" href="{canonical}">' in body
    assert "<title>Vegan Dinner Recipes, Page 2 of 3 · TastesLikeGood</title>" in body
    assert _item_list_slugs(body) == newest[HUB_PAGE_SIZE : 2 * HUB_PAGE_SIZE]
    assert _json_ld(body, "CollectionPage")["url"] == canonical
    crumbs = _json_ld(body, "BreadcrumbList")["itemListElement"]
    assert crumbs[-1]["name"] == "Vegan Dinner Recipes"
    assert crumbs[-1]["item"] == canonical
    nav = _pagination_nav(body)
    assert '<a href="/browse/tag/dinner" class="public-page-step" rel="prev">' in nav
    assert '<a href="/browse/tag/dinner" aria-label="Page 1">1</a>' in nav
    assert "page=1" not in nav

    # The last page holds one recipe: a short last page, not a thin hub.
    last = client.get("/browse/tag/dinner?page=3")
    assert last.status_code == 200
    body = last.get_data(as_text=True)
    assert CARD.findall(body) == newest[2 * HUB_PAGE_SIZE :]
    assert _item_list_slugs(body) == newest[2 * HUB_PAGE_SIZE :]
    assert '<meta name="robots" content="index,follow">' in body
    assert "X-Robots-Tag" not in last.headers
    assert 'rel="next"' not in _pagination_nav(body)

    assert client.get("/browse/tag/dinner?page=4").status_code == 404


def test_empty_hub_renders_page_one_only(app, client):
    _seed(3, "only-dinner")  # members of other hubs, none of dessert
    resp = client.get("/browse/tag/dessert")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No recipes here yet." in body
    assert "public-browse-pagination" not in body
    assert resp.headers["X-Robots-Tag"] == "noindex, follow"
    assert client.get("/browse/tag/dessert?page=2").status_code == 404


def test_hub_page_one_redirects_to_bare_url(app, client):
    _seed(3, "hub-redir")
    resp = client.get("/browse/tag/dinner?page=1&utm_medium=social&save=x")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "http://localhost/browse/tag/dinner?utm_medium=social"


@pytest.mark.parametrize("raw", MALFORMED_PAGES)
def test_hub_malformed_page_is_404(app, client, raw):
    _seed(3, "hub-bad")
    assert client.get("/browse/tag/dinner", query_string={"page": raw}).status_code == 404


def test_unknown_hub_with_page_is_404(app, client):
    assert client.get("/browse/tag/not-a-hub?page=1").status_code == 404
    assert client.get("/browse/tag/not-a-hub?page=2").status_code == 404


def test_hub_trailing_slash_redirect_keeps_the_page(app, client):
    _seed(HUB_PAGE_SIZE + 1, "slash")
    resp = client.get("/browse/tag/dinner/?page=2&utm_source=x")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "http://localhost/browse/tag/dinner?page=2&utm_source=x"

    resp = client.get("/browse/tag/dinner/?page=1")
    assert resp.status_code == 301
    assert resp.headers["Location"] == "http://localhost/browse/tag/dinner"

    assert client.get("/browse/tag/dinner/?page=abc").status_code == 404
    assert client.get("/browse/tag/dinner/?page=2&page=3").status_code == 404


def test_repeated_page_key_is_404(app, client):
    """``?page=2&page=3`` is not a canonical spelling of any page."""
    _seed(2 * BROWSE_PAGE_SIZE + 1, "dup")
    assert client.get("/browse?page=2&page=3").status_code == 404
    assert client.get("/browse?page=2&page=2").status_code == 404
    assert client.get("/browse/tag/dinner?page=2&page=3").status_code == 404


def test_hub_later_page_emptied_by_hydration_is_noindex(app, client, monkeypatch):
    """A one-recipe last page unpublished mid-request renders nothing: noindex it."""
    _seed(HUB_PAGE_SIZE + 1, "race")
    # The oldest member is the only card on page 2.
    catalog_snapshot = public_module._catalog_tag_rows()
    oldest = Recipe.query.filter_by(slug="race-0").one()
    oldest.is_public = False
    db.session.commit()
    monkeypatch.setattr(public_module, "_catalog_tag_rows", lambda: catalog_snapshot)

    response = client.get("/browse/tag/dinner?page=2")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert CARD.findall(body) == []
    assert '<meta name="robots" content="noindex,follow">' in body
    assert response.headers["X-Robots-Tag"] == "noindex, follow"
