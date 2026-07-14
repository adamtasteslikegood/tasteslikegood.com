"""
AI image generation service using Gemini Imagen.

Deduplicates the previously separate generate_recipe_image and regenerate_recipe_image functions
into a single, flexible image generation workflow.

Handles:
- AI image generation via Imagen model
- Image file saving to static/images/
- Recipe metadata updates
- Error logging with traceback
"""

import datetime
import json
import os
import traceback

from flask import has_request_context, session, url_for

from services.gemini_service import get_genai_client
from utils.session_utils import get_user_metadata


def _safe_session_get(key, default=None):
    """Return a value from Flask's session only when a request context is active.

    This lets the service run in contexts without Flask (unit tests, CLI jobs)
    without triggering ``RuntimeError: Working outside of request context``.
    """
    if not has_request_context():
        return default
    return session.get(key, default)


def _anonymous_user_metadata():
    """Default metadata used when no Flask request context is available."""
    return {
        "user_id": None,
        "display_name": None,
        "is_authenticated": False,
        "session_id": None,
    }


def generate_ai_image(filepath, recipe_data, filename, force_regenerate=False):
    """
    Generate an AI image for a recipe using Gemini Imagen.

    This function combines the logic of both generate_recipe_image and regenerate_recipe_image
    into a single, reusable function.

    Args:
        filepath: Full path to the recipe JSON file
        recipe_data: Recipe dictionary loaded from JSON
        filename: Base filename of the recipe (e.g., 'recipe.json')
        force_regenerate: If True, regenerate even if image already exists

    Returns:
        tuple: (image_url, error_dict)
            - image_url: URL to the generated image, or None on error
            - error_dict: {'error': error_message, 'status': status_code} or None on success
    """
    # Check if image already exists and we're not forcing regeneration
    if not force_regenerate and recipe_data.get("ai_image_url"):
        return recipe_data["ai_image_url"], None

    # Clear existing image URL if forcing regeneration
    if force_regenerate and "ai_image_url" in recipe_data:
        del recipe_data["ai_image_url"]

    # Get authenticated client (session is only available inside request context)
    session_credentials = _safe_session_get("credentials")
    client = get_genai_client(session_credentials)

    if not client:
        return None, {"error": "No credentials available", "status": 500}

    try:
        # Get comprehensive user metadata when available; fall back to
        # anonymous metadata when called outside of a Flask request context
        # (e.g. from unit tests or background/CLI jobs).
        if has_request_context():
            user_metadata = get_user_metadata()
        else:
            user_metadata = _anonymous_user_metadata()
        _ = user_metadata["user_id"]  # noqa: F841

        model_to_use = "imagen-4.0-generate-001"
        image_prompt = (
            f"A delicious, high-quality food photography shot of "
            f"{recipe_data.get('name')}. Professional lighting, appetizing."
        )
        generation_timestamp = datetime.datetime.now().isoformat()

        action = "Regenerating" if force_regenerate else "Generating"
        print(f"DEBUG: {action} AI image for {recipe_data.get('name')}...")

        # Generate image
        response = client.models.generate_images(
            model=model_to_use, prompt=image_prompt, config={"number_of_images": 1}
        )

        if not response.generated_images:
            return None, {"error": "No images generated", "status": 500}

        # Save image file
        image_url = save_image_file(response.generated_images[0], filename)

        # Update recipe with image metadata
        update_recipe_with_image(
            recipe_data,
            image_url,
            model_to_use,
            user_metadata,
            image_prompt,
            generation_timestamp,
            filename,
        )

        # Save updated recipe
        with open(filepath, "w") as f:
            json.dump(recipe_data, f, indent=2)

        return image_url, None

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}" or "Unknown error"
        traceback_str = traceback.format_exc()
        print(f"Error generating image: {e}")
        print(traceback_str)

        # Log error to file for debugging
        # TODO: Implement log rotation using Python's logging module with RotatingFileHandler
        with open("recipe_error.txt", "a") as f:
            f.write(f"\nLast Error (Image Gen): {repr(e)}\nTraceback:\n{traceback_str}\n")

        return None, {"error": error_msg, "status": 500}


def save_image_file(generated_image, filename):
    """
    Save the generated image bytes to a file in static/images/.

    Args:
        generated_image: Image object from Gemini response
        filename: Recipe filename to base image filename on

    Returns:
        str: URL path to the saved image
    """
    image_data = generated_image.image.image_bytes

    # Sanitize filename (already validated by caller)
    safe_filename = os.path.basename(filename)
    image_filename = f"ai_{safe_filename.replace('.json', '.png')}"
    image_path = os.path.join("static", "images", image_filename)

    # Ensure directory exists
    os.makedirs(os.path.dirname(image_path), exist_ok=True)

    # Write image bytes
    with open(image_path, "wb") as img_f:
        img_f.write(image_data)

    # Return URL for template
    return url_for("static", filename=f"images/{image_filename}")


def update_recipe_with_image(
    recipe_data, image_url, model, user_metadata, prompt, timestamp, filename
):
    """
    Update recipe dictionary with image URL and generation metadata.

    Args:
        recipe_data: Recipe dictionary to update (modified in-place)
        image_url: URL to the generated image
        model: Model name used for generation
        user_metadata: User metadata dict from get_user_metadata()
        prompt: Image generation prompt
        timestamp: ISO timestamp of generation
        filename: Recipe filename for image path reference
    """
    recipe_data["ai_image_url"] = image_url

    # Update ai_metadata with comprehensive image generation info
    if "ai_metadata" not in recipe_data:
        recipe_data["ai_metadata"] = {}

    safe_filename = os.path.basename(filename)
    image_filename = f"ai_{safe_filename.replace('.json', '.png')}"
    image_path = os.path.join("static", "images", image_filename)

    recipe_data["ai_metadata"]["image_generation"] = {
        "model": model,
        "user_id": user_metadata["user_id"],
        "user_display_name": user_metadata["display_name"],
        "is_authenticated": user_metadata["is_authenticated"],
        "session_id": user_metadata["session_id"],
        "prompt": prompt,
        "timestamp": timestamp,
        "success": True,
        "image_path": image_path,
    }
    recipe_data["ai_metadata"]["images_working"] = True
