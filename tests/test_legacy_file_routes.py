"""The legacy UI's file-based API routes are not served in production (KAN-403)."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402

LEGACY_ROUTES = [
    "/api/generate_image/some_recipe.json",
    "/api/regenerate_image/some_recipe.json",
    "/api/report_recipe/some_recipe.json",
]


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.mark.parametrize("path", LEGACY_ROUTES)
def test_legacy_route_is_404_in_production(client, monkeypatch, path):
    monkeypatch.setenv("FLASK_ENV", "production")
    with (
        patch("blueprints.api_bp.get_recipe") as get_recipe,
        patch("blueprints.api_bp.generate_ai_image") as generate_ai_image,
        patch("blueprints.api_bp.ReportingService.report_recipe") as report_recipe,
    ):
        response = client.post(path, json={"reason": "test"})

    assert response.status_code == 404
    get_recipe.assert_not_called()
    generate_ai_image.assert_not_called()
    report_recipe.assert_not_called()


def test_legacy_image_route_still_works_outside_production(client, monkeypatch):
    monkeypatch.delenv("FLASK_ENV", raising=False)
    with (
        patch("blueprints.api_bp.get_recipe", return_value={"name": "Soup"}),
        patch(
            "blueprints.api_bp.generate_ai_image", return_value=("/static/x.png", None)
        ) as generate_ai_image,
    ):
        response = client.post("/api/regenerate_image/some_recipe.json")

    assert response.status_code == 200
    assert response.get_json() == {"image_url": "/static/x.png"}
    generate_ai_image.assert_called_once()
