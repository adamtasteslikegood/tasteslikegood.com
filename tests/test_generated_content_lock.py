"""Server-written provenance and the generated-content lock (KAN-329, KAN-328).

Only the worker's text write may label a recipe ``generated``, and once a row
carries that label (or is a placeholder the worker still owns) a client write
can change nothing but ``is_public`` and ``personalNotes``. The public page
therefore only ever shows what the model wrote.
"""

import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402
from repositories import db_recipe_repository  # noqa: E402


@pytest.fixture
def app():
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


@pytest.fixture
def user(app):
    user = User(email="owner@example.com", name="Owner")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def logged_in(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
    return user


WORKER_TEXT = {
    "name": "Smoky Chili",
    "description": "The model's description",
    "ingredients": ["beans", "chipotle"],
    "instructions": ["simmer"],
    "notes": "The model's notes",
}


def _placeholder(user_id, recipe_id="gen-1", extra=None):
    """What POST /api/generate persists before the worker runs."""
    pending = {"id": recipe_id, "name": "Generating...", "user_id": user_id}
    pending.update(extra or {})
    row = db_recipe_repository.create_recipe(pending, user_id, status="generating")
    assert row is not None
    return row


def _worker_text_write(recipe_id, user_id, status="ready"):
    """Claim the placeholder and persist the model's text, as worker_api_bp does."""
    token = db_recipe_repository.claim_recipe_for_worker(
        recipe_id,
        expected_status="generating",
        processing_status="processing",
        stale_after_seconds=600,
    )
    assert token is not None
    data = {
        **WORKER_TEXT,
        "id": recipe_id,
        "user_id": user_id,
        "ai_metadata": {"recipe_generation": {}},
    }
    updated = db_recipe_repository.update_recipe_for_worker(
        recipe_id, data, token, status=status, expected_status="processing"
    )
    assert updated is not None
    db.session.expire_all()
    return db.session.get(Recipe, recipe_id)


def _generated_row(user_id, recipe_id="gen-1", is_public=False, slug=None):
    """A finished generated recipe, written the way the worker writes it."""
    _placeholder(user_id, recipe_id)
    row = _worker_text_write(recipe_id, user_id)
    if is_public:
        row.is_public = True
        row.slug = slug or recipe_id
        row.data = {**row.data, "is_public": True, "slug": row.slug}
        db.session.commit()
    return row


# ─── T1: provenance is written by the worker, never by a client ───────────


def test_placeholder_carries_no_origin(app, user):
    row = _placeholder(user.id)
    assert row.origin is None
    assert "origin" not in (row.data or {})


def test_worker_text_write_stamps_generated(app, user):
    _placeholder(user.id)
    row = _worker_text_write("gen-1", user.id)
    assert row.origin == "generated"
    assert row.data["origin"] == "generated"
    assert row.status == "ready"


def test_worker_text_write_stamps_generated_when_image_requested(app, user):
    _placeholder(user.id)
    row = _worker_text_write("gen-1", user.id, status="generating_image")
    assert row.origin == "generated"
    assert row.status == "generating_image"


def test_worker_text_write_replaces_placeholder_blob(app, user):
    """Nothing planted in the placeholder survives into the generated row."""
    _placeholder(user.id, extra={"notes": "planted", "stock_image_url": "https://x/y.jpg"})
    row = _worker_text_write("gen-1", user.id)
    assert row.data["notes"] == "The model's notes"
    assert "stock_image_url" not in row.data
    assert row.data["id"] == "gen-1"
    assert row.data["is_public"] is False
    assert row.name == "Smoky Chili"


def test_worker_text_write_drops_media_and_visibility_keys(app, user):
    """A prompt can make the text model echo image or visibility fields; the
    text write keeps the model's text and leaves media to the image worker."""
    _placeholder(user.id)
    token = db_recipe_repository.claim_recipe_for_worker(
        "gen-1",
        expected_status="generating",
        processing_status="processing",
        stale_after_seconds=600,
    )
    model_output = {
        **WORKER_TEXT,
        "id": "gen-1",
        "image_keywords": ["chili", "bowl"],
        "stock_image_url": "https://evil.example/x.jpg",
        "ai_image_data": "QUJD",
        "ai_image_url": "https://evil.example/y.png",
        "is_public": True,
        "slug": "attacker-slug",
        "origin": "manual",
        "sourceSlug": "someone-elses",
    }
    assert db_recipe_repository.update_recipe_for_worker(
        "gen-1", model_output, token, status="ready", expected_status="processing"
    )
    db.session.expire_all()
    row = db.session.get(Recipe, "gen-1")
    assert row.data["image_keywords"] == ["chili", "bowl"]
    for field in ("stock_image_url", "ai_image_data", "ai_image_url", "sourceSlug"):
        assert field not in row.data, field
    assert row.data["is_public"] is False and row.is_public is False
    assert "slug" not in row.data and row.slug is None
    assert row.origin == "generated" and row.data["origin"] == "generated"


def test_image_patch_does_not_stamp_origin(app, user):
    """The image-completion write never relabels a row."""
    row = db_recipe_repository.create_recipe(
        {"id": "manual-1", "name": "Mine", "origin": "manual"}, user.id
    )
    assert row is not None and row.origin == "manual"
    token = db_recipe_repository.claim_recipe_for_worker(
        "manual-1",
        expected_status="ready",
        processing_status="generating_image",
        stale_after_seconds=600,
    )
    assert token is not None
    patched = db_recipe_repository.patch_recipe_for_worker(
        "manual-1",
        {"ai_image_url": "/api/recipes/manual-1/image"},
        token,
        status="ready",
        expected_status="generating_image",
    )
    assert patched is not None
    db.session.expire_all()
    assert db.session.get(Recipe, "manual-1").origin == "manual"


def test_client_cannot_claim_generated_on_create(client, logged_in):
    resp = client.post("/api/recipes", json={"name": "Forged", "origin": "generated"})
    assert resp.status_code == 201
    assert resp.get_json()["origin"] is None
    assert db.session.get(Recipe, resp.get_json()["id"]).origin is None


def test_client_cannot_claim_generated_on_update(client, logged_in):
    created = client.post("/api/recipes", json={"name": "Plain"}).get_json()
    resp = client.put(f"/api/recipes/{created['id']}", json={"origin": "generated"})
    assert resp.status_code == 200
    assert resp.get_json()["origin"] is None


def test_client_can_still_label_manual_and_saved(client, logged_in):
    manual = client.post("/api/recipes", json={"name": "Mine", "origin": "manual"}).get_json()
    saved = client.post("/api/recipes", json={"name": "Copy", "origin": "saved"}).get_json()
    assert manual["origin"] == "manual"
    assert saved["origin"] == "saved"


# ─── T2: generated content is locked against client writes ────────────────

ATTACK_PAYLOAD = {
    "name": "Attacker title",
    "description": "attacker description",
    "ingredients": ["poison"],
    "instructions": ["do harm"],
    "notes": "attacker notes",
    "slug": "attacker-slug",
    "sourceSlug": "some-public-recipe",
    "ai_image_url": "data:image/png;base64,QUJD",
    "ai_image_data": "QUJD",
    "ai_image_gcs": "gs://bucket/evil.png",
    "stock_image_url": "https://evil.example/x.jpg",
    "image_keywords": ["evil"],
}


def _assert_worker_content_intact(row):
    assert row.name == "Smoky Chili"
    assert row.data["name"] == "Smoky Chili"
    assert row.data["description"] == "The model's description"
    assert row.data["ingredients"] == ["beans", "chipotle"]
    assert row.data["instructions"] == ["simmer"]
    assert row.data["notes"] == "The model's notes"
    assert row.source_slug is None
    assert row.source_recipe_id is None
    assert "sourceSlug" not in row.data or row.data["sourceSlug"] is None
    for field in (
        "ai_image_url",
        "ai_image_data",
        "ai_image_gcs",
        "stock_image_url",
        "image_keywords",
    ):
        assert row.data.get(field) != ATTACK_PAYLOAD[field], field


def test_put_on_generated_row_ignores_content(client, logged_in):
    _generated_row(logged_in.id)
    resp = client.put("/api/recipes/gen-1", json={**ATTACK_PAYLOAD, "id": "gen-1"})
    assert resp.status_code == 200
    db.session.expire_all()
    row = db.session.get(Recipe, "gen-1")
    _assert_worker_content_intact(row)
    assert row.slug is None


def test_upsert_post_on_generated_row_ignores_content(client, logged_in):
    """The SPA saves with POST on an existing id; that path is locked too."""
    _generated_row(logged_in.id)
    resp = client.post("/api/recipes", json={**ATTACK_PAYLOAD, "id": "gen-1"})
    assert resp.status_code == 201
    db.session.expire_all()
    row = db.session.get(Recipe, "gen-1")
    _assert_worker_content_intact(row)


def test_public_generated_row_keeps_slug_and_content(client, logged_in):
    _generated_row(logged_in.id, is_public=True, slug="smoky-chili")
    resp = client.put(
        "/api/recipes/gen-1", json={**ATTACK_PAYLOAD, "id": "gen-1", "is_public": True}
    )
    assert resp.status_code == 200
    db.session.expire_all()
    row = db.session.get(Recipe, "gen-1")
    _assert_worker_content_intact(row)
    assert row.slug == "smoky-chili"
    assert row.is_public is True


def test_placeholder_takeover_is_ignored(client, logged_in):
    """Own content POSTed to the returned id before the worker runs is dropped."""
    _placeholder(logged_in.id)
    resp = client.post("/api/recipes", json={**ATTACK_PAYLOAD, "id": "gen-1"})
    assert resp.status_code == 201
    db.session.expire_all()
    row = db.session.get(Recipe, "gen-1")
    assert row.name == "Generating..."
    assert row.data.get("ingredients") is None
    assert row.status == "generating"
    # ...and the worker's write then lands on a clean row.
    row = _worker_text_write("gen-1", logged_in.id)
    _assert_worker_content_intact(row)


def test_generated_row_accepts_personal_notes(client, logged_in):
    _generated_row(logged_in.id)
    resp = client.put("/api/recipes/gen-1", json={"personalNotes": "my tweak", "notes": "attacker"})
    assert resp.status_code == 200
    db.session.expire_all()
    row = db.session.get(Recipe, "gen-1")
    assert row.data["personalNotes"] == "my tweak"
    assert row.data["notes"] == "The model's notes"


def test_generated_row_accepts_unpublish(client, logged_in):
    _generated_row(logged_in.id, is_public=True, slug="smoky-chili")
    resp = client.put(
        "/api/recipes/gen-1", json={**ATTACK_PAYLOAD, "id": "gen-1", "is_public": False}
    )
    assert resp.status_code == 200
    db.session.expire_all()
    row = db.session.get(Recipe, "gen-1")
    assert row.is_public is False
    assert row.data["is_public"] is False
    _assert_worker_content_intact(row)


def test_generated_row_can_be_published_by_owner(client, logged_in):
    """Critical regression: the SPA's full-echo publish still works."""
    row = _generated_row(logged_in.id)
    echo = {**row.data, "is_public": True}
    resp = client.post("/api/recipes", json=echo)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["is_public"] is True
    assert body["origin"] == "generated"
    assert body["slug"] == "smoky-chili"


def test_non_generated_row_is_still_editable(client, logged_in):
    """Private manual rows keep their old behaviour; only generated content is locked."""
    created = client.post("/api/recipes", json={"name": "Mine", "origin": "manual"}).get_json()
    resp = client.put(f"/api/recipes/{created['id']}", json={"name": "Mine v2", "notes": "edited"})
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "Mine v2"


def test_admin_image_migration_does_not_promote_data_url(app, client, monkeypatch):
    """A data: URL in ai_image_url is never uploaded as the recipe's image."""
    import blueprints.generation_api_bp as gen_bp
    import services.gcs_service as gcs

    monkeypatch.setenv("ADMIN_API_TOKEN", "secret")
    monkeypatch.setattr(gen_bp, "GCS_BUCKET_NAME", "bucket")
    calls = []
    monkeypatch.setattr(gcs, "upload_image", lambda *a, **k: calls.append(a) or "gs://bucket/x.png")

    row = Recipe(
        id="data-url-1",
        name="Hosted",
        data={"id": "data-url-1", "name": "Hosted", "ai_image_url": "data:image/png;base64,QUJD"},
    )
    db.session.add(row)
    db.session.commit()

    resp = client.post("/api/admin/migrate-images", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    assert calls == []
    db.session.expire_all()
    assert db.session.get(Recipe, "data-url-1").data.get("ai_image_gcs") is None
