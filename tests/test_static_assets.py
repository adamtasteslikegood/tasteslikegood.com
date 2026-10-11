"""KAN-362: versioned static URLs, and first-row thumbnails that load eagerly."""

import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from utils.static_assets import IMMUTABLE_CACHE_CONTROL, static_version  # noqa: E402

BASE = datetime(2026, 9, 1, 12, 0, 0)
IMG = re.compile(r"<img\b[^>]*>", re.S)


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


def _add(slug, age):
    db.session.add(
        Recipe(
            id=str(uuid.uuid4()),
            name=slug,
            slug=slug,
            is_public=True,
            created_at=BASE + timedelta(minutes=age),
            data={"name": slug, "description": "d", "tags": ["dinner"]},
        )
    )


# ── static versioning ────────────────────────────────────────────────────────


def test_public_pages_link_their_static_files_with_the_content_hash(app, client):
    _add("stew", 0)
    db.session.commit()
    body = client.get("/browse").get_data(as_text=True)

    for filename in ("css/tokens.css", "css/recipe-site.css", "js/public.js"):
        version = static_version(app, filename)
        assert version and re.fullmatch(r"[0-9a-f]{12}", version)
        assert f"/static/{filename}?v={version}" in body


def test_current_version_is_immutable_and_anything_else_revalidates(app, client):
    version = static_version(app, "css/recipe-site.css")

    current = client.get(f"/static/css/recipe-site.css?v={version}")
    assert current.status_code == 200
    assert current.headers["Cache-Control"] == IMMUTABLE_CACHE_CONTROL

    for url in ("/static/css/recipe-site.css", "/static/css/recipe-site.css?v=000000000000"):
        resp = client.get(url)
        assert resp.status_code == 200
        assert "immutable" not in resp.headers.get("Cache-Control", "")

    # A revalidation keeps the year: a 304's headers replace the stored ones.
    revalidated = client.get(
        f"/static/css/recipe-site.css?v={version}",
        headers={"If-None-Match": current.headers["ETag"]},
    )
    assert revalidated.status_code == 304
    assert revalidated.headers["Cache-Control"] == IMMUTABLE_CACHE_CONTROL

    missing = client.get(f"/static/css/nope.css?v={version}")
    assert missing.status_code == 404
    assert "immutable" not in missing.headers.get("Cache-Control", "")


def test_version_follows_the_file_content(app, tmp_path, monkeypatch):
    (tmp_path / "a.css").write_text("body{color:red}")
    monkeypatch.setattr(app, "static_folder", str(tmp_path))
    first = static_version(app, "a.css")
    (tmp_path / "a.css").write_text("body{color:blue;margin:0}")
    assert static_version(app, "a.css") != first

    assert static_version(app, "missing.css") is None
    assert static_version(app, "../app.py") is None
    assert static_version(app, ".") is None


# ── first-row thumbnails ─────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["/browse", "/browse/tag/dinner"])
def test_first_row_of_cards_loads_eagerly_and_the_rest_lazily(app, client, monkeypatch, url):
    monkeypatch.setattr(
        "blueprints.public_bp._card_image",
        lambda recipe: {"src": f"https://example.test/{recipe.slug}.webp"},
    )
    for index in range(6):
        _add(f"stew-{index}", index)
    db.session.commit()

    images = [tag for tag in IMG.findall(client.get(url).get_data(as_text=True))]
    assert len(images) == 6
    assert 'fetchpriority="high"' in images[0]
    for tag in images[:3]:
        assert "loading=" not in tag
    for tag in images[1:]:
        assert "fetchpriority" not in tag
    for tag in images[3:]:
        assert 'loading="lazy"' in tag


@pytest.mark.parametrize("url", ["/browse", "/browse/tag/dinner"])
def test_priority_goes_to_the_first_image_when_the_first_card_has_none(
    app, client, monkeypatch, url
):
    for index in range(5):
        _add(f"stew-{index}", index)
    db.session.commit()
    # Newest first, so stew-4 is the first card; it has no image.
    monkeypatch.setattr(
        "blueprints.public_bp._card_image",
        lambda recipe: (
            None if recipe.slug == "stew-4" else {"src": f"https://example.test/{recipe.slug}.webp"}
        ),
    )

    images = IMG.findall(client.get(url).get_data(as_text=True))
    assert len(images) == 4
    assert 'fetchpriority="high"' in images[0] and "stew-3" in images[0]
    assert sum("fetchpriority" in tag for tag in images) == 1
    assert "loading=" not in images[1]
    for tag in images[2:]:
        assert 'loading="lazy"' in tag
