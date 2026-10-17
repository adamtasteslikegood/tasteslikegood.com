"""Flask refuses request bodies over MAX_CONTENT_LENGTH with a JSON 413 (KAN-408)."""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from config import MAX_CONTENT_LENGTH  # noqa: E402


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    return app


def test_limit_is_two_megabytes(app):
    assert MAX_CONTENT_LENGTH == 2 * 1024 * 1024
    assert app.config["MAX_CONTENT_LENGTH"] == MAX_CONTENT_LENGTH


def _oversize_route_app(app):
    # A route that reads its body, so the refusal is Flask's, not a route's own check.
    @app.route("/__test_body", methods=["POST"])
    def read_body():
        from flask import request

        return {"bytes": len(request.get_data())}

    return app.test_client()


def test_body_over_limit_with_content_length_is_413_json(app):
    client = _oversize_route_app(app)
    response = client.post(
        "/__test_body", data=b"x" * (MAX_CONTENT_LENGTH + 1), content_type="application/json"
    )
    assert response.status_code == 413
    assert response.get_json() == {"error": "The request body is too large"}


def test_declared_oversize_body_is_413_even_when_the_route_never_reads_it(app):
    calls = []

    @app.route("/__test_no_read", methods=["POST"])
    def no_read():
        calls.append(True)
        return {"ok": True}

    response = app.test_client().post(
        "/__test_no_read", data=b"x" * (MAX_CONTENT_LENGTH + 1), content_type="application/json"
    )

    assert response.status_code == 413
    assert response.get_json() == {"error": "The request body is too large"}
    assert calls == []


def test_chunked_body_is_never_read_past_the_limit(app):
    """A chunked body has no Content-Length to refuse on. Werkzeug stops reading
    at MAX_CONTENT_LENGTH, so a route never holds more than the limit. Express's
    1 MB cap is what answers 413 for an oversize chunked body."""
    _oversize_route_app(app)
    # Called through the WSGI app as gunicorn would for a chunked request: no
    # Content-Length, and the input marked as terminated by the server.
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/__test_body",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "CONTENT_TYPE": "application/json",
        "HTTP_TRANSFER_ENCODING": "chunked",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b"x" * (MAX_CONTENT_LENGTH * 2)),
        "wsgi.input_terminated": True,
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    statuses = []
    body = b"".join(
        app.wsgi_app(environ, lambda status, headers, exc=None: statuses.append(status))
    )

    assert statuses == ["200 OK"]
    assert json.loads(body) == {"bytes": MAX_CONTENT_LENGTH}


def test_body_under_limit_is_accepted(app):
    client = _oversize_route_app(app)
    response = client.post(
        "/__test_body", data=b"x" * (1024 * 1024), content_type="application/json"
    )
    assert response.status_code == 200
    assert response.get_json() == {"bytes": 1024 * 1024}
