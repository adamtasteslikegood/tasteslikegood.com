"""The legacy UI's file-based API routes are gone in every environment (KAN-403, KAN-409).

KAN-403 closed them only when FLASK_ENV was "production"; staging sets no
FLASK_ENV, so they still ran there. KAN-409 deletes them outright.
"""

import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402

LEGACY_ROUTES = [
    "/api/generate_image/some_recipe.json",
    "/api/regenerate_image/some_recipe.json",
    "/api/report_recipe/some_recipe.json",
    "/api/models/refresh",
]


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.mark.parametrize("flask_env", [None, "production", "development"])
@pytest.mark.parametrize("path", LEGACY_ROUTES)
def test_legacy_route_is_404_in_every_environment(client, monkeypatch, path, flask_env):
    if flask_env is None:
        monkeypatch.delenv("FLASK_ENV", raising=False)
    else:
        monkeypatch.setenv("FLASK_ENV", flask_env)

    response = client.post(path, json={"reason": "test"})

    assert response.status_code == 404


def test_legacy_routes_are_not_registered():
    app = create_app()
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/models/refresh" not in rules
    for prefix in ("/api/generate_image/", "/api/regenerate_image/", "/api/report_recipe/"):
        assert not any(rule.startswith(prefix) for rule in rules), prefix


def test_disabled_model_refresh_never_calls_service(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "blueprints.api_bp.refresh_models_from_api",
        lambda credentials: calls.append(credentials) or ([], "api_key", None),
    )
    response = client.post("/api/models/refresh", json={})
    assert response.status_code == 404
    assert calls == []
