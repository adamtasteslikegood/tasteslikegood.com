"""Tests for Markdown for agents on the public pages (KAN-397).

Covers:
- ``Accept: text/markdown`` on /r/<slug> and /browse returns Markdown
- the same URLs still return HTML to a browser
- both representations say ``Vary: Accept``
- a 404 and a redirect are passed on as they are
- other paths are not touched, and the switch turns it all off
"""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from utils.markdown_for_agents import _is_public_page, _with_vary_accept  # noqa: E402

MARKDOWN = {"Accept": "text/markdown"}


def _build_app(monkeypatch, **overrides):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    return create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        WTF_CSRF_ENABLED=False,
        **overrides,
    )


@pytest.fixture
def app(monkeypatch):
    app = _build_app(monkeypatch)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _add_recipe(app, name="Thai Peanut Noodles", slug="thai-peanut-noodles"):
    with app.app_context():
        db.session.add(
            Recipe(
                id=str(uuid.uuid4()),
                user_id=None,
                name=name,
                slug=slug,
                is_public=True,
                data={
                    "name": name,
                    "description": f"{name} description",
                    "ingredients": {
                        "Main": [{"name": "rice noodles", "amount": [200], "units": "g"}]
                    },
                    "instructions": ["Boil the noodles."],
                },
            )
        )
        db.session.commit()


def _vary(response):
    return [
        token.strip().lower()
        for value in response.headers.getlist("Vary")
        for token in value.split(",")
    ]


def test_recipe_page_is_markdown_when_asked(app, client):
    _add_recipe(app)

    response = client.get("/r/thai-peanut-noodles", headers=MARKDOWN)

    assert response.status_code == 200
    assert response.mimetype == "text/markdown"
    body = response.get_data(as_text=True)
    assert "# Thai Peanut Noodles" in body
    assert "rice noodles" in body
    assert "Boil the noodles." in body
    assert "<html" not in body
    assert "accept" in _vary(response)
    assert response.content_length == len(response.get_data())


def test_markdown_is_the_page_content_without_the_page_furniture(app, client):
    _add_recipe(app)

    recipe = client.get("/r/thai-peanut-noodles", headers=MARKDOWN).get_data(as_text=True)
    browse = client.get("/browse", headers=MARKDOWN).get_data(as_text=True)

    # One title, the steps numbered once, no breadcrumb, header or footer.
    assert recipe.count("# Thai Peanut Noodles") == 1
    assert "1. Boil the noodles." in recipe
    assert "1. 1." not in recipe
    assert "[Home]" not in recipe
    assert "Privacy" not in recipe
    # The browse page keeps its recipe links but not its form or label.
    assert "(/r/thai-peanut-noodles)" in browse
    assert "Newest first" not in browse
    assert "Anti-recipe browse" not in browse


def test_recipe_page_is_still_html_for_a_browser(app, client):
    _add_recipe(app)

    response = client.get("/r/thai-peanut-noodles", headers={"Accept": "text/html"})

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "<html" in response.get_data(as_text=True)
    assert "accept" in _vary(response)


def test_browse_is_markdown_when_asked(app, client):
    _add_recipe(app)

    response = client.get("/browse", headers=MARKDOWN)

    assert response.status_code == 200
    assert response.mimetype == "text/markdown"
    assert "(/r/thai-peanut-noodles)" in response.get_data(as_text=True)
    assert "accept" in _vary(response)


def test_markdown_follows_the_page_with_no_cache(app, client):
    _add_recipe(app)
    assert client.get("/r/thai-peanut-noodles", headers=MARKDOWN).status_code == 200

    with app.app_context():
        Recipe.query.filter_by(slug="thai-peanut-noodles").update({"is_public": False})
        db.session.commit()

    assert client.get("/r/thai-peanut-noodles", headers=MARKDOWN).status_code == 404


def test_missing_recipe_stays_a_404(client):
    response = client.get("/r/no-such-recipe", headers=MARKDOWN)

    assert response.status_code == 404
    assert response.mimetype != "text/markdown"
    assert "accept" in _vary(response)


def test_trailing_slash_redirect_is_passed_on(client):
    response = client.get("/r/thai-peanut-noodles/", headers=MARKDOWN)

    assert response.status_code == 301
    assert response.headers["Location"].endswith("/r/thai-peanut-noodles")


def test_other_paths_are_not_touched(app, client):
    _add_recipe(app)

    response = client.get("/api/recipes/public/thai-peanut-noodles", headers=MARKDOWN)

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert "accept" not in _vary(response)


def test_post_is_not_converted(client):
    response = client.post("/browse", headers=MARKDOWN)

    assert response.status_code == 405
    assert response.mimetype != "text/markdown"


def test_switch_off_serves_html_only(monkeypatch):
    app = _build_app(monkeypatch, MARKDOWN_FOR_AGENTS=False)
    with app.app_context():
        db.create_all()
        _add_recipe(app)

        response = app.test_client().get("/r/thai-peanut-noodles", headers=MARKDOWN)

        assert response.status_code == 200
        assert response.mimetype == "text/html"
        assert "accept" not in _vary(response)
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/r/some-recipe", True),
        ("/browse", True),
        ("/browse/tag/dinner", True),
        ("/", False),
        ("/browsers", False),
        ("/recipe/file.json", False),
        ("/api/recipes/public/some-recipe", False),
        ("/sitemap.xml", False),
    ],
)
def test_public_page_paths(path, expected):
    assert _is_public_page(path) is expected


@pytest.mark.parametrize(
    "headers, expected",
    [
        ([], [("Vary", "Accept")]),
        ([("Vary", "Cookie")], [("Vary", "Cookie"), ("Vary", "Accept")]),
        ([("Vary", "Cookie, Accept")], [("Vary", "Cookie, Accept")]),
        ([("vary", "*")], [("vary", "*")]),
    ],
)
def test_vary_accept_is_added_once(headers, expected):
    assert _with_vary_accept(headers) == expected
