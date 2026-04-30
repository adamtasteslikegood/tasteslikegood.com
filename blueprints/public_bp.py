"""
Public SSR blueprint — server-rendered routes for anonymous visitors and
search-engine crawlers.

Exposes:
    GET /r/<slug>   Single published recipe (is_public=True)
    GET /browse     Paginated index of all published recipes

These endpoints serve HTML directly so crawlers can index the content
without executing client-side JavaScript. All other traffic (Angular SPA,
JSON API) continues to flow through the existing blueprints.
"""

import logging
from math import ceil

from flask import Blueprint, abort, render_template, request
from sqlalchemy.orm import joinedload

from models import Recipe

logger = logging.getLogger(__name__)

public_bp = Blueprint("public", __name__)

BROWSE_PAGE_SIZE = 20


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

    return render_template("public/recipe.html", recipe=recipe)


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

    return render_template(
        "public/browse.html",
        recipes=recipes,
        page=page,
        total_pages=total_pages,
        total=total,
        page_size=BROWSE_PAGE_SIZE,
    )
