import json
import logging
from typing import Optional

from google.cloud import pubsub_v1

from config import GCP_PROJECT_ID

logger = logging.getLogger(__name__)

# The publisher client is created lazily: PublisherClient() needs Google
# credentials, so constructing it at import time made merely importing (or
# patching) this module raise DefaultCredentialsError anywhere without ADC.
# If PUBSUB_EMULATOR_HOST is set, the client automatically uses it.
_publisher: Optional[pubsub_v1.PublisherClient] = None


def _get_publisher() -> pubsub_v1.PublisherClient:
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def publish_message(topic_name: str, data: dict) -> str:
    """
    Publish a JSON message to a Pub/Sub topic.

    Args:
        topic_name: The ID of the topic (e.g., 'recipe-generation').
        data: The dictionary to publish as JSON.

    Returns:
        The message ID as a string.
    """
    publisher = _get_publisher()
    topic_path = publisher.topic_path(GCP_PROJECT_ID, topic_name)
    data_str = json.dumps(data)
    data_bytes = data_str.encode("utf-8")

    try:
        future = publisher.publish(topic_path, data=data_bytes)
        message_id = str(future.result())
        logger.info(f"Published message {message_id} to {topic_name}")
        return message_id
    except Exception as e:
        logger.error(f"Failed to publish to {topic_name}: {e}")
        raise
