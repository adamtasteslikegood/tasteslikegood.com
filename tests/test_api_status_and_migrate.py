"""Regression tests for /api/status DB probe (#146) and /api/migrate auth (#145).

- /api/status must report database "connected" against a healthy DB; the old
  raw-string ``db.session.execute("SELECT 1")`` raised under SQLAlchemy 2.x and
  was swallowed into a permanent ``database: error``.
- POST /api/migrate must require the same admin bearer token as the
  /api/admin/* routes, and fail closed when ADMIN_API_TOKEN is unset.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app
from extensions import db


@pytest.fixture
def app():
    app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


class TestApiStatus:
    def test_reports_database_connected(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["database"]["status"] == "connected"
        assert body["database"]["error"] is None


class TestMigrateAuth:
    def test_no_token_rejected(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "sekrit")
        resp = client.post("/api/migrate")
        assert resp.status_code == 403

    def test_wrong_token_rejected(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "sekrit")
        resp = client.post("/api/migrate", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 403

    def test_fails_closed_when_token_unset(self, client, monkeypatch):
        monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
        resp = client.post("/api/migrate", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 403

    def test_valid_token_runs_migration(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "sekrit")
        with patch(
            "blueprints.api_bp.MigrationService.migrate_all_recipes",
            return_value={"migrated_count": 0, "files": []},
        ) as migrate_all:
            resp = client.post("/api/migrate", headers={"Authorization": "Bearer sekrit"})
        assert resp.status_code == 200
        assert resp.get_json() == {"migrated_count": 0, "files": []}
        migrate_all.assert_called_once()
