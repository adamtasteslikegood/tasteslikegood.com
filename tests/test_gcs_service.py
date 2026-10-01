import logging

import pytest
from google.api_core.exceptions import NotFound

from services import gcs_service
from services.gcs_service import _object_name_from_uri, _validated_object_name_from_uri


def test_versioned_gcs_uri_resolves_for_the_same_recipe():
    assert (
        _object_name_from_uri(
            "recipe-images",
            "recipe-1",
            "gs://recipe-images/images/recipe-1/lease-token.png",
        )
        == "images/recipe-1/lease-token.png"
    )


def test_gcs_uri_cannot_select_another_recipe_object():
    assert (
        _object_name_from_uri(
            "recipe-images",
            "recipe-1",
            "gs://recipe-images/images/recipe-2/lease-token.png",
        )
        == "images/recipe-1.png"
    )
    assert (
        _validated_object_name_from_uri(
            "recipe-images",
            "recipe-1",
            "gs://recipe-images/images/recipe-2/lease-token.png",
        )
        is None
    )


# ── download_image: direct download, no metadata round trip (KAN-268) ─────────


class _FakeBlob:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.download_kwargs = None

    def exists(self, *args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("download_image must not call blob.exists()")

    def reload(self, *args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("download_image must not call blob.reload()")

    def download_as_bytes(self, **kwargs):
        self.download_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.payload


class _FakeBucket:
    def __init__(self, blob):
        self._blob = blob
        self.requested = []

    def blob(self, name):
        self.requested.append(name)
        return self._blob


@pytest.fixture
def fake_bucket(monkeypatch):
    def install(blob):
        bucket = _FakeBucket(blob)
        monkeypatch.setattr(gcs_service, "_bucket", bucket)
        monkeypatch.setattr(gcs_service, "_bucket_name", "recipe-images")
        return bucket

    return install


def test_download_image_returns_bytes_without_existence_check(fake_bucket):
    blob = _FakeBlob(payload=b"\x89PNG\r\n\x1a\nbytes")
    bucket = fake_bucket(blob)

    result = gcs_service.download_image(
        "recipe-images", "recipe-1", "gs://recipe-images/images/recipe-1/v1.png"
    )

    assert result == b"\x89PNG\r\n\x1a\nbytes"
    assert bucket.requested == ["images/recipe-1/v1.png"]
    assert blob.download_kwargs == {"single_shot_download": True}


def test_download_image_missing_object_is_none_and_silent(fake_bucket, caplog):
    fake_bucket(_FakeBlob(error=NotFound("no such object")))

    with caplog.at_level(logging.DEBUG, logger="services.gcs_service"):
        result = gcs_service.download_image("recipe-images", "recipe-1")

    assert result is None
    # Same as the old exists() == False path: a missing object is not an error.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_download_image_other_failure_is_none_and_logged(fake_bucket, caplog):
    fake_bucket(_FakeBlob(error=RuntimeError("connection reset")))

    with caplog.at_level(logging.ERROR, logger="services.gcs_service"):
        result = gcs_service.download_image("recipe-images", "recipe-1")

    assert result is None
    assert any("Failed to download image" in r.getMessage() for r in caplog.records)
