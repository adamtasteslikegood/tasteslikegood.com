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
import json
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402


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


def _make_pending_recipe(recipe_id):
    recipe = Recipe(
        id=recipe_id,
        user_id=None,
        guest_session_id="guest-1",
        name="Generating...",
        status="generating",
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
    with patch(
        "blueprints.worker_api_bp.attempt_recipe_generation",
        side_effect=attempts,
    ) as mock_attempt, patch(
        "blueprints.worker_api_bp.invalidate_recipe"
    ), patch("services.pubsub_service.publish_message"):
        resp = client.post("/api/worker/recipe", json=_push_envelope(recipe_id))

    assert resp.status_code == 200
    assert mock_attempt.call_count == 2
    with app.app_context():
        recipe = db.session.get(Recipe, recipe_id)
        assert recipe.status == "ready"
        assert recipe.data["name"] == "Fresh Peach Salsa"


def test_worker_marks_error_after_all_attempts_fail(app, client):
    recipe_id = str(uuid.uuid4())
    with app.app_context():
        _make_pending_recipe(recipe_id)

    with patch(
        "blueprints.worker_api_bp.attempt_recipe_generation",
        return_value=(None, None, "Model returned invalid JSON"),
    ) as mock_attempt, patch(
        "blueprints.worker_api_bp.GENERATION_MAX_ATTEMPTS", 3
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

    class FakeModels:
        def generate_content(self, **kwargs):
            class FakeResponse:
                # Truncated mid-string, as seen in production logs
                text = '{\n  "name": "Fresh Peach Salsa",\n  "description": "A swe'

            return FakeResponse()

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    with app.test_request_context("/api/generate"), patch.object(
        generation_bp, "Client", FakeClient
    ), patch.object(generation_bp, "GOOGLE_API_KEY", "fake-key"):
        recipe_data, raw_json, last_error = generation_bp.attempt_recipe_generation(
            "make peach salsa", "gemini-3.1-pro-preview"
        )

    assert recipe_data is None
    assert raw_json is None
    assert last_error != "Unknown error"
    assert "invalid JSON" in last_error


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

    with app.test_request_context("/api/generate"), patch.object(
        generation_bp, "Client", FakeClient
    ), patch.object(generation_bp, "GOOGLE_API_KEY", "fake-key"), patch.object(
        generation_bp,
        "validate_recipe_data",
        side_effect=ValidationError("'name' is a required property"),
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
