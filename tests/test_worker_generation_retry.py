"""Regression tests for async recipe-generation robustness.

Production incident 2026-07-04: gemini-3.1-pro-preview intermittently returned
malformed/truncated JSON. The worker made a single attempt, marked the recipe
status "error" (surfacing "generation failed during async processing" in the
SPA), and logged only "Unknown error" because the JSONDecodeError path in
attempt_recipe_generation never set an error message.

Covers:
- worker retries a failed generation attempt before marking status "error"
- worker marks status "error" only after all attempts fail
- attempt_recipe_generation reports a real error message for invalid JSON
  instead of "Unknown error"
"""

import base64
import datetime
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ReadTimeout, Request

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402


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


@pytest.fixture(autouse=True)
def _skip_pubsub_auth(monkeypatch):
    """Bypass OIDC verification — these tests exercise the handler body."""
    monkeypatch.setattr("blueprints.worker_api_bp.PUBSUB_AUTH_OPTIONAL", True)


VALID_RECIPE = {
    "name": "Fresh Peach Salsa",
    "description": "A sweet and spicy salsa.",
    "ingredients": [],
    "instructions": [],
}


def _push_envelope(recipe_id):
    payload = {
        "recipe_id": recipe_id,
        "prompt": "peach salsa",
        "model": "gemini-3.1-pro-preview",
        "user_id": None,
        "guest_session_id": "guest-1",
    }
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"message": {"data": data}}


def _image_push_envelope(recipe_id, force_regenerate=False, image_request_id=None):
    payload = {
        "recipe_id": recipe_id,
        "user_id": None,
        "guest_session_id": "guest-1",
        "force_regenerate": force_regenerate,
    }
    if image_request_id is not None:
        payload["image_request_id"] = image_request_id
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"message": {"data": data}}


def _make_pending_recipe(recipe_id, status="generating"):
    recipe = Recipe(
        id=recipe_id,
        user_id=None,
        guest_session_id="guest-1",
        name="Generating...",
        status=status,
        data={"id": recipe_id, "name": "Generating..."},
    )
    db.session.add(recipe)
    db.session.commit()
    return recipe


def test_worker_retries_transient_generation_failure(app, client):
    """One bad model sample must not surface an error to the user."""
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        _make_pending_recipe(recipe_id)

    attempts = [
        (None, None, "Model returned invalid JSON (likely truncated)"),
        (VALID_RECIPE, json.dumps(VALID_RECIPE), None),
    ]
    with (
        patch(
            "blueprints.worker_api_bp.attempt_recipe_generation",
            side_effect=attempts,
        ) as mock_attempt,
        patch("blueprints.worker_api_bp.invalidate_recipe"),
        patch("services.pubsub_service.publish_message"),
    ):
        resp = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))

    assert resp.status_code == 200
    assert mock_attempt.call_count == 2
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.status == "ready"
        assert recipe.data["name"] == "Fresh Peach Salsa"
        assert recipe.data["ai_metadata"]["image_enqueue"]["status"] == "pending"


def test_worker_marks_error_after_all_attempts_fail(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        _make_pending_recipe(recipe_id)

    with (
        patch(
            "blueprints.worker_api_bp.attempt_recipe_generation",
            return_value=(None, None, "Model returned invalid JSON"),
        ) as mock_attempt,
        patch("blueprints.worker_api_bp.GENERATION_MAX_ATTEMPTS", 3),
    ):
        resp = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))

    assert resp.status_code == 200
    assert mock_attempt.call_count == 3
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.status == "error"


def test_invalid_json_sets_real_error_message(app):
    """attempt_recipe_generation must not report 'Unknown error' for bad JSON."""
    from blueprints import generation_bp
    from flask import session

    class FakeModels:
        def generate_content(self, **kwargs):
            class FakeResponse:
                # Truncated mid-string, as seen in production logs
                text = '{\n  "name": "Fresh Peach Salsa",\n  "description": "A swe'

            return FakeResponse()

    class FakeClient:
        options = []

        def __init__(self, **kwargs):
            self.options.append(kwargs)
            self.models = FakeModels()

    with (
        app.test_request_context("/api/generate"),
        patch.object(generation_bp, "Client", FakeClient),
        patch.object(generation_bp, "GOOGLE_API_KEY", "fake-key"),
    ):
        session["credentials"] = {"token": "identity-only-token"}
        recipe_data, raw_json, last_error = generation_bp.attempt_recipe_generation(
            "make peach salsa", "gemini-3.1-pro-preview"
        )

    assert recipe_data is None
    assert raw_json is None
    assert last_error != "Unknown error"
    assert "invalid JSON" in last_error
    assert FakeClient.options == [
        {
            "api_key": "fake-key",
            "http_options": generation_bp.GENAI_HTTP_OPTIONS,
        }
    ]


def test_schema_validation_failure_sets_real_error_message(app):
    """Schema failures must surface the ValidationError cause, not
    'Unknown error' (validate_recipe_data raises; it never returns False)."""
    from jsonschema import ValidationError

    from blueprints import generation_bp

    class FakeModels:
        def generate_content(self, **kwargs):
            class FakeResponse:
                text = json.dumps(VALID_RECIPE)

            return FakeResponse()

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    with (
        app.test_request_context("/api/generate"),
        patch.object(generation_bp, "Client", FakeClient),
        patch.object(generation_bp, "GOOGLE_API_KEY", "fake-key"),
        patch.object(
            generation_bp,
            "validate_recipe_data",
            side_effect=ValidationError("'name' is a required property"),
        ),
    ):
        recipe_data, raw_json, last_error = generation_bp.attempt_recipe_generation(
            "make peach salsa", "gemini-3.1-pro-preview"
        )

    assert recipe_data is None
    assert last_error != "Unknown error"
    assert "schema validation" in last_error
    assert "required property" in last_error


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3", 3),
        ("5", 5),
        ("0", 1),  # clamp: below 1 would skip generation entirely
        ("-2", 1),
        ("not-a-number", 3),  # bad env var must not break import
        (None, 3),
    ],
)
def test_parse_max_attempts_is_defensive(raw, expected):
    from blueprints.worker_api_bp import _parse_max_attempts

    assert _parse_max_attempts(raw) == expected


def test_image_worker_persists_generated_image(app, client):
    recipe_id = str(uuid.uuid4())
    image_request_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="ready")
        recipe.data["ai_metadata"] = {
            "image_request": {
                "id": image_request_id,
                "status": "pending",
                "force_regenerate": True,
            }
        }
        db.session.commit()

    class GeneratedImage:
        class Image:
            image_bytes = b"generated-image"

        image = Image()

    class FakeModels:
        def generate_images(self, **kwargs):
            class Response:
                generated_images = [GeneratedImage()]

            return Response()

    class FakeClient:
        models = FakeModels()

    with (
        patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()),
        patch("blueprints.worker_api_bp.GCS_BUCKET_NAME", None),
        patch("blueprints.worker_api_bp.invalidate_recipe"),
        patch("blueprints.worker_api_bp.invalidate_recipe_image"),
    ):
        response = client.post(
            "/api/worker/image",
            json=_image_push_envelope(
                recipe_id,
                force_regenerate=True,
                image_request_id=image_request_id,
            ),
        )

    assert response.status_code == 200
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.data["ai_image_data"] == base64.b64encode(b"generated-image").decode("ascii")
        assert recipe.data["ai_image_url"] == f"/api/recipes/{recipe_id}/image"
        assert recipe.data["ai_metadata"]["image_generation"]["success"] is True
        assert recipe.data["ai_metadata"]["image_request"]["status"] == "complete"


def test_image_worker_records_generation_failure(app, client):
    recipe_id = str(uuid.uuid4())
    image_request_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="ready")
        recipe.data["ai_metadata"] = {
            "image_request": {
                "id": image_request_id,
                "status": "pending",
                "force_regenerate": True,
            }
        }
        db.session.commit()

    class FakeModels:
        def generate_images(self, **kwargs):
            raise RuntimeError("Imagen unavailable")

    class FakeClient:
        models = FakeModels()

    with patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()):
        response = client.post(
            "/api/worker/image",
            json=_image_push_envelope(
                recipe_id,
                force_regenerate=True,
                image_request_id=image_request_id,
            ),
        )

    assert response.status_code == 500
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        metadata = recipe.data["ai_metadata"]["image_generation"]
        assert metadata["success"] is False
        assert metadata["error"] == "Image generation failed"
        assert recipe.data["ai_metadata"]["image_request"]["status"] == "pending"


def test_image_worker_records_terminal_generation_failure(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        _make_pending_recipe(recipe_id, status="ready")

    class FakeModels:
        def generate_images(self, **kwargs):
            raise ValueError("Invalid image request")

    class FakeClient:
        models = FakeModels()

    with patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()):
        response = client.post("/api/worker/image", json=_image_push_envelope(recipe_id))

    assert response.status_code == 200
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.data["ai_metadata"]["image_generation"]["success"] is False


def test_image_worker_retries_http_transport_failure(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        _make_pending_recipe(recipe_id, status="ready")

    class FakeModels:
        def generate_images(self, **kwargs):
            raise ReadTimeout("Imagen timed out", request=Request("POST", "https://example.test"))

    class FakeClient:
        models = FakeModels()

    with patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()):
        response = client.post("/api/worker/image", json=_image_push_envelope(recipe_id))

    assert response.status_code == 500


def test_legacy_image_delivery_completes_pending_non_force_request(app, client):
    recipe_id = str(uuid.uuid4())
    image_request_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="ready")
        recipe.data["ai_metadata"] = {
            "image_request": {
                "id": image_request_id,
                "status": "pending",
                "force_regenerate": False,
            }
        }
        db.session.commit()

    class GeneratedImage:
        class Image:
            image_bytes = b"generated-image"

        image = Image()

    class FakeModels:
        def generate_images(self, **kwargs):
            class Response:
                generated_images = [GeneratedImage()]

            return Response()

    class FakeClient:
        models = FakeModels()

    with (
        patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()),
        patch("blueprints.worker_api_bp.GCS_BUCKET_NAME", None),
        patch("blueprints.worker_api_bp.invalidate_recipe"),
        patch("blueprints.worker_api_bp.invalidate_recipe_image"),
    ):
        response = client.post("/api/worker/image", json=_image_push_envelope(recipe_id))

    assert response.status_code == 200
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.data["ai_metadata"]["image_request"]["id"] == image_request_id
        assert recipe.data["ai_metadata"]["image_request"]["status"] == "complete"


def test_completed_force_image_request_is_not_replayed(app, client):
    recipe_id = str(uuid.uuid4())
    image_request_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="ready")
        recipe.data.update(
            {
                "ai_image_data": "completed-image",
                "ai_image_url": f"/api/recipes/{recipe_id}/image",
                "ai_metadata": {
                    "image_request": {
                        "id": image_request_id,
                        "status": "complete",
                        "force_regenerate": True,
                    }
                },
            }
        )
        db.session.commit()

    with patch("blueprints.worker_api_bp.get_genai_client") as get_client:
        response = client.post(
            "/api/worker/image",
            json=_image_push_envelope(
                recipe_id,
                force_regenerate=True,
                image_request_id=image_request_id,
            ),
        )

    assert response.status_code == 200
    get_client.assert_not_called()


def test_image_worker_skips_existing_image(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="ready")
        recipe.data.update(
            {
                "ai_image_data": "existing-image",
                "ai_image_url": f"/api/recipes/{recipe_id}/image",
            }
        )
        db.session.commit()

    with patch("blueprints.worker_api_bp.get_genai_client") as get_client:
        response = client.post("/api/worker/image", json=_image_push_envelope(recipe_id))

    assert response.status_code == 200
    get_client.assert_not_called()


def test_image_worker_uploads_to_gcs_when_configured(app, client):
    recipe_id = str(uuid.uuid4())
    previous_gcs_uri = f"gs://recipe-images/images/{recipe_id}/previous-lease.png"
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="ready")
        recipe.data = {
            **recipe.data,
            "ai_image_gcs": previous_gcs_uri,
            "ai_image_url": f"/api/recipes/{recipe_id}/image",
        }
        db.session.commit()

    class GeneratedImage:
        class Image:
            image_bytes = b"generated-image"

        image = Image()

    class FakeModels:
        def generate_images(self, **kwargs):
            class Response:
                generated_images = [GeneratedImage()]

            return Response()

    class FakeClient:
        models = FakeModels()

    with (
        patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()),
        patch("blueprints.worker_api_bp.GCS_BUCKET_NAME", "recipe-images"),
        patch(
            "services.gcs_service.upload_image",
            return_value=f"gs://recipe-images/images/{recipe_id}/lease-token.png",
        ) as upload,
        patch("services.gcs_service.delete_image", return_value=True) as delete,
        patch("blueprints.worker_api_bp.invalidate_recipe"),
        patch("blueprints.worker_api_bp.invalidate_recipe_image"),
    ):
        response = client.post(
            "/api/worker/image",
            json=_image_push_envelope(recipe_id, force_regenerate=True),
        )

    assert response.status_code == 200
    upload.assert_called_once()
    assert upload.call_args.args == ("recipe-images", recipe_id, b"generated-image")
    uuid.UUID(upload.call_args.kwargs["version"])
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert (
            recipe.data["ai_image_gcs"] == f"gs://recipe-images/images/{recipe_id}/lease-token.png"
        )
        assert "ai_image_data" not in recipe.data
    delete.assert_called_once_with("recipe-images", recipe_id, previous_gcs_uri)


def test_recipe_worker_uses_owner_after_guest_login(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id)
        user = User(email="worker-owner@example.com", name="Worker Owner")
        db.session.add(user)
        db.session.flush()
        recipe.user_id = user.id
        recipe.guest_session_id = None
        db.session.commit()
        owner_id = user.id

    with (
        patch(
            "blueprints.worker_api_bp.attempt_recipe_generation",
            return_value=(VALID_RECIPE, json.dumps(VALID_RECIPE), None),
        ),
        patch("blueprints.worker_api_bp.invalidate_recipe"),
        patch("services.pubsub_service.publish_message"),
    ):
        response = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))

    assert response.status_code == 200
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.status == "ready"
        assert recipe.user_id == owner_id
        assert recipe.guest_session_id is None


def test_image_worker_uses_owner_after_guest_login(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="ready")
        user = User(email="image-worker-owner@example.com", name="Image Worker Owner")
        db.session.add(user)
        db.session.flush()
        recipe.user_id = user.id
        recipe.guest_session_id = None
        db.session.commit()
        owner_id = user.id

    class GeneratedImage:
        class Image:
            image_bytes = b"generated-image"

        image = Image()

    class FakeModels:
        def generate_images(self, **kwargs):
            class Response:
                generated_images = [GeneratedImage()]

            return Response()

    class FakeClient:
        models = FakeModels()

    with (
        patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()),
        patch("blueprints.worker_api_bp.GCS_BUCKET_NAME", None),
        patch("blueprints.worker_api_bp.invalidate_recipe"),
        patch("blueprints.worker_api_bp.invalidate_recipe_image"),
    ):
        response = client.post("/api/worker/image", json=_image_push_envelope(recipe_id))

    assert response.status_code == 200
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.user_id == owner_id
        assert recipe.guest_session_id is None
        assert recipe.data["ai_image_url"] == f"/api/recipes/{recipe_id}/image"


def test_recipe_worker_skips_completed_redelivery(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id)
        recipe.status = "ready"
        db.session.commit()

    with patch("blueprints.worker_api_bp.attempt_recipe_generation") as generate:
        response = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))

    assert response.status_code == 200
    generate.assert_not_called()


def test_recipe_worker_retries_unexpected_failure(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        _make_pending_recipe(recipe_id)

    with (
        patch(
            "blueprints.worker_api_bp.attempt_recipe_generation",
            return_value=(VALID_RECIPE, json.dumps(VALID_RECIPE), None),
        ),
        patch(
            "blueprints.worker_api_bp.db_recipe_repository.update_recipe_for_worker",
            side_effect=RuntimeError("database unavailable"),
        ),
    ):
        response = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))

    assert response.status_code == 500
    with app.app_context():
        assert db.session.get(Recipe, recipe_id).status == "generating"


def test_recipe_worker_redelivery_retries_only_failed_image_enqueue(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        _make_pending_recipe(recipe_id)

    with (
        patch(
            "blueprints.worker_api_bp.attempt_recipe_generation",
            return_value=(VALID_RECIPE, json.dumps(VALID_RECIPE), None),
        ) as generate,
        patch(
            "services.pubsub_service.publish_message",
            side_effect=[RuntimeError("Pub/Sub unavailable"), "message-id"],
        ) as publish,
        patch("blueprints.worker_api_bp.invalidate_recipe"),
    ):
        first = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))
        second = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))

    assert first.status_code == 500
    assert second.status_code == 200
    generate.assert_called_once()
    assert publish.call_count == 2
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.status == "ready"
        assert recipe.data["name"] == "Fresh Peach Salsa"
        assert recipe.data["ai_metadata"]["image_enqueue"]["status"] == "pending"


def test_recipe_redelivery_requeues_unclaimed_generating_image(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="generating_image")
        recipe.data = {
            **recipe.data,
            "ai_metadata": {"image_enqueue": {"status": "pending"}},
        }
        db.session.commit()

    with (
        patch("services.pubsub_service.publish_message") as publish,
        patch("blueprints.worker_api_bp.attempt_recipe_generation") as generate,
    ):
        response = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))

    assert response.status_code == 200
    publish.assert_called_once()
    generate.assert_not_called()


def test_image_worker_records_missing_credentials_and_retries(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        _make_pending_recipe(recipe_id, status="ready")

    with patch("blueprints.worker_api_bp.get_genai_client", return_value=None):
        response = client.post("/api/worker/image", json=_image_push_envelope(recipe_id))

    assert response.status_code == 500
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.status == "ready"
        metadata = recipe.data["ai_metadata"]["image_generation"]
        assert metadata["success"] is False
        assert metadata["error"] == "No AI credentials available"
        assert recipe.data["ai_metadata"]["image_enqueue"]["status"] == "complete"


def test_worker_claim_prevents_concurrent_generation_and_reclaims_stale_work(app):
    from repositories import db_recipe_repository

    recipe_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id)

        first_claim = db_recipe_repository.claim_recipe_for_worker(
            recipe_id,
            expected_status="generating",
            processing_status="processing",
            stale_after_seconds=120,
        )
        assert first_claim
        assert not db_recipe_repository.claim_recipe_for_worker(
            recipe_id,
            expected_status="generating",
            processing_status="processing",
            stale_after_seconds=120,
        )

        recipe = db.session.get(Recipe, recipe_id)
        recipe.updated_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=601)
        db.session.commit()

        second_claim = db_recipe_repository.claim_recipe_for_worker(
            recipe_id,
            expected_status="generating",
            processing_status="processing",
            stale_after_seconds=120,
        )
        assert second_claim
        assert second_claim != first_claim
        assert not db_recipe_repository.set_recipe_status_for_worker(
            recipe_id,
            "ready",
            first_claim,
            expected_status="processing",
            release_claim=True,
        )
        assert db_recipe_repository.set_recipe_status_for_worker(
            recipe_id,
            "ready",
            second_claim,
            expected_status="processing",
            release_claim=True,
        )
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.worker_claim_token is None


def test_worker_claims_queued_image_status_only_once(app):
    from repositories import db_recipe_repository

    recipe_id = str(uuid.uuid4())
    with app.app_context():
        _make_pending_recipe(recipe_id, status="generating_image")

        claim_token = db_recipe_repository.claim_recipe_for_worker(
            recipe_id,
            expected_status="generating_image",
            processing_status="generating_image",
            stale_after_seconds=120,
        )

        assert claim_token
        assert not db_recipe_repository.claim_recipe_for_worker(
            recipe_id,
            expected_status="generating_image",
            processing_status="generating_image",
            stale_after_seconds=120,
        )


def test_superseded_recipe_worker_cannot_commit_or_enqueue_image(app, client):
    from repositories import db_recipe_repository

    recipe_id = str(uuid.uuid4())
    replacement_claim = None
    with app.app_context():
        _make_pending_recipe(recipe_id)

    def reclaim_during_generation(*_args):
        nonlocal replacement_claim
        recipe = db.session.get(Recipe, recipe_id)
        recipe.updated_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=601)
        db.session.commit()
        replacement_claim = db_recipe_repository.claim_recipe_for_worker(
            recipe_id,
            expected_status="generating",
            processing_status="processing",
            stale_after_seconds=120,
        )
        return VALID_RECIPE, json.dumps(VALID_RECIPE), None

    with (
        patch(
            "blueprints.worker_api_bp.attempt_recipe_generation",
            side_effect=reclaim_during_generation,
        ),
        patch("services.pubsub_service.publish_message") as publish,
    ):
        response = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))

    assert response.status_code == 500
    assert replacement_claim
    publish.assert_not_called()
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.status == "processing"
        assert recipe.worker_claim_token == replacement_claim
        assert recipe.data["name"] == "Generating..."


def test_superseded_image_worker_cannot_commit_generated_bytes(app, client):
    from repositories import db_recipe_repository

    recipe_id = str(uuid.uuid4())
    replacement_claim = None
    with app.app_context():
        _make_pending_recipe(recipe_id, status="ready")

    class GeneratedImage:
        class Image:
            image_bytes = b"superseded-image"

        image = Image()

    class FakeModels:
        def generate_images(self, **_kwargs):
            nonlocal replacement_claim
            recipe = db.session.get(Recipe, recipe_id)
            recipe.updated_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=601)
            db.session.commit()
            replacement_claim = db_recipe_repository.claim_recipe_for_worker(
                recipe_id,
                expected_status="ready",
                processing_status="generating_image",
                stale_after_seconds=120,
            )

            class Response:
                generated_images = [GeneratedImage()]

            return Response()

    class FakeClient:
        models = FakeModels()

    with (
        patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()),
        patch("blueprints.worker_api_bp.GCS_BUCKET_NAME", None),
    ):
        response = client.post("/api/worker/image", json=_image_push_envelope(recipe_id))

    assert response.status_code == 500
    assert replacement_claim
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.status == "generating_image"
        assert recipe.worker_claim_token == replacement_claim
        assert "ai_image_data" not in recipe.data


def test_image_worker_preserves_user_edits_made_during_generation(app, client):
    from repositories import db_recipe_repository

    recipe_id = str(uuid.uuid4())
    image_request_id = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="ready")
        recipe.data = {
            **recipe.data,
            "ingredients": {"main": [{"name": "old ingredient"}]},
            "ai_metadata": {
                "image_request": {
                    "id": image_request_id,
                    "status": "pending",
                    "force_regenerate": True,
                }
            },
        }
        db.session.commit()

    class GeneratedImage:
        class Image:
            image_bytes = b"generated-image"

        image = Image()

    class FakeModels:
        def generate_images(self, **_kwargs):
            updated = db_recipe_repository.update_recipe(
                recipe_id,
                {
                    "name": "Edited While Generating",
                    "ingredients": {"main": [{"name": "new ingredient"}]},
                    "ai_metadata": {
                        "image_request": {
                            "id": "stale-client-value",
                            "status": "complete",
                        }
                    },
                },
                user_id=None,
                guest_session_id="guest-1",
            )
            assert updated is not None

            class Response:
                generated_images = [GeneratedImage()]

            return Response()

    class FakeClient:
        models = FakeModels()

    with (
        patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()),
        patch("blueprints.worker_api_bp.GCS_BUCKET_NAME", None),
        patch("blueprints.worker_api_bp.invalidate_recipe"),
        patch("blueprints.worker_api_bp.invalidate_recipe_image"),
    ):
        response = client.post(
            "/api/worker/image",
            json=_image_push_envelope(
                recipe_id,
                force_regenerate=True,
                image_request_id=image_request_id,
            ),
        )

    assert response.status_code == 200
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.name == "Edited While Generating"
        assert recipe.data["ingredients"]["main"][0]["name"] == "new ingredient"
        assert recipe.data["ai_image_data"] == base64.b64encode(b"generated-image").decode("ascii")
        assert recipe.data["ai_metadata"]["image_request"]["id"] == image_request_id
        assert recipe.data["ai_metadata"]["image_request"]["status"] == "complete"


def test_active_image_request_survives_recipe_upsert(app):
    from repositories import db_recipe_repository

    recipe_id = str(uuid.uuid4())
    image_request_id = str(uuid.uuid4())
    claim_token = str(uuid.uuid4())
    with app.app_context():
        recipe = _make_pending_recipe(recipe_id, status="generating_image")
        recipe.worker_claim_token = claim_token
        recipe.data["ai_metadata"] = {
            "image_request": {
                "id": image_request_id,
                "status": "pending",
                "force_regenerate": True,
            }
        }
        db.session.commit()

        updated = db_recipe_repository.create_recipe(
            {
                "id": recipe_id,
                "name": "Edited During Generation",
                "ai_metadata": {
                    "image_request": {
                        "id": "stale-client-value",
                        "status": "complete",
                    }
                },
            },
            user_id=None,
            guest_session_id="guest-1",
        )

        assert updated is not None
        assert updated.status == "generating_image"
        assert updated.worker_claim_token == claim_token
        assert updated.data["ai_metadata"]["image_request"]["id"] == image_request_id
        assert updated.data["ai_metadata"]["image_request"]["status"] == "pending"


def test_superseded_gcs_worker_deletes_its_versioned_orphan(app, client):
    from repositories import db_recipe_repository

    recipe_id = str(uuid.uuid4())
    replacement_claim = None
    uploaded_versions = []
    with app.app_context():
        _make_pending_recipe(recipe_id, status="ready")

    class GeneratedImage:
        class Image:
            image_bytes = b"superseded-image"

        image = Image()

    class FakeModels:
        def generate_images(self, **_kwargs):
            nonlocal replacement_claim
            recipe = db.session.get(Recipe, recipe_id)
            recipe.updated_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=601)
            db.session.commit()
            replacement_claim = db_recipe_repository.claim_recipe_for_worker(
                recipe_id,
                expected_status="ready",
                processing_status="generating_image",
                stale_after_seconds=120,
            )

            class Response:
                generated_images = [GeneratedImage()]

            return Response()

    class FakeClient:
        models = FakeModels()

    def upload(_bucket, _recipe_id, _bytes, *, version):
        uploaded_versions.append(version)
        return f"gs://recipe-images/images/{recipe_id}/{version}.png"

    with (
        patch("blueprints.worker_api_bp.get_genai_client", return_value=FakeClient()),
        patch("blueprints.worker_api_bp.GCS_BUCKET_NAME", "recipe-images"),
        patch("services.gcs_service.upload_image", side_effect=upload),
        patch("services.gcs_service.delete_image", return_value=True) as delete,
    ):
        response = client.post("/api/worker/image", json=_image_push_envelope(recipe_id))

    assert response.status_code == 500
    assert replacement_claim
    assert len(uploaded_versions) == 1
    assert uploaded_versions[0] != replacement_claim
    delete.assert_called_once_with(
        "recipe-images",
        recipe_id,
        f"gs://recipe-images/images/{recipe_id}/{uploaded_versions[0]}.png",
    )
