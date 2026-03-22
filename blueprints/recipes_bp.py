"""
Recipe viewing blueprint.

Handles routes for:
- Homepage with recipe list
- Individual recipe display
- JSON viewer for recipes
"""

import json

from flask import Blueprint, Response, abort, render_template, request

from repositories.recipe_repository import (
    get_all_recipes,
    get_recipe,
    invalidate_cache,
    migrate_recipe_data,
    save_recipe,
    validate_recipe_filepath,
)
from services.stock_image_service import (
    _get_fallback_image,
    validate_and_refresh_stock_image,
)
from utils.session_utils import get_user_metadata

recipes_bp = Blueprint("recipes", __name__)


@recipes_bp.route("/")
def index():
    """The homepage route. Displays a list of all recipes."""
    recipes = get_all_recipes()
    return render_template("index.html", recipes=recipes)


@recipes_bp.route("/recipe/<filename>")
def show_recipe(filename):  # noqa: C901
    """
    Display a single recipe with auto-migration and lazy image loading.

    Note: This endpoint reads and writes to recipe JSON files. While we use file locking
    in the repository layer, concurrent requests may still experience some delays.
    """
    try:
        validate_recipe_filepath(filename)
    except ValueError:
        abort(404)

    try:
        # Load recipe with file locking
        recipe_data = get_recipe(filename)

        updated = False

        # --- Auto-Migration ---
        recipe_data, migrated = migrate_recipe_data(recipe_data, filename)
        if migrated:
            updated = True
            print(f"DEBUG: Auto-migrated {filename} on view.")

        # --- Lazy Load Images ---
        # 1. Stock Image - validate existing or fetch new
        user_metadata = get_user_metadata()
        new_url, stock_metadata, was_refreshed = validate_and_refresh_stock_image(
            recipe_data, user_metadata
        )

        if was_refreshed and new_url:
            recipe_data["stock_image_url"] = new_url
            print(f"DEBUG: Stock image updated for {recipe_data.get('name')}: {new_url}")

            # Update ai_metadata with stock image generation info
            if "ai_metadata" not in recipe_data:
                recipe_data["ai_metadata"] = {}
            recipe_data["ai_metadata"]["stock_image_generation"] = stock_metadata
            recipe_data["ai_metadata"]["images_working"] = True

            # Store attribution at top level for easy template access
            if stock_metadata and stock_metadata.get("attribution"):
                recipe_data["stock_image_attribution"] = stock_metadata["attribution"]

            updated = True
        elif not new_url and not recipe_data.get("stock_image_url"):
            # Complete fallback - use curated static image
            recipe_name = recipe_data.get("name", "food")
            recipe_data["stock_image_url"] = _get_fallback_image(recipe_name)
            print(f"DEBUG: Ultimate fallback to curated image: {recipe_data['stock_image_url']}")
            updated = True

        # 2. AI Image - REMOVED synchronous generation
        # Generation is now handled asynchronously via /api/generate_image/<filename>

        if updated:
            # Save the updated recipe with file locking
            save_recipe(filename, recipe_data)
            # Invalidate cache since recipe name may have changed during migration
            invalidate_cache()

        recipe_data["filename"] = filename
        return render_template("recipe.html", recipe=recipe_data)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error processing {filename}. Error: {e}")
        abort(500)


@recipes_bp.route("/recipe/<filename>/json")
def show_json(filename):
    """Display the raw JSON of a recipe with syntax highlighting."""
    try:
        validate_recipe_filepath(filename)
    except ValueError:
        abort(404)

    try:
        # Load recipe with file locking
        recipe_data = get_recipe(filename)

        pretty_json = json.dumps(recipe_data, indent=2)

        if request.args.get("raw") == "true":
            return Response(pretty_json, mimetype="application/json")

        recipe_data["filename"] = filename
        return render_template("json_viewer.html", recipe=recipe_data, recipe_json_str=pretty_json)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error processing {filename}. Error: {e}")
        abort(500)
