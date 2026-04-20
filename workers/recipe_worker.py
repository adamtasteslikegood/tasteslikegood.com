import json
import logging
import os
import sys
import datetime
from google.cloud import pubsub_v1

# Add parent directory to path so imports work correctly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app
from config import GCP_PROJECT_ID
from repositories import db_recipe_repository
from blueprints.generation_bp import attempt_recipe_generation, build_generation_prompt
from utils.cache_utils import invalidate_recipe

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def callback(message: pubsub_v1.subscriber.message.Message):
    with app.app_context():
        try:
            data = json.loads(message.data.decode("utf-8"))
            recipe_id = data["recipe_id"]
            prompt = data["prompt"]
            selected_model = data["model"]
            user_id = data.get("user_id")
            guest_session_id = data.get("guest_session_id")

            logger.info(f"Processing recipe generation for {recipe_id}")

            full_prompt = build_generation_prompt(prompt)
            # Worker doesn't have a session so it won't use User Credentials, only API Key
            recipe_data, recipe_json_str, last_error = attempt_recipe_generation(
                full_prompt, selected_model
            )

            if not recipe_data:
                logger.error(f"Recipe generation failed for {recipe_id}: {last_error}")
                db_recipe_repository.update_recipe_status(recipe_id, "error", user_id, guest_session_id)
                message.ack()
                return

            # Add metadata
            recipe_data["id"] = recipe_id
            recipe_data["user_id"] = user_id
            recipe_data["ai_metadata"] = {
                "recipe_generation": {
                    "model": selected_model,
                    "user_id": user_id,
                    "user_display_name": "Background Worker",
                    "is_authenticated": user_id is not None,
                    "session_id": guest_session_id,
                    "prompt": prompt,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "success": True,
                },
                "image_generation": None,
                "stock_image_generation": None,
            }

            db_recipe_repository.update_recipe(recipe_id, recipe_data, user_id, guest_session_id)
            db_recipe_repository.update_recipe_status(recipe_id, "ready", user_id, guest_session_id)
            
            invalidate_recipe(user_id, guest_session_id, recipe_id)
            logger.info(f"Successfully generated recipe {recipe_id}")
            message.ack()
            
            # Trigger image generation
            from services.pubsub_service import publish_message
            try:
                publish_message("image-generation", {
                    "recipe_id": recipe_id,
                    "user_id": user_id,
                    "guest_session_id": guest_session_id,
                    "force_regenerate": False
                })
                logger.info(f"Queued image generation for recipe {recipe_id}")
            except Exception as e:
                logger.error(f"Failed to queue image generation for {recipe_id}: {e}")

        except Exception as e:
            logger.error(f"Error processing recipe message: {e}")
            message.ack()

def start_recipe_worker():
    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(GCP_PROJECT_ID, "recipe-worker-sub")

    future = subscriber.subscribe(subscription_path, callback=callback)
    logger.info(f"Listening for messages on {subscription_path}...")
    
    try:
        future.result()
    except KeyboardInterrupt:
        future.cancel()

if __name__ == "__main__":
    start_recipe_worker()
