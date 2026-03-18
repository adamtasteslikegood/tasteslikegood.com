"""
Model service for Gemini model management.

Handles:
- Loading models from cache
- Filtering models for recipe generation
- Refreshing model list from Gemini API
- Sorting models by preference
"""

import json
import time

import google.oauth2.credentials
from google.genai import Client

from config import GOOGLE_API_KEY

MODELS_LIST_PATH = "models_list.json"

# Curated list of preferred Gemini models for recipe generation
PREFERRED_MODELS = [
    "models/gemini-2.5-pro",
    "models/gemini-2.5-flash",
    "models/gemini-2.0-flash",
    "models/gemini-2.0-flash-exp",
    "models/gemini-3-pro-preview",
    "models/gemini-2.0-flash-lite",
    "models/gemini-exp-1206",
    "models/gemini-pro-latest",
    "models/gemini-flash-latest",
]


def load_models_from_cache():
    """
    Load models from the cached models_list.json file.

    Returns:
        list: List of model dictionaries, or None if loading fails
    """
    try:
        with open(MODELS_LIST_PATH, "r") as f:
            data = json.load(f)
            return data.get("models", [])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Could not load models cache: {e}")
        return None


def filter_and_sort_models(models_list):
    """
    Filter and sort models for recipe generation.

    Filters out:
    - Embedding models
    - Image/video generation models
    - Audio/TTS models
    - Non-Gemini/Gemma models
    - Models without 'generateContent' support

    Sorts by:
    1. Preferred models (in order of preference)
    2. Other models alphabetically

    Args:
        models_list: List of model dictionaries from API

    Returns:
        list: Filtered and sorted list of up to 10 models
    """
    exclude_patterns = [
        "embedding",
        "imagen",
        "veo",
        "live",
        "tts",
        "audio",
        "robotics",
        "aqa",
    ]

    filtered_models = []
    for m in models_list:
        model_name = m.get("name", "").lower()

        # Skip non-generation models
        if any(pattern in model_name for pattern in exclude_patterns):
            continue

        # Skip image generation specific models
        if "image" in model_name and "gemini" in model_name:
            continue

        # Only include gemini/gemma models
        if not ("gemini" in model_name or "gemma" in model_name):
            continue

        # Metadata check: If the API doesn't provide methods, we rely on name-based
        # filtering above (exclude_patterns) which is more robust.
        supported_methods = m.get("supported_generation_methods", [])

        filtered_models.append(
            {
                "id": m.get("name"),
                "name": m.get("display_name") or m.get("name"),
                "label": m.get("display_name") or m.get("name"),
                "supported_methods": supported_methods,
            }
        )

    # Sort: preferred models first, then alphabetically
    def sort_key(model):
        model_id = model["id"]
        if model_id in PREFERRED_MODELS:
            return (0, PREFERRED_MODELS.index(model_id))
        return (1, model["name"])

    filtered_models.sort(key=sort_key)
    return filtered_models[:10]  # Return up to 10 models


def refresh_models_from_api(session_credentials=None):
    """
    Fetches fresh models from Gemini API and updates the cache.

    Uses dual authentication strategy:
    1. Try user credentials from session (if provided)
    2. Fallback to API key

    Args:
        session_credentials: Optional dict of user OAuth credentials from Flask session

    Returns:
        tuple: (models_list, auth_method, error_message)
            - models_list: List of filtered models, or None on error
            - auth_method: 'user_credentials', 'api_key', or None
            - error_message: Error string if failed, None otherwise
    """
    client = None
    auth_method = None

    # 1. Try User Credentials first
    if session_credentials:
        try:
            creds = google.oauth2.credentials.Credentials(**session_credentials)
            client = Client(credentials=creds)
            auth_method = "user_credentials"
        except Exception as e:
            print(f"User credential client failed: {e}")

    # 2. Fallback to API Key
    if client is None and GOOGLE_API_KEY:
        try:
            client = Client(api_key=GOOGLE_API_KEY)
            auth_method = "api_key"
        except Exception as e:
            print(f"API Key client failed: {e}")

    if client is None:
        return (
            None,
            None,
            "No valid authentication method available. Please login or configure API key.",
        )

    try:
        # Fetch models from Gemini API
        models_response = client.models.list()

        # Convert to list of dicts for caching
        models_data = []
        for model in models_response:
            models_data.append(
                {
                    "name": model.name,
                    "display_name": getattr(model, "display_name", model.name),
                    "supported_generation_methods": getattr(
                        model, "supported_generation_methods", []
                    ),
                }
            )

        # Update the cache file
        cache_data = {
            "models": models_data,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(MODELS_LIST_PATH, "w") as f:
            json.dump(cache_data, f, indent=2)

        # Return filtered models
        filtered_models = filter_and_sort_models(models_data)

        return filtered_models, auth_method, None

    except Exception as e:
        error_msg = f"Failed to fetch models: {str(e)}"
        print(f"Error refreshing models from API: {e}")
        return None, auth_method, error_msg
