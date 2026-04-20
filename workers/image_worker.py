import json
import logging
import os
import sys
import datetime
import base64
from google.cloud import pubsub_v1

# Add parent directory to path so imports work correctly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app
from config import GCP_PROJECT_ID, GCS_BUCKET_NAME
from repositories import db_recipe_repository
from services.gemini_service import get_genai_client
from utils.cache_utils import invalidate_recipe, invalidate_recipe_image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def callback(message: pubsub_v1.subscriber.message.Message):
    with app.app_context():
        try:
            data = json.loads(message.data.decode("utf-8"))
            recipe_id = data["recipe_id"]
            user_id = data.get("user_id")
            guest_session_id = data.get("guest_session_id")
            force_regenerate = data.get("force_regenerate", False)

            logger.info(f"Processing image generation for {recipe_id}")

            recipe = db_recipe_repository.get_recipe_by_id(recipe_id, user_id, guest_session_id)
            if not recipe:
                logger.error(f"Recipe not found: {recipe_id}")
                message.ack()
                return

            recipe_data = recipe.data or {}
            
            # Check if image already exists
            has_real_image = bool(recipe_data.get("ai_image_data") or recipe_data.get("ai_image_gcs"))
            if not force_regenerate and has_real_image and recipe_data.get("ai_image_url"):
                logger.info(f"Image already exists for {recipe_id}")
                message.ack()
                return

            # Since this is a worker, we rely on the API key configured in get_genai_client fallback
            client = get_genai_client(None)

            if not client:
                logger.error("No AI credentials available for worker")
                message.ack()
                return

            recipe_name = recipe_data.get("name", "vegan dish")
            image_keywords = recipe_data.get("image_keywords", [])

            keyword_str = ", ".join(image_keywords) if image_keywords else ""
            image_prompt = (
                f"Professional food photography of {recipe_name}. "
                f"{keyword_str}. "
                f"High resolution, photorealistic, natural lighting, overhead shot, "
                f"delicious plating."
            )

            try:
                response = client.models.generate_images(
                    model="imagen-4.0-generate-001",
                    prompt=image_prompt,
                    config={"number_of_images": 1},
                )
                
                if not response.generated_images:
                    raise Exception("No images generated")
                    
                image_bytes = response.generated_images[0].image.image_bytes
                image_url = f"/api/recipes/{recipe_id}/image"

                if GCS_BUCKET_NAME:
                    from services.gcs_service import upload_image
                    gcs_uri = upload_image(GCS_BUCKET_NAME, recipe_id, image_bytes)
                    if not gcs_uri:
                        raise Exception("Failed to upload image to storage")
                    recipe_data["ai_image_gcs"] = gcs_uri
                    recipe_data.pop("ai_image_data", None)
                else:
                    image_b64 = base64.b64encode(image_bytes).decode("ascii")
                    recipe_data["ai_image_data"] = image_b64

                recipe_data["ai_image_url"] = image_url

                if "ai_metadata" not in recipe_data:
                    recipe_data["ai_metadata"] = {}

                recipe_data["ai_metadata"]["image_generation"] = {
                    "model": "imagen-4.0-generate-001",
                    "user_id": user_id,
                    "user_display_name": "Background Worker",
                    "is_authenticated": user_id is not None,
                    "session_id": guest_session_id,
                    "prompt": image_prompt,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "success": True,
                }

                db_recipe_repository.update_recipe(recipe_id, recipe_data, user_id, guest_session_id)
                invalidate_recipe_image(recipe_id)
                invalidate_recipe(user_id, guest_session_id, recipe_id)

                logger.info(f"Successfully generated image for recipe {recipe_id}")
                message.ack()

            except Exception as e:
                logger.error(f"Image generation failed for {recipe_id}: {e}")
                if "ai_metadata" not in recipe_data:
                    recipe_data["ai_metadata"] = {}
                recipe_data["ai_metadata"]["image_generation"] = {
                    "success": False,
                    "error": str(e),
                    "timestamp": datetime.datetime.now().isoformat()
                }
                db_recipe_repository.update_recipe(recipe_id, recipe_data, user_id, guest_session_id)
                message.ack()

        except Exception as e:
            logger.error(f"Error processing image message: {e}")
            message.ack()

def start_image_worker():
    subscriber = pubsub_v1.SubscriberClient()
    subscription_path = subscriber.subscription_path(GCP_PROJECT_ID, "image-worker-sub")

    future = subscriber.subscribe(subscription_path, callback=callback)
    logger.info(f"Listening for messages on {subscription_path}...")
    
    try:
        future.result()
    except KeyboardInterrupt:
        future.cancel()

if __name__ == "__main__":
    start_image_worker()