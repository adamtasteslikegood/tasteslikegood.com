"""
Google Cloud Storage service for recipe image management.

Handles uploading, retrieving, and deleting recipe images
in a GCS bucket. Replaces the legacy base64-in-PostgreSQL pattern.

Uses Application Default Credentials (ADC) — no extra config needed
on Cloud Run (the service account already has storage.objectAdmin).
"""

import logging
from typing import Optional

from google.cloud import storage

logger = logging.getLogger(__name__)

# Lazy-initialized GCS client and bucket
_client: Optional[storage.Client] = None
_bucket: Optional[storage.Bucket] = None
_bucket_name: Optional[str] = None


def _init_gcs(bucket_name: str) -> bool:
    """Initialize GCS client and bucket reference. Returns True if successful."""
    global _client, _bucket, _bucket_name
    if _bucket is not None and _bucket_name == bucket_name:
        return True
    try:
        _client = storage.Client()
        _bucket = _client.bucket(bucket_name)
        _bucket_name = bucket_name
        logger.info(f"GCS initialized with bucket: {bucket_name}")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize GCS client: {e}")
        _client = None
        _bucket = None
        _bucket_name = None
        return False


def _object_name(recipe_id: str) -> str:
    """Build the GCS object name for a recipe image."""
    return f"images/{recipe_id}.png"


def upload_image(bucket_name: str, recipe_id: str, image_bytes: bytes) -> Optional[str]:
    """
    Upload raw PNG bytes to GCS.

    Args:
        bucket_name: GCS bucket name
        recipe_id: Recipe UUID
        image_bytes: Raw PNG image bytes

    Returns:
        GCS URI (gs://bucket/images/id.png) on success, None on failure
    """
    if not _init_gcs(bucket_name):
        return None
    assert _bucket is not None
    try:
        blob = _bucket.blob(_object_name(recipe_id))
        blob.upload_from_string(image_bytes, content_type="image/png")
        gcs_uri = f"gs://{bucket_name}/{_object_name(recipe_id)}"
        logger.info(f"Uploaded image for recipe {recipe_id}: {gcs_uri}")
        return gcs_uri
    except Exception as e:
        logger.error(f"Failed to upload image for recipe {recipe_id}: {e}")
        return None


def download_image(bucket_name: str, recipe_id: str) -> Optional[bytes]:
    """
    Download raw PNG bytes from GCS.

    Args:
        bucket_name: GCS bucket name
        recipe_id: Recipe UUID

    Returns:
        Raw PNG bytes on success, None if not found or on failure
    """
    if not _init_gcs(bucket_name):
        return None
    assert _bucket is not None
    try:
        blob = _bucket.blob(_object_name(recipe_id))
        if not blob.exists():
            return None
        return blob.download_as_bytes()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(f"Failed to download image for recipe {recipe_id}: {e}")
        return None


def delete_image(bucket_name: str, recipe_id: str) -> bool:
    """
    Delete a recipe image from GCS.

    Args:
        bucket_name: GCS bucket name
        recipe_id: Recipe UUID

    Returns:
        True if deleted (or didn't exist), False on error
    """
    if not _init_gcs(bucket_name):
        return False
    assert _bucket is not None
    try:
        blob = _bucket.blob(_object_name(recipe_id))
        if blob.exists():
            blob.delete()
            logger.info(f"Deleted image for recipe {recipe_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to delete image for recipe {recipe_id}: {e}")
        return False


def image_exists(bucket_name: str, recipe_id: str) -> bool:
    """Check if an image exists in GCS for a given recipe."""
    if not _init_gcs(bucket_name):
        return False
    assert _bucket is not None
    try:
        blob = _bucket.blob(_object_name(recipe_id))
        return blob.exists()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(f"Failed to check image existence for recipe {recipe_id}: {e}")
        return False
