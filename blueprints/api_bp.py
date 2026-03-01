"""
API blueprint for Tastes Like Good application.

Handles API routes for:
- Model listing and refresh
- AI image generation and regeneration
- Recipe reporting
- System status
- Data migration
- Jokes endpoint
"""

import os
import json
import csv
import datetime
from flask import Blueprint, jsonify, request, session

from config import CONFIG, GOOGLE_API_KEY, DEFAULT_MODEL, RECIPES_DIR
from repositories.recipe_repository import (
    get_recipe,
    validate_recipe_filepath,
    invalidate_cache,
)
from services.model_service import (
    load_models_from_cache,
    filter_and_sort_models,
    refresh_models_from_api,
)
from services.image_service import generate_ai_image
from services.reporting_service import ReportingService
from services.migration_service import MigrationService
import logging

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.route("/models", methods=["GET"])
def get_models():
    """Returns a curated list of Gemini models for recipe generation."""
    try:
        cached_models = load_models_from_cache()

        if cached_models:
            return jsonify(filter_and_sort_models(cached_models))

        # Fallback: return empty list if no cache available
        return jsonify([])

    except Exception as e:
        logger.error(f"Error fetching models: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.route("/models/refresh", methods=["POST"])
def refresh_models():
    """Fetches fresh models from Gemini API and updates the cache."""
    # Get session credentials if available
    session_credentials = session.get("credentials")

    # Refresh models from API
    models, auth_method, error = refresh_models_from_api(session_credentials)

    if error:
        return jsonify(
            {"error": error, "auth_method": auth_method}
        ), 401 if auth_method is None else 500

    # Calculate total fetched (need to reload cache for full count)
    cached_models = load_models_from_cache()
    total_fetched = len(cached_models) if cached_models else len(models)

    return jsonify(
        {
            "models": models,
            "auth_method": auth_method,
            "total_fetched": total_fetched,
            "message": f"Successfully fetched {total_fetched} models using {auth_method}",
        }
    )


@api_bp.route("/generate_image/<filename>", methods=["POST"])
def generate_recipe_image(filename):
    """
    Generates an AI image for a recipe asynchronously.

    Note: Uses file locking in the repository layer to prevent race conditions
    during concurrent read/write operations.
    """
    try:
        filepath = validate_recipe_filepath(filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        # Load recipe with file locking
        recipe_data = get_recipe(filename)

        # Generate image (force_regenerate=False checks if image exists)
        image_url, error = generate_ai_image(
            filepath, recipe_data, filename, force_regenerate=False
        )

        if error:
            return jsonify(error), error["status"]

        return jsonify({"image_url": image_url})

    except FileNotFoundError:
        return jsonify({"error": "Recipe not found"}), 404
    except Exception as e:
        logger.error(f"Image generation error for {filename}: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.route("/regenerate_image/<filename>", methods=["POST"])
def regenerate_recipe_image(filename):
    """Force regeneration of an AI image, even if one already exists."""
    try:
        filepath = validate_recipe_filepath(filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        # Load recipe with file locking
        recipe_data = get_recipe(filename)

        # Generate image (force_regenerate=True overwrites existing)
        image_url, error = generate_ai_image(
            filepath, recipe_data, filename, force_regenerate=True
        )

        if error:
            return jsonify(error), error["status"]

        return jsonify({"image_url": image_url})

    except FileNotFoundError:
        return jsonify({"error": "Recipe not found"}), 404
    except Exception as e:
        logger.error(f"Regeneration error for {filename}: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.route("/report_recipe/<filename>", methods=["POST"])
def report_recipe(filename):
    """Log a user report about a recipe or image."""
    try:
        # Validate filename to prevent path traversal
        validate_recipe_filepath(filename)

        data = request.get_json()
        reason = data.get("reason", "No reason provided")

        result = ReportingService.report_recipe(filename, reason)
        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error logging report for {filename}: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.route("/status", methods=["GET"])
def api_status():
    """Return API status and configuration info."""
    # Check database connection
    db_status = "unknown"
    db_error = None
    try:
        from extensions import db

        db.session.execute("SELECT 1")
        db_status = "connected"
    except Exception as e:
        db_status = "error"
        db_error = str(e)

    return jsonify(
        {
            "status": "running",
            "api_key_loaded": bool(GOOGLE_API_KEY),
            "default_model": DEFAULT_MODEL,
            "database": {"status": db_status, "error": db_error},
        }
    )


@api_bp.route("/migrate", methods=["POST"])
def run_migration():
    """
    Migrate all recipes in the recipes directory to the latest schema.
    """
    try:
        results = MigrationService.migrate_all_recipes()
        return jsonify(results)
    except Exception as e:
        logger.error(f"Migration error: {e}")
        return jsonify({"error": str(e)}), 500


@api_bp.route("/jokes", methods=["GET"])
def get_jokes():
    """Returns additional jokes from CSV file if it exists."""
    joke_file = CONFIG.get("app", {}).get("joke_file", "computer_jokes.csv")
    if not os.path.exists(joke_file):
        return jsonify([])
    try:
        with open(joke_file, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return jsonify([row["joke"] for row in reader if row.get("joke")])
        return jsonify([])
    try:
        with open(joke_file, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            return jsonify([row['joke'] for row in reader if row.get('joke')])
    except Exception as e:
        logger.error(f"Error loading jokes from {joke_file}: {e}")
        return jsonify([])
