"""
Recipe generation blueprint.

Handles routes for:
- Recipe generation form display
- Recipe generation processing with AI
"""

import datetime
import json
import re
import time

from flask import Blueprint, redirect, render_template, request, url_for
from google.genai import Client, types
from jsonschema import ValidationError

from config import (
    CONFIG,
    DEFAULT_MODEL,
    GOOGLE_API_KEY,
    RECIPE_SCHEMA_PATH,
    RECIPE_VALIDATOR,
)
from repositories.recipe_repository import (
    invalidate_cache,
    save_recipe,
)
from services.gemini_service import GENAI_HTTP_OPTIONS
from utils import normalize_recipe_data
from utils.session_utils import get_user_metadata
from validators import validate_recipe_data

generation_bp = Blueprint("generation", __name__)


def validate_generation_input(prompt):
    """
    Validate user input for recipe generation.

    Args:
        prompt: User's recipe generation prompt

    Returns:
        tuple: (is_valid, error_message)
            - is_valid: True if valid, False otherwise
            - error_message: Error description or None
    """
    if not prompt:
        return False, "A prompt describing the desired recipe is required."

    if len(prompt) < 10:
        return False, "Prompt must be at least 10 characters."

    if len(prompt) > 500:
        return False, "Prompt must be no more than 500 characters."

    if RECIPE_VALIDATOR is None:
        return False, "Recipe schema is unavailable; cannot validate generated recipes."

    return True, None


def build_generation_prompt(user_prompt):
    """
    Build the full prompt for the AI model including schema.

    Args:
        user_prompt: User's recipe request

    Returns:
        str: Complete prompt with schema and instructions
    """
    # Load the JSON schema
    with open(RECIPE_SCHEMA_PATH, "r") as f:
        schema = f.read()

    full_prompt = (
        f"Generate a Vegan recipe based on the following request: '{user_prompt}'. "
        f"The output must be a valid JSON object that strictly follows this schema:\n"
        f"{schema}\n"
        f"IMPORTANT: Include 'image_keywords' - an array of 3-5 descriptive terms optimized for "
        f"stock photo searches (e.g., ['vegan buddha bowl', 'colorful vegetables', "
        f"'healthy lunch']). "
        f"Focus on visual descriptions of the finished dish, not recipe names.\n"
        f"Do NOT include these fields (we handle them separately): 'stock_image_url', "
        f"'ai_image_url', 'image', 'user_id', 'ai_metadata'. "
        f"Just omit them entirely - do not set them to null.\n"
        f"CRITICAL: Return ONLY the flat JSON object matching the schema. Do NOT nest it "
        f"inside a 'properties' or 'type' object. "
        f"The top-level keys must be 'name', 'description', 'ingredients', etc.\n"
        f"Do not include any text before or after the JSON object."
    )

    return full_prompt


def attempt_recipe_generation(
    full_prompt,
    selected_model,
    timeout_ms=None,
):  # noqa: C901
    """
    Attempt recipe generation using the server-side API key.

    Args:
        full_prompt: Complete prompt with schema
        selected_model: Model ID to use for generation
        timeout_ms: Optional HTTP timeout override for this model call

    Returns:
        tuple: (recipe_data, raw_json_string, error_message)
            - recipe_data: Parsed recipe dict or None
            - raw_json_string: Raw JSON response or None
            - error_message: Error description or None
    """

    def _attempt_with_client(client, source_name):
        """Helper to attempt generation with a specific client."""
        print(f"Attempting generation with {source_name} using {selected_model}...")
        try:
            response = client.models.generate_content(
                model=selected_model,
                contents=full_prompt,
                config={
                    "response_mime_type": "application/json",
                    "temperature": 0.7,
                },
            )

            text_response = response.text.strip()

            # Clean up markdown code blocks if present
            if text_response.startswith("```json"):
                text_response = text_response[7:]
            if text_response.startswith("```"):
                text_response = text_response[3:]
            if text_response.endswith("```"):
                text_response = text_response[:-3]

            text_response = text_response.strip()

            try:
                data = json.loads(text_response)

                # FIX: Check for nested 'properties' (common model error with schemas)
                if "properties" in data and "name" not in data:
                    print("DEBUG: Detected nested JSON structure. Flattening...")
                    data = data["properties"]

                # Normalize data to handle typos and variations
                data = normalize_recipe_data(data)

                # Validate against schema — validate_recipe_data raises
                # ValidationError (it never returns False), so surface it
                # with a clear cause instead of a generic error label.
                try:
                    validate_recipe_data(data)
                except ValidationError as e:
                    print(f"Validation failed for {source_name}: {e}")
                    raise ValueError(f"Generated recipe failed schema validation: {e}") from e

                return data, text_response
            except json.JSONDecodeError as e:
                print(f"JSON Decode Error for {source_name}: {text_response[:100]}...")
                raise ValueError(
                    f"Model returned invalid JSON "
                    f"(likely truncated, {len(text_response)} chars): {e}"
                ) from e

        except Exception as e:
            print(f"Generation error with {source_name}: {e}")
            raise e

    http_options = (
        GENAI_HTTP_OPTIONS if timeout_ms is None else types.HttpOptions(timeout=timeout_ms)
    )
    recipe_data = None
    recipe_json_str = None
    last_error_message = "Server API key is unavailable"

    if GOOGLE_API_KEY:
        try:
            api_client = Client(
                api_key=GOOGLE_API_KEY,
                http_options=http_options,
            )
            recipe_data, recipe_json_str = _attempt_with_client(api_client, "API Key")
        except Exception as e:
            print(f"API Key generation failed: {e}")
            last_error_message = f"API Key Error ({type(e).__name__}): {e}"

    return recipe_data, recipe_json_str, last_error_message


def save_generated_recipe(recipe_data, user_prompt, selected_model):
    """
    Save generated recipe with metadata to disk.

    Args:
        recipe_data: Recipe dictionary to save
        user_prompt: Original user prompt
        selected_model: Model used for generation

    Returns:
        str: Filename of saved recipe

    Raises:
        Exception: If saving fails
    """
    # Get comprehensive user metadata
    user_metadata = get_user_metadata()
    user_id = user_metadata["user_id"]

    # Add user identification to recipe
    recipe_data["user_id"] = user_id
    generation_timestamp = datetime.datetime.now().isoformat()

    recipe_data["ai_metadata"] = {
        # New comprehensive metadata structure
        "recipe_generation": {
            "model": selected_model,
            "user_id": user_id,
            "user_display_name": user_metadata["display_name"],
            "is_authenticated": user_metadata["is_authenticated"],
            "session_id": user_metadata["session_id"],
            "prompt": user_prompt,
            "timestamp": generation_timestamp,
            "success": True,
        },
        "image_generation": None,  # Will be filled by async /api/generate_image
        "stock_image_generation": None,  # Will be filled when stock image is fetched
        # Legacy fields for backwards compatibility
        "model": selected_model,
        "timestamp": generation_timestamp,
        "prompt": user_prompt,
        "images_working": True,  # Optimistic default
    }

    # Create a safe filename from the recipe name
    recipe_name = recipe_data.get("name", "untitled_recipe")
    # Remove special characters and limit length
    safe_name = re.sub(r"[^\w\s-]", "", recipe_name).strip().lower()
    safe_name = re.sub(r"[-\s]+", "_", safe_name)
    # Limit length to avoid filesystem issues
    safe_name = safe_name[:100]
    # Ensure we have a valid filename
    if not safe_name:
        safe_name = "untitled_recipe"
    filename = f"{safe_name}.json"

    # Save using repository (with file locking)
    save_recipe(filename, recipe_data)

    # Invalidate recipe list cache since we added a new recipe
    invalidate_cache()

    return filename


@generation_bp.route("/generate_recipe", methods=["GET", "POST"])
def generate_recipe():
    """
    Handles both displaying the form and processing the generation request.

    GET: Shows generation form
    POST: Processes recipe generation with validation, normalization, and saving
    """
    if request.method == "GET":
        # Show the form
        return render_template("generate_recipe.html", default_model=DEFAULT_MODEL)

    # --- POST: Process generation ---
    start_time = time.time()

    # 1. Validate input
    prompt = request.form.get("prompt", "").strip()
    is_valid, error_message = validate_generation_input(prompt)
    if not is_valid:
        return error_message, 400

    # 2. Get selected model
    default_model = CONFIG.get("app", {}).get("default_model", "models/gemini-2.5-flash")
    selected_model = request.form.get("model", default_model)

    # 3. Build full prompt
    full_prompt = build_generation_prompt(prompt)

    # 4. Attempt generation with dual auth strategy
    recipe_data, recipe_json_str, last_error_message = attempt_recipe_generation(
        full_prompt, selected_model
    )

    if recipe_data:
        try:
            # 5. Save recipe with metadata
            filename = save_generated_recipe(recipe_data, prompt, selected_model)

            end_time = time.time()
            print(f"Recipe generated successfully in {end_time - start_time:.2f} seconds.")

            # 6. Redirect to the new recipe's page
            return redirect(url_for("recipes.show_recipe", filename=filename))
        except Exception as e:
            last_error_message = f"File Save Error: {e}"

    # If we reached here, generation or saving failed
    # Log the error details securely
    try:
        with open("recipe_error.json", "a+") as f:
            f.write(f"{recipe_json_str}\n")
        with open("recipe_error.txt", "a") as f:
            f.write(f"Full prompt:\n{full_prompt}\n\nLast Error: {last_error_message}\n")
    except Exception as logging_error:
        print(f"Error while logging: {logging_error}")

    # Show the error response to the user. The failure detail was written to
    # the error logs above and must not be echoed back to the client.
    return (
        "Sorry, there was an error generating the recipe. Please try again.",
        500,
    )
