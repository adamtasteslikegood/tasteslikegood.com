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
    """Only ``page`` is validated; S9's ?sort&tag must still render."""
    _seed(2, "other")
    resp = client.get("/browse?sort=newest&tag=dinner")
    assert resp.status_code == 200
    assert '<link rel="canonical" href="http://localhost/browse">' in resp.get_data(as_text=True)


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
