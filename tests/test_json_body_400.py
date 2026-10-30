"""A write route answers a body that is not a JSON object with a JSON 400 (KAN-426).

``request.get_json()`` raises ``BadRequest`` for a malformed body and
``UnsupportedMediaType`` for a non-JSON content type. Inside each route's broad
``except Exception`` both became 500 "Failed to create recipe", which also
reached error tracking as a server fault. A JSON array or string got past the
``"name" in data`` check and failed later, also as a 500.
"""

import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402

ROUTES = [
    ("POST", "/api/recipes"),
    ("PUT", "/api/recipes/no-such-recipe"),
    ("POST", "/api/collections"),
    ("POST", "/api/collections/no-such-collection/recipes"),
]

BODIES = [
    pytest.param(b"xxxx", "application/json", id="not-json"),
    pytest.param(b'{"name": "t"', "application/json", id="truncated-object"),
    pytest.param(b"", "application/json", id="empty"),
    pytest.param(b'["name", "recipe_id"]', "application/json", id="json-array"),
    pytest.param(b'"name recipe_id"', "application/json", id="json-string"),
    pytest.param(b'{"name": "t", "recipe_id": "r"}', "text/plain", id="not-json-content-type"),
]


@pytest.fixture
def client(tmp_path):
    flask_app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'test.db'}",
        SECRET_KEY="test-secret",
    )
    with flask_app.app_context():
        db.create_all()
        yield flask_app.test_client()
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("body,content_type", BODIES)
def test_body_that_is_not_a_json_object_is_a_json_400(client, method, path, body, content_type):
    resp = client.open(path, method=method, data=body, content_type=content_type)

    assert resp.status_code == 400, resp.data
    assert resp.is_json
    assert resp.get_json() == {"error": "Request body must be a JSON object"}
