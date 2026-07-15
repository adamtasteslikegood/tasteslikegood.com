"""
Public SSR blueprint — server-rendered routes for anonymous visitors and
search-engine crawlers.

Exposes:
    GET /r/<slug>    Single published recipe (is_public=True)
    GET /browse      Paginated index of all published recipes
    GET /sitemap.xml Dynamic XML sitemap of public recipe routes

These endpoints serve HTML or XML directly so crawlers can index the content
without executing client-side JavaScript. All other traffic (Angular SPA,
JSON API) continues to flow through the existing blueprints.
"""

import logging
import os
from collections.abc import Mapping
from math import ceil
from typing import Any
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, SubElement, tostring

from flask import Blueprint, Response, abort, jsonify, render_template, request, url_for
from sqlalchemy.orm import joinedload

from models import Recipe

logger = logging.getLogger(__name__)

public_bp = Blueprint("public", __name__)

BROWSE_PAGE_SIZE = 20


def _public_base_url() -> str:
    configured = os.environ.get("FRONTEND_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return request.url_root.rstrip("/")


def _absolute_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("//"):
        return f"{request.scheme}:{value}"
    if not value.startswith("/"):
        value = f"/{value}"
    return f"{_public_base_url()}{value}"


def _canonical_url(endpoint: str, **values: Any) -> str:
    return f"{_public_base_url()}{url_for(endpoint, **values)}"


def _safe_minutes(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        minutes = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return minutes if minutes > 0 else None


def _minutes_to_iso_duration(value: Any) -> str | None:
    minutes = _safe_minutes(value)
    if minutes is None:
        return None
    return f"PT{minutes}M"


def _recipe_image_url(recipe: Recipe) -> str | None:
    data = recipe.data or {}
    ai_image_url = data.get("ai_image_url")
    if ai_image_url:
        return _absolute_url(ai_image_url)
    if data.get("ai_image_data") or data.get("ai_image_gcs"):
        return _canonical_url("generation_api.serve_recipe_image", recipe_id=recipe.id)
    return _absolute_url(data.get("stock_image_url"))


def _format_ingredient(ingredient: Mapping[str, Any]) -> str:
    amount = ingredient.get("amount")
    if isinstance(amount, (list, tuple)):
        if len(amount) >= 2:
            amount_text = f"{amount[0]}–{amount[1]}"
        elif len(amount) == 1:
            amount_text = str(amount[0])
        else:
            amount_text = ""
    elif amount not in (None, ""):
        amount_text = str(amount)
    else:
        amount_text = ""

    units = str(ingredient.get("units", "") or "").strip()
    name = str(ingredient.get("name", "") or "").strip()
    notes = str(ingredient.get("notes", "") or "").strip()

    parts = [part for part in [amount_text, units, name] if part]
    text = " ".join(parts)
    if notes:
        text = f"{text} ({notes})" if text else notes
    return text


def _recipe_ingredient_groups(
    data: dict[str, Any],
) -> list[tuple[str, list[dict[str, Any]]]]:
    raw_groups = data.get("ingredients")
    if not isinstance(raw_groups, dict):
        return []

    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for group_name, raw_ingredients in raw_groups.items():
        if not isinstance(raw_ingredients, list):
            continue
        ingredients = [ingredient for ingredient in raw_ingredients if isinstance(ingredient, dict)]
        if ingredients:
            groups.append((str(group_name), ingredients))
    return groups


def _recipe_instructions(data: dict[str, Any]) -> list[str]:
    instructions: list[str] = []
    for step in data.get("instructions", []) or []:
        if isinstance(step, dict):
            text = str(step.get("description", "") or "").strip()
        else:
            text = str(step).strip()
        if text:
            instructions.append(text)
    return instructions


def _clean_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, raw in value.items()
            if (cleaned := _clean_json(raw)) not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [cleaned for raw in value if (cleaned := _clean_json(raw)) not in (None, "", [], {})]
    return value


def _recipe_json_ld(recipe: Recipe, canonical_url: str, image_url: str | None) -> dict[str, Any]:
    data = recipe.data or {}
    prep_minutes = _safe_minutes(data.get("prepTime"))
    cook_minutes = _safe_minutes(data.get("cookTime"))
    total_minutes = (prep_minutes or 0) + (cook_minutes or 0)
    instructions = _recipe_instructions(data)

    ingredient_lines = [
        formatted
        for _, group in _recipe_ingredient_groups(data)
        for ingredient in group
        if (formatted := _format_ingredient(ingredient))
    ]

    author_name = None
    if recipe.user and recipe.user.name:
        author_name = recipe.user.name

    json_ld = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": recipe.name,
        "description": data.get("description") or "A vegan recipe from TastesLikeGood.",
        "url": canonical_url,
        "mainEntityOfPage": canonical_url,
        "image": [image_url] if image_url else None,
        "author": (
            {"@type": "Person", "name": author_name}
            if author_name
            else {"@type": "Organization", "name": "TastesLikeGood"}
        ),
        "datePublished": recipe.created_at.date().isoformat() if recipe.created_at else None,
        "dateModified": recipe.updated_at.date().isoformat() if recipe.updated_at else None,
        "prepTime": _minutes_to_iso_duration(prep_minutes),
        "cookTime": _minutes_to_iso_duration(cook_minutes),
        "totalTime": _minutes_to_iso_duration(total_minutes) if total_minutes else None,
        "recipeYield": (
            str(data.get("servings")) if data.get("servings") not in (None, "") else None
        ),
        "recipeIngredient": ingredient_lines or None,
        "recipeInstructions": [
            {"@type": "HowToStep", "position": index + 1, "text": text}
            for index, text in enumerate(instructions)
        ]
        or None,
        "keywords": ", ".join(data.get("tags", [])) if isinstance(data.get("tags"), list) else None,
        "recipeCategory": "Vegan",
    }
    cleaned: dict[str, Any] = _clean_json(json_ld)
    return cleaned


def _pinterest_share_url(canonical_url: str, image_url: str | None, recipe_name: str) -> str:
    params = {
        "url": canonical_url,
        "description": recipe_name,
    }
    if image_url:
        params["media"] = image_url
    return f"https://www.pinterest.com/pin/create/button/?{urlencode(params)}"


def _save_recipe_payload(recipe: Recipe, image_url: str | None) -> dict[str, Any]:
    data = recipe.data or {}
    return {
        "id": recipe.id,
        "name": recipe.name,
        "description": data.get("description", ""),
        "prepTime": _safe_minutes(data.get("prepTime")) or 0,
        "cookTime": _safe_minutes(data.get("cookTime")) or 0,
        "servings": data.get("servings") or 0,
        "ingredients": data.get("ingredients") or {},
        "instructions": data.get("instructions") or [],
        "notes": data.get("notes"),
        "tags": data.get("tags") or [],
        "stock_image_url": data.get("stock_image_url"),
        "ai_image_url": image_url,
        "image": image_url,
        "slug": recipe.slug,
        "is_public": recipe.is_public,
    }


@public_bp.route("/r/<slug>", methods=["GET"])
def show_public_recipe(slug):
    """Render the SSR view of a single published recipe.

    Returns 404 when no recipe matches the slug or the recipe is not public.
    """
    recipe = (
        Recipe.query.options(joinedload(Recipe.user))
        .filter(Recipe.slug == slug, Recipe.is_public.is_(True))
        .first()
    )

    if recipe is None:
        abort(404)

    data = recipe.data or {}
    canonical_url = _canonical_url("public.show_public_recipe", slug=recipe.slug)
    image_url = _recipe_image_url(recipe)
    description = data.get("description") or "A vegan recipe from TastesLikeGood."

    return render_template(
        "public/recipe.html",
        recipe=recipe,
        canonical_url=canonical_url,
        image_url=image_url,
        description=description,
        ingredient_groups=_recipe_ingredient_groups(data),
        recipe_json_ld=_recipe_json_ld(recipe, canonical_url, image_url),
        pinterest_share_url=_pinterest_share_url(canonical_url, image_url, recipe.name),
        spa_save_url=f"{_public_base_url()}/?save={recipe.slug}#kitchen",
        save_to_cookbook_url=f"{_public_base_url()}/#kitchen",
    )


@public_bp.route("/api/recipes/public/<slug>", methods=["GET"])
def public_recipe_json(slug):
    """JSON payload of a published recipe for the SPA's ?save=<slug> flow.

    Returns 404 when no recipe matches the slug or the recipe is not public.
    """
    recipe = Recipe.query.filter(Recipe.slug == slug, Recipe.is_public.is_(True)).first()
    if recipe is None:
        abort(404)
    return jsonify(_save_recipe_payload(recipe, _recipe_image_url(recipe)))


@public_bp.route("/browse", methods=["GET"])
def browse_public_recipes():
    """Paginated SSR index of published recipes.

    Uses ``joinedload`` on ``Recipe.user`` so the template can show author
    names without triggering an extra SELECT per row (N+1).
    """
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    base_query = Recipe.query.filter(Recipe.is_public.is_(True)).options(joinedload(Recipe.user))

    total = base_query.with_entities(Recipe.id).count()
    total_pages = max(1, ceil(total / BROWSE_PAGE_SIZE))
    page = min(page, total_pages)

    recipes = (
        base_query.order_by(Recipe.created_at.desc())
        .limit(BROWSE_PAGE_SIZE)
        .offset((page - 1) * BROWSE_PAGE_SIZE)
        .all()
    )

    canonical_url = _canonical_url(
        "public.browse_public_recipes",
        **({"page": page} if page > 1 else {}),
    )

    return render_template(
        "public/browse.html",
        recipes=recipes,
        page=page,
        total_pages=total_pages,
        total=total,
        page_size=BROWSE_PAGE_SIZE,
        canonical_url=canonical_url,
        description="Browse published vegan recipes from the TastesLikeGood community.",
    )


@public_bp.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    """Return an XML sitemap of the public recipe surface."""
    recipes = (
        Recipe.query.with_entities(Recipe.slug, Recipe.updated_at, Recipe.created_at)
        .filter(Recipe.is_public.is_(True), Recipe.slug.isnot(None))
        .order_by(Recipe.updated_at.desc(), Recipe.created_at.desc())
        .all()
    )

    latest_recipe_update = next(
        (
            recipe.updated_at or recipe.created_at
            for recipe in recipes
            if recipe.updated_at or recipe.created_at
        ),
        None,
    )

    entries = [
        {
            "loc": f"{_public_base_url()}/",
            "lastmod": latest_recipe_update.date().isoformat() if latest_recipe_update else None,
            "changefreq": "daily",
            "priority": "1.0",
        },
        {
            "loc": _canonical_url("public.browse_public_recipes"),
            "lastmod": latest_recipe_update.date().isoformat() if latest_recipe_update else None,
            "changefreq": "daily",
            "priority": "0.9",
        },
    ]

    for recipe in recipes:
        last_modified = recipe.updated_at or recipe.created_at
        entries.append(
            {
                "loc": _canonical_url("public.show_public_recipe", slug=recipe.slug),
                "lastmod": last_modified.date().isoformat() if last_modified else None,
                "changefreq": "weekly",
                "priority": "0.8",
            }
        )

    urlset = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
    for entry in entries:
        url = SubElement(urlset, "url")
        SubElement(url, "loc").text = entry["loc"]
        if entry.get("lastmod"):
            SubElement(url, "lastmod").text = entry["lastmod"]
        if entry.get("changefreq"):
            SubElement(url, "changefreq").text = entry["changefreq"]
        if entry.get("priority"):
            SubElement(url, "priority").text = entry["priority"]

    xml_body = tostring(urlset, encoding="utf-8", xml_declaration=True)
    return Response(xml_body, mimetype="application/xml")
