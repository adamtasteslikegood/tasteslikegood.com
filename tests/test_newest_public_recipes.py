"""KAN-348: the home page's newest-recipes list.

Express reads ``/api/public/newest-recipes`` and writes the links into the home
shell, so the order and the visibility filter are decided here.
"""

import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402

BASE = datetime(2026, 9, 1, 12, 0, 0)
URL = "/api/public/newest-recipes"


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


def _add(slug, *, created, published, public=True, name=None):
    """``created`` and ``published`` are minutes after BASE."""
    recipe = Recipe(
        id=str(uuid.uuid4()),
        name=name or slug.replace("-", " ").title(),
        slug=slug,
        is_public=public,
        created_at=BASE + timedelta(minutes=created),
        data={"name": slug},
    )
    db.session.add(recipe)
    db.session.flush()
    # Set after the flush: the publish listener stamps "now" on a public row.
    recipe.first_published_at = BASE + timedelta(minutes=published)
    return recipe


def _slugs(client, query=""):
    return [row["slug"] for row in client.get(URL + query).get_json()["recipes"]]


def test_orders_by_first_published_at_not_created_at(app, client):
    _add("created-last-published-first", created=30, published=1)
    _add("created-first-published-last", created=0, published=20)
    _add("middle", created=10, published=10)
    db.session.commit()

    assert _slugs(client) == [
        "created-first-published-last",
        "middle",
        "created-last-published-first",
    ]


def test_payload_carries_name_and_publication_date(app, client):
    _add("stew", created=0, published=5, name="Hearty <Stew>")
    db.session.commit()

    (row,) = client.get(URL).get_json()["recipes"]
    assert row == {
        "slug": "stew",
        "name": "Hearty <Stew>",
        "first_published_at": (BASE + timedelta(minutes=5)).isoformat(),
    }


def test_unpublished_slugless_and_undated_rows_are_never_listed(app, client):
    _add("public", created=0, published=1)
    _add("unpublished-again", created=1, published=2, public=False)
    undated = _add("no-date", created=2, published=5)
    slugless = _add("temp", created=3, published=3)
    slugless.slug = None
    db.session.commit()
    # A legacy public row the migration left undated. The publish listener
    # stamps any public row it flushes, so write the NULL underneath it.
    db.session.execute(
        update(Recipe).where(Recipe.id == undated.id).values(first_published_at=None)
    )
    db.session.commit()

    assert _slugs(client) == ["public"]

    # Unpublishing takes effect on the next read.
    Recipe.query.filter_by(slug="public").one().is_public = False
    db.session.commit()
    assert _slugs(client) == []


def test_limit_defaults_to_eight_and_is_clamped(app, client):
    for index in range(15):
        _add(f"soup-{index}", created=index, published=index)
    db.session.commit()

    assert len(_slugs(client)) == 8
    assert _slugs(client)[0] == "soup-14"
    assert len(_slugs(client, "?limit=6")) == 6
    assert len(_slugs(client, "?limit=500")) == 12
    assert len(_slugs(client, "?limit=0")) == 1
    assert len(_slugs(client, "?limit=abc")) == 8


def test_same_publication_instant_breaks_the_tie_by_id(app, client):
    first = _add("tie-a", created=0, published=4)
    second = _add("tie-b", created=1, published=4)
    db.session.commit()

    expected = [r.slug for r in sorted((first, second), key=lambda r: r.id, reverse=True)]
    assert _slugs(client) == expected
