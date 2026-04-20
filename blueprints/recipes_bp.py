"""
Recipe viewing blueprint.

Handles routes for:
- Homepage with recipe list
- Individual recipe display
- JSON viewer for recipes
"""

import json

from flask import Blueprint, Response, abort, render_template, request

from repositories import db_recipe_repository
from repositories.recipe_repository import (
    get_all_recipes,
    get_recipe,
    invalidate_cache,
    migrate_recipe_data,
    save_recipe,
    validate_recipe_filepath,
)
from services.recipe_presenter import RecipePresenter
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


@recipes_bp.route("/browse")
def browse_public_recipes():
    """Display a paginated list of all public recipes."""
    page = request.args.get("page", 1, type=int)
    pagination = db_recipe_repository.get_public_recipes(page=page)
    return render_template("browse.html", pagination=pagination)


@recipes_bp.route("/r/<slug>")
def show_public_recipe(slug):
    """Display a single public recipe by its slug."""
    recipe = db_recipe_repository.get_recipe_by_slug(slug)
    if not recipe:
        abort(404)

    # Use request.host_url to get the full base URL (Express origin)
    base_url = request.host_url.rstrip("/")
    json_ld = RecipePresenter.get_json_ld(recipe, base_url)
    meta = RecipePresenter.get_meta_tags(recipe, base_url)

    import json

    return render_template(
        "recipe.html",
        recipe=recipe,
        is_public=True,
        json_ld=json.dumps(json_ld),
        meta=meta,
    )


@recipes_bp.route("/sitemap.xml")
def sitemap():
    """Generate a sitemap.xml for all public recipes."""
    from models import Recipe
    from flask import make_response

    recipes = Recipe.query.filter_by(is_public=True).all()
    
    # Use request.host_url to get the full base URL (Express origin)
    base_url = request.host_url.rstrip('/')
    
    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    
    # Add browse page
    xml.append(f'  <url><loc>{base_url}/browse</loc><changefreq>daily</changefreq><priority>0.8</priority></url>')
    
    # Add all public recipes
    for recipe in recipes:
        url = f"{base_url}/r/{recipe.slug}"
        lastmod = recipe.updated_at.strftime('%Y-%m-%d') if recipe.updated_at else recipe.created_at.strftime('%Y-%m-%d')
        xml.append(f'  <url><loc>{url}</loc><lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>1.0</priority></url>')
        
    xml.append('</urlset>')
    
    response = make_response('\n'.join(xml))
    response.headers["Content-Type"] = "application/xml"
    return response


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
