"""Navigation aids on the public SSR pages.

- KAN-342: a public recipe links the Previous and Next recipe in the default
  /browse order (newest first).
- KAN-343: /browse and the tag hubs repeat the page numbers as a compact text
  nav in the header.
"""

import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from blueprints.public_bp import (  # noqa: E402
    BROWSE_PAGE_SIZE,
    HUB_PAGE_SIZE,
    _adjacent_recipes,
)
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402

BASE = datetime(2026, 9, 1, 12, 0, 0)
STEP_NAV = re.compile(r'<nav class="public-recipe-neighbors"[^>]*>(.*?)</nav>', re.S)
HEADER = re.compile(r'<header class="public-browse-header">(.*?)</header>', re.S)
COMPACT_NAV = re.compile(r'<nav class="public-page-compact"[^>]*>(.*?)</nav>', re.S)


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


def _add(slug, age, *, public=True, name=None):
    """A recipe created ``age`` minutes after BASE: a larger age is newer."""
    db.session.add(
        Recipe(
            id=str(uuid.uuid4()),
            name=name or slug.replace("-", " ").title(),
            slug=slug,
            is_public=public,
            created_at=BASE + timedelta(minutes=age),
            data={"name": slug, "description": "d", "tags": ["dinner"]},
        )
    )


def _step_navs(client, slug):
    body = client.get(f"/r/{slug}").get_data(as_text=True)
    return STEP_NAV.findall(body)


# ── KAN-342: previous / next recipe ──────────────────────────────────────────


def test_middle_recipe_links_both_neighbours_in_browse_order(app, client):
    _add("oldest", 0)
    _add("middle", 1, name="Middle <Stew>")
    _add("newest", 2)
    db.session.commit()

    navs = _step_navs(client, "middle")
    assert len(navs) == 1, "expected one nav, under the recipe"
    for nav in navs:
        # Newest first, like /browse: the newer recipe is Previous.
        assert 'href="/r/newest"' in nav and 'href="/r/oldest"' in nav
        assert nav.index("/r/newest") < nav.index("/r/oldest")
        assert "Previous" in nav and "Next" in nav
        assert "Newest" in nav and "Oldest" in nav

    # Neighbour names are escaped where they are shown.
    newest = "".join(_step_navs(client, "newest"))
    assert "Middle &lt;Stew&gt;" in newest
    assert "<Stew>" not in newest


def test_first_and_last_recipe_omit_the_missing_side(app, client):
    _add("oldest", 0)
    _add("newest", 1)
    db.session.commit()

    assert len(_step_navs(client, "newest")) == 1
    assert len(_step_navs(client, "oldest")) == 1
    for nav in _step_navs(client, "newest"):
        assert "Previous" not in nav
        assert 'href="/r/oldest"' in nav and "Next" in nav
    for nav in _step_navs(client, "oldest"):
        assert "Next" not in nav
        assert 'href="/r/newest"' in nav and "Previous" in nav


def test_unpublished_and_slugless_rows_are_skipped(app, client):
    _add("oldest", 0)
    _add("private", 1, public=False)
    _add("middle", 2)
    db.session.add(
        Recipe(
            id=str(uuid.uuid4()),
            name="No slug",
            slug=None,
            is_public=True,
            created_at=BASE + timedelta(minutes=3),
            data={"name": "No slug"},
        )
    )
    _add("newest", 4)
    db.session.commit()

    assert len(_step_navs(client, "middle")) == 1
    for nav in _step_navs(client, "middle"):
        assert 'href="/r/newest"' in nav and 'href="/r/oldest"' in nav
        assert "private" not in nav and "No slug" not in nav


def test_a_lone_recipe_has_no_step_nav(app, client):
    _add("only", 0)
    db.session.commit()
    assert _step_navs(client, "only") == []


def test_step_links_are_not_declared_as_pagination_in_the_head(app, client):
    _add("oldest", 0)
    _add("middle", 1)
    _add("newest", 2)
    db.session.commit()
    body = client.get("/r/middle").get_data(as_text=True)
    assert '<link rel="prev"' not in body and '<link rel="next"' not in body


def test_neighbour_nav_does_not_share_a_class_with_the_method_steps(app, client):
    """The method ``<li>`` rows are ``public-recipe-step``; sharing it corrupts both layouts."""
    db.session.add(
        Recipe(
            id=str(uuid.uuid4()),
            name="With steps",
            slug="with-steps",
            is_public=True,
            created_at=BASE + timedelta(minutes=1),
            data={"name": "With steps", "instructions": ["Chop.", "Cook."]},
        )
    )
    _add("oldest", 0)
    db.session.commit()

    body = client.get("/r/with-steps").get_data(as_text=True)
    assert '<li class="public-recipe-step">' in body
    assert '<nav class="public-recipe-step"' not in body
    css = (Path(__file__).resolve().parent.parent / "static/css/recipe-site.css").read_text()
    assert ".public-recipe-neighbors {" in css
    assert css.count(".public-recipe-step {") == 1, "only the method-step rule may use this name"


def test_one_neighbour_row_sits_under_the_recipe_with_and_without_a_hero_image(
    app, client, monkeypatch
):
    """Adam's staging check (2026-10-10): the row under the title unbalanced the
    page. One row, after the recipe's tags and before the related recipes."""
    for index, slug in enumerate(("older", "middle", "newer")):
        _add(slug, index)
    db.session.commit()

    def check():
        body = client.get("/r/middle").get_data(as_text=True)
        assert len(STEP_NAV.findall(body)) == 1
        nav = body.index('<nav class="public-recipe-neighbors"')
        assert body.index('<section class="public-recipe-tags">') < nav
        assert nav < body.index('<section class="public-related"')

    check()
    monkeypatch.setattr(
        "blueprints.public_bp._rendered_image",
        lambda recipe: ("https://example.test/hero.jpg", None),
    )
    check()


def test_neighbour_buttons_name_the_recipe_they_lead_to(app, client):
    for index, slug in enumerate(("older", "middle", "newer")):
        _add(slug, index)
    db.session.commit()
    (nav,) = _step_navs(client, "middle")
    assert 'aria-label="Previous recipe: ' in nav and 'aria-label="Next recipe: ' in nav
    assert "← Previous" in nav and "Next →" in nav


def test_no_nav_when_neither_neighbour_can_be_loaded(app):
    """A neighbour unpublished between the catalog scan and the lookup leaves nothing to link."""
    _add("middle", 1)
    db.session.commit()
    middle = Recipe.query.filter_by(slug="middle").one()
    ghost = SimpleNamespace(id="gone", created_at=BASE, updated_at=BASE, tags=[])
    here = SimpleNamespace(id=middle.id, created_at=middle.created_at, updated_at=None, tags=[])
    with app.test_request_context("/r/middle"):
        assert _adjacent_recipes(middle, [here, ghost]) is None


# ── KAN-343: compact page numbers in the header ──────────────────────────────


def _compact(client, url):
    body = client.get(url).get_data(as_text=True)
    header = HEADER.search(body)
    assert header, "no browse header"
    return COMPACT_NAV.findall(header.group(1)), body


def test_browse_header_repeats_the_page_numbers(app, client):
    for index in range(2 * BROWSE_PAGE_SIZE + 1):
        _add(f"soup-{index}", index)
    db.session.commit()

    navs, body = _compact(client, "/browse?page=2")
    assert len(navs) == 1
    nav = navs[0]
    # Same URLs as the main nav: page 1 is the bare listing, never ?page=1.
    assert '<a href="/browse" aria-label="Page 1">1</a>' in nav
    assert '<a href="/browse?page=3" aria-label="Page 3">3</a>' in nav
    assert '<span aria-current="page">2</span>' in nav
    assert "?page=1" not in nav
    # Two navs on the page must not share a landmark name.
    assert body.count('aria-label="Pagination"') == 1


def test_tag_hub_header_repeats_the_page_numbers(app, client):
    for index in range(HUB_PAGE_SIZE + 1):
        _add(f"stew-{index}", index)
    db.session.commit()

    navs, _ = _compact(client, "/browse/tag/dinner")
    assert len(navs) == 1
    assert '<span aria-current="page">1</span>' in navs[0]
    assert '<a href="/browse/tag/dinner?page=2" aria-label="Page 2">2</a>' in navs[0]


def test_single_page_listing_has_no_compact_nav(app, client):
    _add("only", 0)
    db.session.commit()
    assert _compact(client, "/browse")[0] == []
    assert _compact(client, "/browse/tag/dinner")[0] == []
