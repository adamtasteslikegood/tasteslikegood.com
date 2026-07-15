"""Regression tests for the global Flask error handler."""

import sys
from pathlib import Path

import pytest
from flask import abort

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402


@pytest.fixture
def client():
    app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )

    @app.get("/test/http-error")
    def http_error():
        abort(418)

    @app.get("/test/unexpected-error")
    def unexpected_error():
        raise RuntimeError("boom")

    return app.test_client()


def test_http_exception_keeps_its_status_code(client):
    response = client.get("/test/http-error")

    assert response.status_code == 418


def test_unexpected_exception_uses_generic_500_page(client):
    response = client.get("/test/unexpected-error")

    assert response.status_code == 500
    assert "Internal Server Error" in response.get_data(as_text=True)
