"""Tests for the collections (cookbook) API — duplicate-cookbook race fix.

Covers the server-side defenses added to stop rapid repeat "Create" clicks from
persisting duplicate cookbooks:

- a partial unique index on (owner scope, name), surfaced as HTTP 409
- idempotent replay when the same id / Idempotency-Key is retried (HTTP 200)

Owner scope is (user_id) for authenticated users and (guest_session_id) for
guests; the two are enforced by separate partial unique indexes.
"""

import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import Cookbook  # noqa: E402
from models.user import User  # noqa: E402


@pytest.fixture
def app(tmp_path):
    # A file-backed SQLite DB is shared across the test client's per-request
    # connections (an in-memory DB is not), so schema created here is visible
    # to the HTTP handlers. See CLAUDE.md "Tests must hit the right DB engine".
    flask_app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'test.db'}",
        SECRET_KEY="test-secret",
    )
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(email):
    u = User(email=email, name=email.split("@")[0])
    db.session.add(u)
    db.session.commit()
    return u.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _create(client, name, **body):
    payload = {"name": name}
    payload.update(body)
    return client.post("/api/collections", json=payload)


def _count(client):
    resp = client.get("/api/collections")
    return len(resp.get_json()["collections"])


# ── Authenticated users ────────────────────────────────────────────────────


def test_duplicate_name_same_user_returns_409(app, client):
    with app.app_context():
        uid = _make_user("owner@example.com")
    _login(client, uid)

    r1 = _create(client, "Weeknight Dinners")
    assert r1.status_code == 201, r1.data

    r2 = _create(client, "Weeknight Dinners")
    assert r2.status_code == 409, r2.data
    body = r2.get_json()
    assert "already have a cookbook" in body["error"].lower()
    # The 409 payload echoes the existing cookbook so the client can reconcile.
    assert body["collection"]["name"] == "Weeknight Dinners"

    assert _count(client) == 1


def test_same_name_different_users_both_succeed(app):
    with app.app_context():
        uid1 = _make_user("a@example.com")
        uid2 = _make_user("b@example.com")

    # Use two independent clients so sessions don't bleed.
    client1 = _client_for(app)
    client2 = _client_for(app)
    _login(client1, uid1)
    _login(client2, uid2)

    assert _create(client1, "Shared Name").status_code == 201
    assert _create(client2, "Shared Name").status_code == 201

    assert _count(client1) == 1
    assert _count(client2) == 1


def test_idempotent_replay_same_id_returns_200(app, client):
    with app.app_context():
        uid = _make_user("owner@example.com")
    _login(client, uid)

    fixed_id = "cookbook-fixed-id-1"
    r1 = _create(client, "My Book", id=fixed_id)
    assert r1.status_code == 201, r1.data

    r2 = _create(client, "My Book", id=fixed_id)
    assert r2.status_code == 200, r2.data
    assert r2.get_json()["id"] == fixed_id

    assert _count(client) == 1


def test_idempotency_key_header_replays(app, client):
    with app.app_context():
        uid = _make_user("owner@example.com")
    _login(client, uid)

    headers = {"Idempotency-Key": "key-abc"}
    r1 = client.post("/api/collections", json={"name": "Header Book"}, headers=headers)
    assert r1.status_code == 201, r1.data
    assert r1.get_json()["id"] == "key-abc"

    r2 = client.post("/api/collections", json={"name": "Header Book"}, headers=headers)
    assert r2.status_code == 200, r2.data
    assert r2.get_json()["id"] == "key-abc"

    assert _count(client) == 1


def test_missing_name_still_400(app, client):
    with app.app_context():
        uid = _make_user("owner@example.com")
    _login(client, uid)
    resp = client.post("/api/collections", json={})
    assert resp.status_code == 400


# ── Guests ─────────────────────────────────────────────────────────────────


def test_guest_duplicate_name_returns_409(client):
    # No login → guest scope; the client keeps its guest session across requests.
    assert _create(client, "Guest Cookbook").status_code == 201
    assert _create(client, "Guest Cookbook").status_code == 409
    assert _count(client) == 1


def test_guest_and_user_may_share_a_name(app):
    with app.app_context():
        uid = _make_user("owner@example.com")

    guest = _client_for(app)
    authed = _client_for(app)
    _login(authed, uid)

    # Same name, different scopes (guest vs. authenticated) → no collision.
    assert _create(guest, "Overlap").status_code == 201
    assert _create(authed, "Overlap").status_code == 201


# ── Model-level constraint ─────────────────────────────────────────────────


def test_model_rejects_duplicate_user_name(app):
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        uid = _make_user("owner@example.com")
        db.session.add(Cookbook(id="c1", user_id=uid, name="Dup"))
        db.session.commit()
        db.session.add(Cookbook(id="c2", user_id=uid, name="Dup"))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def _client_for(app):
    return app.test_client()
