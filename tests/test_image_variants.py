"""Sized WebP image variants (KAN-271).

Covers:
- the width allow-list and the resize/encode helper
- ``GET /api/recipes/<id>/image?w=<n>``: WebP at the right size, same access
  check as the original, 400 on an unlisted width, fallback on bad bytes
- public image responses retain a bounded cache lifetime even with ``?v=``
- variants are built from storage, never from the (possibly stale) Valkey
  entry for the original
- the recipe hero and browse cards emit srcset/sizes/dimensions and the hero
  is not lazy-loaded
"""

import base64
import io
import re
import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from blueprints.public_bp import _image_version_token  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from services.image_variants import (  # noqa: E402
    MAX_SOURCE_PIXELS,
    PIN_SIZE,
    make_pin_variant,
    make_webp_variant,
    parse_pin_flag,
    parse_variant_width,
)


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


def _jpeg(width=1408, height=768, color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="JPEG", quality=90)
    return out.getvalue()


def _add_image_recipe(slug, image_bytes, *, public=True, timestamp="2026-09-01T10:00:00"):
    recipe = Recipe(
        id=str(uuid.uuid4()),
        name=f"Photo {slug}",
        slug=slug,
        is_public=public,
        data={
            "name": f"Photo {slug}",
            "description": "Has a photo.",
            "ai_image_data": base64.b64encode(image_bytes).decode("ascii"),
            "ai_metadata": {"image_generation": {"success": True, "timestamp": timestamp}},
        },
    )
    db.session.add(recipe)
    db.session.commit()
    return recipe.id


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [(None, None), ("400", 400), ("1200", 1200)])
def test_parse_variant_width_accepts_allow_listed_widths(raw, expected):
    assert parse_variant_width(raw) == expected


@pytest.mark.parametrize("raw", ["500", "0", "-400", "abc", "", "400.0", "99999"])
def test_parse_variant_width_rejects_everything_else(raw):
    with pytest.raises(ValueError):
        parse_variant_width(raw)


def test_make_webp_variant_downscales_and_keeps_aspect_ratio():
    source = _jpeg()
    variant = make_webp_variant(source, 400)
    assert variant is not None
    with Image.open(io.BytesIO(variant)) as image:
        assert image.format == "WEBP"
        assert image.size == (400, 218)
    assert len(variant) < len(source)


def test_make_webp_variant_guarantees_advertised_width_for_narrow_sources():
    variant = make_webp_variant(_jpeg(300, 200), 1200)
    assert variant is not None
    with Image.open(io.BytesIO(variant)) as image:
        assert image.size == (1200, 800)


def test_make_webp_variant_rejects_oversized_source_before_decode():
    source = mock.MagicMock()
    source.width = 5001
    source.height = 5000
    source.__enter__.return_value = source
    assert source.width * source.height > MAX_SOURCE_PIXELS

    with (
        mock.patch("services.image_variants.Image.open", return_value=source),
        mock.patch("services.image_variants.ImageOps.exif_transpose") as transpose,
    ):
        assert make_webp_variant(b"compact crafted image", 400) is None
    transpose.assert_not_called()


def test_make_webp_variant_rejects_oversized_output_before_resize():
    source = mock.MagicMock()
    source.width = 1
    source.height = 30_000
    source.mode = "RGB"
    source.__enter__.return_value = source

    with (
        mock.patch("services.image_variants.Image.open", return_value=source),
        mock.patch("services.image_variants.ImageOps.exif_transpose", return_value=source),
    ):
        assert make_webp_variant(b"compact extreme-aspect image", 1200) is None
    source.resize.assert_not_called()


def test_make_webp_variant_keeps_transparency():
    out = io.BytesIO()
    Image.new("RGBA", (800, 400), (0, 0, 0, 0)).save(out, format="PNG")
    variant = make_webp_variant(out.getvalue(), 400)
    assert variant is not None
    with Image.open(io.BytesIO(variant)) as image:
        assert image.mode == "RGBA"


def test_make_webp_variant_returns_none_for_undecodable_bytes():
    assert make_webp_variant(b"\x89PNG\r\n\x1a\nnot really a png", 400) is None


# ── the image route ──────────────────────────────────────────────────────────


def test_variant_is_webp_at_the_requested_width(app, client):
    with app.app_context():
        recipe_id = _add_image_recipe("variant-pie", _jpeg())

    resp = client.get(f"/api/recipes/{recipe_id}/image?w=800")
    assert resp.status_code == 200
    assert resp.mimetype == "image/webp"
    with Image.open(io.BytesIO(resp.data)) as image:
        assert image.width == 800
    assert resp.headers["Cache-Control"] == "public, max-age=86400"


def test_unlisted_width_is_rejected(app, client):
    with app.app_context():
        recipe_id = _add_image_recipe("odd-width-pie", _jpeg())

    resp = client.get(f"/api/recipes/{recipe_id}/image?w=640")
    assert resp.status_code == 400


def test_variant_of_a_private_recipe_still_requires_ownership(app, client):
    with app.app_context():
        recipe_id = _add_image_recipe("private-variant", _jpeg(), public=False)

    resp = client.get(f"/api/recipes/{recipe_id}/image?w=400")
    assert resp.status_code == 404


def test_undecodable_image_falls_back_to_the_original_bytes(app, client):
    broken = b"\x89PNG\r\n\x1a\nnot really a png"
    with app.app_context():
        recipe_id = _add_image_recipe("broken-pie", broken)

    resp = client.get(f"/api/recipes/{recipe_id}/image?w=400")
    assert resp.status_code == 200
    assert resp.data == broken
    assert resp.mimetype == "image/png"
    # Full-size bytes under a sized ?w= URL must not be pinned by any HTTP
    # cache: a later successful variant would otherwise never reach clients.
    assert resp.headers["Cache-Control"] == "no-store"
    # The plain URL for the same image keeps its normal public lifetime.
    plain = client.get(f"/api/recipes/{recipe_id}/image")
    assert plain.headers["Cache-Control"] == "public, max-age=86400"


@pytest.mark.parametrize(
    "query",
    ["w=400", "w=400&v=stale0000000", "w=400&v=__TOKEN__", "v=__TOKEN__"],
)
def test_public_image_cache_lifetime_stays_bounded(app, client, query):
    with app.app_context():
        recipe_id = _add_image_recipe("bounded-cache-pie", _jpeg())
        token = _image_version_token(db.session.get(Recipe, recipe_id))

    resp = client.get(f"/api/recipes/{recipe_id}/image?{query.replace('__TOKEN__', token)}")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=86400"


def test_failed_gcs_variant_read_does_not_cache_legacy_bytes_as_gcs(app, client, monkeypatch):
    legacy = _jpeg(color=(0, 0, 220))
    with app.app_context():
        recipe_id = _add_image_recipe("gcs-fallback-pie", legacy)
        recipe = db.session.get(Recipe, recipe_id)
        data = dict(recipe.data)
        data["ai_image_gcs"] = "gs://bucket/recipe/version-2.png"
        recipe.data = data
        db.session.commit()

    monkeypatch.setattr("blueprints.generation_api_bp.GCS_BUCKET_NAME", "bucket")
    download = mock.Mock(return_value=None)
    monkeypatch.setattr("services.gcs_service.download_image", download)

    resp = client.get(f"/api/recipes/{recipe_id}/image?w=400")
    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"
    assert resp.data == legacy
    # Full-size fallback bytes at a sized ?w= URL: no HTTP cache may pin them,
    # or a transient GCS failure outlives itself by a day in every CDN.
    assert resp.headers["Cache-Control"] == "no-store"
    download.assert_called()


def test_non_string_gcs_uri_keeps_legacy_variant_fallback(app, client, monkeypatch):
    legacy = _jpeg(color=(0, 0, 220))
    with app.app_context():
        recipe_id = _add_image_recipe("malformed-gcs-pie", legacy)
        recipe = db.session.get(Recipe, recipe_id)
        data = dict(recipe.data)
        data["ai_image_gcs"] = 12345
        recipe.data = data
        db.session.commit()

    monkeypatch.setattr("blueprints.generation_api_bp.GCS_BUCKET_NAME", "bucket")
    download = mock.Mock(return_value=None)
    monkeypatch.setattr("services.gcs_service.download_image", download)

    resp = client.get(f"/api/recipes/{recipe_id}/image?w=400")

    assert resp.status_code == 200
    assert resp.mimetype == "image/webp"
    with Image.open(io.BytesIO(resp.data)) as image:
        assert image.width == 400
    download.assert_not_called()


def test_private_variant_is_never_publicly_cacheable(app, client):
    from models.user import User

    with app.app_context():
        owner = User(email="variant-owner@example.com", name="Variant Owner")
        db.session.add(owner)
        db.session.commit()
        recipe_id = _add_image_recipe("owned-variant", _jpeg(), public=False)
        recipe = db.session.get(Recipe, recipe_id)
        recipe.user_id = owner.id
        db.session.commit()
        owner_id = owner.id
        token = _image_version_token(recipe)

    with client.session_transaction() as flask_session:
        flask_session["user_id"] = owner_id

    resp = client.get(f"/api/recipes/{recipe_id}/image?w=400&v={token}")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "private, no-store"


def test_variant_is_built_from_storage_not_the_cached_original(app, client, monkeypatch):
    """The worker commits a new image before it clears ``vgc:img:<id>``.

    In that window the Valkey entry for the original still holds the OLD
    photo while pages already link the new ``?v=``. A variant built from it
    would pin the old photo under an immutable URL for a year.
    """
    red, stale_blue = _jpeg(color=(220, 0, 0)), _jpeg(color=(0, 0, 220))
    with app.app_context():
        recipe_id = _add_image_recipe("race-pie", red)

    def fake_get(key):
        return stale_blue if key == f"vgc:img:{recipe_id}" else None

    monkeypatch.setattr("blueprints.generation_api_bp.safe_get", fake_get)

    resp = client.get(f"/api/recipes/{recipe_id}/image?w=400")
    with Image.open(io.BytesIO(resp.data)) as image:
        r, g, b = image.convert("RGB").getpixel((10, 10))
    assert r > 150 and b < 80, "variant was built from the stale cached original"


def test_regenerated_image_gets_new_variant_cache_keys(app, client, monkeypatch):
    stored: dict[str, bytes] = {}
    monkeypatch.setattr("blueprints.generation_api_bp.safe_get", stored.get)
    monkeypatch.setattr(
        "blueprints.generation_api_bp.safe_set",
        lambda key, value, timeout=None: stored.__setitem__(key, value),
    )
    with app.app_context():
        recipe_id = _add_image_recipe("regen-variant", _jpeg())

    client.get(f"/api/recipes/{recipe_id}/image?w=400")
    first_keys = {k for k in stored if ":w400:" in k}
    assert len(first_keys) == 1

    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        data = dict(recipe.data)
        # Reproduce a generic PUT changing the stored bytes while retaining the
        # old worker metadata timestamp. The immutable marker must still move.
        data["ai_image_data"] = base64.b64encode(_jpeg(color=(0, 0, 220))).decode("ascii")
        recipe.data = data
        db.session.commit()

    client.get(f"/api/recipes/{recipe_id}/image?w=400")
    assert len({k for k in stored if ":w400:" in k} - first_keys) == 1


# ── markup ───────────────────────────────────────────────────────────────────


def _hero_img(body: str) -> str:
    pattern = r'<div class="public-recipe-image-wrap">\s*(?:\{#.*?#\}\s*)?(<img.*?/>)'
    match = re.search(pattern, body, re.S)
    assert match, body
    return match.group(1)


def test_recipe_hero_is_sized_prioritized_and_not_lazy(app, client):
    with app.app_context():
        recipe_id = _add_image_recipe("hero-pie", _jpeg())
        token = _image_version_token(db.session.get(Recipe, recipe_id))

    body = client.get("/r/hero-pie").get_data(as_text=True)
    hero = _hero_img(body)
    assert 'loading="lazy"' not in hero
    assert 'fetchpriority="high"' in hero
    assert 'width="1408"' in hero and 'height="768"' in hero
    for width in (400, 800, 1200):
        assert f"/api/recipes/{recipe_id}/image?w={width}&amp;v={token} {width}w" in hero
    assert "sizes=" in hero
    assert re.search(r'<link rel="preload" as="image"[^>]*imagesrcset="[^"]*w=1200', body)
    # Social cards keep the full-size original: unfurlers want >= 1200 px wide.
    og_image = f"http://localhost/api/recipes/{recipe_id}/image?v={token}"
    assert f'<meta property="og:image" content="{og_image}">' in body


def test_stock_image_hero_has_no_srcset_or_preload(app, client):
    with app.app_context():
        recipe = Recipe(
            id=str(uuid.uuid4()),
            name="Stock Pie",
            slug="stock-pie",
            is_public=True,
            data={"name": "Stock Pie", "stock_image_url": "https://images.example.com/pie.jpg"},
        )
        db.session.add(recipe)
        db.session.commit()

    body = client.get("/r/stock-pie").get_data(as_text=True)
    hero = _hero_img(body)
    assert 'src="https://images.example.com/pie.jpg"' in hero
    assert "srcset" not in hero
    assert 'loading="lazy"' not in hero
    assert 'rel="preload"' not in body


def test_browse_cards_use_small_variants_and_stay_lazy(app, client):
    with app.app_context():
        recipe_id = _add_image_recipe("card-pie", _jpeg())
        token = _image_version_token(db.session.get(Recipe, recipe_id))

    body = client.get("/browse").get_data(as_text=True)
    card = re.search(r'<div class="public-browse-card-image">\s*(<img.*?/>)', body, re.S)
    assert card, body
    img = card.group(1)
    assert f'src="/api/recipes/{recipe_id}/image?w=400&amp;v={token}"' in img
    assert f"w=800&amp;v={token} 800w" in img
    assert 'loading="lazy"' in img
    assert 'width="400"' in img and 'height="300"' in img
    # KAN-297: cards are two per row at every width up to 900px, phones included.
    assert 'sizes="(max-width: 900px) 50vw, 400px"' in img


def test_browse_grid_never_collapses_to_one_column():
    # KAN-297: the phone breakpoint used to reset the card grid to ``1fr``.
    css = (Path(__file__).resolve().parents[1] / "static/css/recipe-site.css").read_text()
    rules = re.findall(r"\.public-browse-list\s*\{([^}]*)\}", css)
    columns = [m for rule in rules for m in re.findall(r"grid-template-columns:\s*([^;]+);", rule)]
    assert columns == ["repeat(3, minmax(0, 1fr))", "repeat(2, minmax(0, 1fr))"]


# ── Pinterest pin variant (KAN-284) ──────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [(None, False), ("1", True)])
def test_parse_pin_flag_accepts_absent_or_one(raw, expected):
    assert parse_pin_flag(raw) is expected


@pytest.mark.parametrize("raw", ["0", "2", "true", "", "yes"])
def test_parse_pin_flag_rejects_everything_else(raw):
    with pytest.raises(ValueError):
        parse_pin_flag(raw)


@pytest.mark.parametrize("size", [(1408, 768), (1024, 1024), (600, 1400), (300, 200)], ids=str)
def test_pin_variant_is_a_2_by_3_jpeg_for_any_source_shape(size):
    pin = make_pin_variant(_jpeg(*size))
    assert pin is not None
    with Image.open(io.BytesIO(pin)) as image:
        assert image.format == "JPEG"
        assert image.size == PIN_SIZE == (1000, 1500)


def test_pin_variant_keeps_the_whole_landscape_photo_centred():
    # A 1408x768 photo with a blue left edge band, a green right edge band and a
    # red middle. Fitted to 1000 px wide it is ~545 px tall, centred, so both
    # bands must survive sharp at the pin's edges. A 2:3 centre crop (a 512 px
    # slice of the middle) would lose both and leave only red.
    source = Image.new("RGB", (1408, 768), (200, 30, 30))
    source.paste((30, 30, 200), (0, 0, 141, 768))
    source.paste((30, 170, 30), (1267, 0, 1408, 768))
    out = io.BytesIO()
    source.save(out, format="JPEG", quality=95)

    pin = make_pin_variant(out.getvalue())
    with Image.open(io.BytesIO(pin)) as image:
        r, g, b = image.getpixel((500, 750))
        assert r > 150 and g < 80 and b < 80  # centre of the photo
        r, g, b = image.getpixel((20, 750))
        assert b > 150 and r < 80 and g < 80  # left edge band kept
        r, g, b = image.getpixel((980, 750))
        assert g > 120 and r < 80 and b < 80  # right edge band kept
        # Above the ~545 px photo is the blurred pad, not the sharp band.
        assert image.getpixel((20, 300)) != image.getpixel((20, 750))


def test_pin_variant_flattens_transparency_onto_the_pad():
    out = io.BytesIO()
    Image.new("RGBA", (800, 800), (0, 120, 0, 0)).save(out, format="PNG")
    pin = make_pin_variant(out.getvalue())
    assert pin is not None
    with Image.open(io.BytesIO(pin)) as image:
        assert image.mode == "RGB"
        assert image.size == PIN_SIZE
        # RGBA -> RGB without compositing exposed black for this fully
        # transparent source instead of flattening it onto an opaque canvas.
        r, g, b = image.getpixel((500, 750))
        assert r > 240 and g > 240 and b > 240


def test_pin_variant_flattens_mixed_alpha_before_blurring():
    source = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    source.paste((220, 20, 20, 255), (60, 60, 140, 140))
    out = io.BytesIO()
    source.save(out, format="PNG")

    pin = make_pin_variant(out.getvalue())
    assert pin is not None
    with Image.open(io.BytesIO(pin)) as image:
        # This point is just outside the sharp foreground subject, where the
        # RGB pad should carry its blurred red colour. Blurring RGBA first and
        # reusing the blurred alpha as a paste mask applies alpha twice and
        # leaves a pale seam here instead.
        r, g, b = image.getpixel((175, 750))
        assert r > 235 and r - g > 40 and r - b > 40


def test_pin_variant_of_undecodable_bytes_is_none():
    assert make_pin_variant(b"not an image") is None


def test_pin_endpoint_serves_the_pin_jpeg(app, client):
    with app.app_context():
        recipe_id = _add_image_recipe("pin-pie", _jpeg())

    resp = client.get(f"/api/recipes/{recipe_id}/image?pin=1")
    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"
    with Image.open(io.BytesIO(resp.data)) as image:
        assert image.size == PIN_SIZE
    assert resp.headers["Cache-Control"] == "public, max-age=86400"


@pytest.mark.parametrize(
    "query", ["pin=2", "pin=true", "pin=1&w=400", "pin=1&pin=2", "pin=1&pin=1"]
)
def test_pin_endpoint_rejects_bad_combined_or_duplicate_params(app, client, query):
    with app.app_context():
        recipe_id = _add_image_recipe("pin-bad-" + query.replace("=", "").replace("&", ""), _jpeg())

    assert client.get(f"/api/recipes/{recipe_id}/image?{query}").status_code == 400


def test_pin_of_a_private_recipe_still_requires_ownership(app, client):
    with app.app_context():
        recipe_id = _add_image_recipe("private-pin", _jpeg(), public=False)

    assert client.get(f"/api/recipes/{recipe_id}/image?pin=1").status_code == 404


def test_undecodable_image_pin_falls_back_uncached(app, client):
    broken = b"\x89PNG\r\n\x1a\nnot really a png"
    with app.app_context():
        recipe_id = _add_image_recipe("broken-pin", broken)

    resp = client.get(f"/api/recipes/{recipe_id}/image?pin=1")
    assert resp.status_code == 200
    assert resp.data == broken
    assert resp.headers["Cache-Control"] == "no-store"
