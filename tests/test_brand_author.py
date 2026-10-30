"""The site's own author name on public recipes (KAN-434).

Recipes owned by user id 1 show "Tasteslikegood - classic" in the /r/<slug>
and /browse bylines and in Recipe JSON-LD ``author.name``; other users keep
their own name; a Google sign-in that rewrites ``user.name`` does not revert it.
"""

import html
import json
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402
from services.oauth_flow import upsert_authenticated_user  # noqa: E402
from utils.brand_author import (  # noqa: E402
    author_display_name,
    brand_author_name,
    is_brand_author,
)

BRAND = "Tasteslikegood - classic"


@pytest.fixture(autouse=True)
def _default_brand_env(monkeypatch):
    monkeypatch.delenv("BRAND_AUTHOR_USER_IDS", raising=False)
    monkeypatch.delenv("BRAND_AUTHOR_SUFFIX", raising=False)


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


def _seed(app):
    """Adam (id 1, Google name) and another cook, one public recipe each."""
    with app.app_context():
        adam = User(id=1, email="adam@example.com", name="Adam Google", google_id="g-adam")
        cook = User(id=2, email="cook@example.com", name="Sam Cook", google_id="g-sam")
        db.session.add_all([adam, cook])
        db.session.flush()
        for owner, name, slug in [
            (adam, "Brand Stew", "brand-stew"),
            (cook, "Community Curry", "community-curry"),
        ]:
            db.session.add(
                Recipe(
                    id=str(uuid.uuid4()),
                    user_id=owner.id,
                    name=name,
                    slug=slug,
                    is_public=True,
                    data={"name": name, "description": f"{name} description"},
                )
            )
        db.session.commit()


def _recipe_ld(body: str) -> dict:
    blocks: list[dict] = [
        json.loads(html.unescape(block))
        for block in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', body, re.DOTALL
        )
    ]
    (found,) = [block for block in blocks if block.get("@type") == "Recipe"]
    assert isinstance(found, dict)
    return found


# ── the helper ───────────────────────────────────────────────────────────────


def test_brand_author_defaults_to_user_one_and_classic():
    assert is_brand_author(1)
    assert not is_brand_author(2)
    assert not is_brand_author(None)
    assert brand_author_name() == BRAND


def test_author_display_name_uses_brand_for_user_one_even_without_a_name():
    assert author_display_name(SimpleNamespace(id=1, name="Adam Google")) == BRAND
    assert author_display_name(SimpleNamespace(id=1, name=None)) == BRAND


def test_author_display_name_keeps_other_users_names():
    assert author_display_name(SimpleNamespace(id=2, name="Sam Cook")) == "Sam Cook"
    assert author_display_name(SimpleNamespace(id=2, name="")) is None
    assert author_display_name(None) is None


def test_env_overrides_ids_and_suffix(monkeypatch):
    monkeypatch.setenv("BRAND_AUTHOR_USER_IDS", "7, 9")
    monkeypatch.setenv("BRAND_AUTHOR_SUFFIX", "admins")

    assert not is_brand_author(1)
    assert is_brand_author(7) and is_brand_author(9)
    assert author_display_name(SimpleNamespace(id=9, name="X")) == "Tasteslikegood - admins"


def test_empty_suffix_is_plain_brand_name(monkeypatch):
    monkeypatch.setenv("BRAND_AUTHOR_SUFFIX", "")

    assert brand_author_name() == "Tasteslikegood"


# ── the public pages ─────────────────────────────────────────────────────────


def test_recipe_page_byline_and_json_ld_use_brand_for_user_one(app, client):
    _seed(app)

    body = client.get("/r/brand-stew").get_data(as_text=True)

    assert f"<li>By {BRAND}</li>" in body
    assert "Adam Google" not in body
    assert _recipe_ld(body)["author"] == {"@type": "Person", "name": BRAND}


def test_recipe_page_keeps_other_users_name(app, client):
    _seed(app)

    body = client.get("/r/community-curry").get_data(as_text=True)

    assert "<li>By Sam Cook</li>" in body
    assert BRAND not in body
    assert _recipe_ld(body)["author"] == {"@type": "Person", "name": "Sam Cook"}


def test_browse_bylines(app, client):
    _seed(app)

    body = client.get("/browse").get_data(as_text=True)

    assert f"<li>By {BRAND}</li>" in body
    assert "<li>By Sam Cook</li>" in body
    assert "Adam Google" not in body


def test_google_sign_in_does_not_revert_the_brand_name(app, client):
    _seed(app)
    with app.app_context():
        user, error = upsert_authenticated_user(
            {"id": "g-adam", "email": "adam@example.com", "name": "Adam Renamed"}
        )
        assert error is None
        db.session.commit()
        assert db.session.get(User, 1).name == "Adam Renamed"

    body = client.get("/r/brand-stew").get_data(as_text=True)

    assert f"<li>By {BRAND}</li>" in body
    assert "Adam Renamed" not in body
    assert _recipe_ld(body)["author"]["name"] == BRAND
