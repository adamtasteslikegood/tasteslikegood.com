"""Tests for scripts/repair_missing_images.py (KAN-141)."""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from scripts.repair_missing_images import _is_imageless, enqueue, select_candidates  # noqa: E402


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


def _recipe(name, *, data=None, status="ready", public=False, canonical=False, slug=None):
    recipe = Recipe(
        id=str(uuid.uuid4()),
        name=name,
        status=status,
        slug=slug,
        is_public=public,
        is_canonical=canonical,
        data=data if data is not None else {"name": name},
    )
    db.session.add(recipe)
    return recipe


class TestIsImageless:
    def test_no_image_fields_is_imageless(self):
        assert _is_imageless({}) is True

    def test_url_without_bytes_is_still_imageless(self):
        # The blank-hero case: ai_image_url points at an object never written.
        assert _is_imageless({"ai_image_url": "/api/recipes/x/image"}) is True

    def test_gcs_or_base64_counts_as_imaged(self):
        assert _is_imageless({"ai_image_gcs": "gs://bucket/x.png"}) is False
        assert _is_imageless({"ai_image_data": "base64..."}) is False


class TestSelectCandidates:
    def test_orders_canonical_then_public_then_oldest(self, app):
        plain = _recipe("plain")
        public = _recipe("public", public=True, slug="public-r")
        canonical = _recipe("canonical", public=True, canonical=True, slug="canon-r")
        _recipe("imaged", data={"name": "imaged", "ai_image_gcs": "gs://b/i.png"})
        _recipe("busy", status="generating_image")
        db.session.commit()

        picked = select_candidates(10)

        assert [r.id for r in picked] == [canonical.id, public.id, plain.id]

    def test_respects_limit(self, app):
        for i in range(5):
            _recipe(f"r{i}")
        db.session.commit()

        assert len(select_candidates(2)) == 2


class TestEnqueue:
    def test_enqueues_via_pubsub_and_marks_generating(self, app, monkeypatch):
        published = []
        monkeypatch.setattr(
            "services.pubsub_service.publish_message",
            lambda topic, data: published.append((topic, data)) or "msg-1",
        )
        recipe = _recipe("needs image")
        db.session.commit()

        assert enqueue(recipe) is True

        topic, message = published[0]
        assert topic == "image-generation"
        assert message["recipe_id"] == recipe.id
        assert db.session.get(Recipe, recipe.id).status == "generating_image"

    def test_publish_failure_releases_queue_and_reports_false(self, app, monkeypatch):
        def boom(topic, data):
            raise RuntimeError("pubsub down")

        monkeypatch.setattr("services.pubsub_service.publish_message", boom)
        recipe = _recipe("needs image")
        db.session.commit()

        assert enqueue(recipe) is False
        assert db.session.get(Recipe, recipe.id).status == "ready"
