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
import hashlib
import json
import os
import traceback

from flask import has_request_context, url_for
from werkzeug.utils import secure_filename

from services.gemini_service import get_genai_client
from utils.session_utils import get_user_metadata


def _anonymous_user_metadata():
    """Default metadata used when no Flask request context is available."""
    return {
        "user_id": None,
        "display_name": None,
        "is_authenticated": False,
        "session_id": None,
    }


def _image_filename(filename):
    """Map a recipe filename to its AI image filename inside static/images/.

    secure_filename() keeps the path contained but is lossy — it strips
    non-ASCII, and recipe filenames may contain Unicode word characters (see
    save_generated_recipe). Whenever sanitization changed the stem, append a
    digest of the original so distinct recipes never collide on one image.
    """
    stem = os.path.basename(filename)
    if stem.endswith(".json"):
        stem = stem[: -len(".json")]
    safe_stem = secure_filename(stem)
    if safe_stem != stem:
        digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:12]
        safe_stem = f"{safe_stem}-{digest}" if safe_stem else f"recipe-{digest}"
    return f"ai_{safe_stem}.png"


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

    # Imagen is a server-side operation. Identity-only OAuth credentials are
    # insufficient for image generation, so always use the configured server
    # credential instead of a signed-in user's session token.
    client = get_genai_client(None)

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

        from config import IMAGE_MODEL
        from google.genai import types as genai_types

        model_to_use = IMAGE_MODEL
        image_prompt = (
            f"A delicious, high-quality food photography shot of "
            f"{recipe_data.get('name')}. Professional lighting, appetizing."
        )
        generation_timestamp = datetime.datetime.now().isoformat()

        action = "Regenerating" if force_regenerate else "Generating"
        print(f"DEBUG: {action} AI image for {recipe_data.get('name')}...")

        response = client.models.generate_content(
            model=model_to_use,
            contents=image_prompt,
            config=genai_types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

        image_bytes = None
        if response.candidates:
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    image_bytes = part.inline_data.data
                    break
        if not image_bytes:
            return None, {"error": "No images generated", "status": 500}

        # Save image file
        image_url = save_image_bytes(image_bytes, filename)

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
        traceback_str = traceback.format_exc()
        print(f"Error generating image: {e}")
        print(traceback_str)

        # Log error to file for debugging
        # TODO: Implement log rotation using Python's logging module with RotatingFileHandler
        with open("recipe_error.txt", "a") as f:
            f.write(f"\nLast Error (Image Gen): {repr(e)}\nTraceback:\n{traceback_str}\n")

        return None, {"error": "Image generation failed", "status": 500}


def save_image_bytes(image_data, filename):
    """Save raw image bytes to static/images/ and return the URL path."""
    image_filename = _image_filename(filename)
    image_path = os.path.join("static", "images", image_filename)
    os.makedirs(os.path.dirname(image_path), exist_ok=True)
    with open(image_path, "wb") as img_f:
        img_f.write(image_data)
    if has_request_context():
        return url_for("static", filename=f"images/{image_filename}")
    return f"/static/images/{image_filename}"


def save_image_file(generated_image, filename):
    """Legacy wrapper: extract bytes from an Imagen response object."""
    return save_image_bytes(generated_image.image.image_bytes, filename)


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

    # Same helper as save_image_file() so the recorded path matches the file
    image_filename = _image_filename(filename)
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
