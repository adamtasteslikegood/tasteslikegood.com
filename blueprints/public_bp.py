"""
Public SSR blueprint — server-rendered routes for anonymous visitors and
search-engine crawlers.

Exposes:
    GET /r/<slug>    Single published recipe (is_public=True)
    GET /browse      Paginated index of all published recipes
    GET /browse/tag/<slug>  Paginated curated category hub
    GET /sitemap.xml Dynamic XML sitemap of public recipe routes

These endpoints serve HTML or XML directly so crawlers can index the content
without executing client-side JavaScript. All other traffic (Angular SPA,
JSON API) continues to flow through the existing blueprints.
"""

from datetime import datetime
from math import ceil
from typing import Any

from flask import (
    Blueprint,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from extensions import db
from models import Recipe, RetiredSlug
from services.public_hubs import (
    _PAGE_PARAM,
    BROWSE_PAGE_SIZE,
    BROWSE_SORT_LABELS,
    BROWSE_SORTS,
    HUB_PAGE_SIZE,
    MAX_TAG_FILTER_LENGTH,
    PAGINATION_WINDOW,
    RELATED_RECIPE_COUNT,
    _adjacent_recipes,
    _breadcrumbs,
    _browse_view,
    _carried_redirect_params,
    _carries_tag,
    _catalog_tag_rows,
    _counts_from_members,
    _footer_hubs,
    _hub_counts,
    _hub_members,
    _hub_url,
    _hydrate_hub_page,
    _linkable_hubs,
    _normalise_tag,
    _page_numbers,
    _page_values,
    _pagination,
    _recipe_breadcrumbs,
    _recipe_hubs,
    _related_recipes,
    _requested_page,
    _row_tags,
    _sitemap_hub_entries,
)
from services.public_images import (
    CARD_IMAGE_SIZES,
    CARD_IMAGE_WIDTHS,
    HERO_IMAGE_SIZES,
    HERO_IMAGE_WIDTHS,
    _card_image,
    _image_variants,
    _image_version_source,
    _image_version_token,
    _own_image_url,
    _pin_image_url,
    _recipe_image,
    _recipe_image_url,
    _rendered_image,
    _rendered_image_url,
    _resolve_source_for_image,
    _serves_own_image_bytes,
    _versioned_image_url,
)
from services.public_seo import (
    _PIN_ABBREVIATIONS,
    _PIN_INITIALISM,
    _SENTENCE_END,
    DEFAULT_RECIPE_DESCRIPTION,
    MAX_PIN_DESCRIPTION_LENGTH,
    MAX_PIN_TAIL_LENGTH,
    PIN_DESCRIPTION_TAGS,
    RECIPE_CATEGORY_BY_TAG,
    RECIPE_CUISINE_BY_TAG,
    SITE_SOCIAL_PROFILES,
    STEP_NAME_LIMIT,
    _absolute_url,
    _breadcrumb_json_ld,
    _build_sitemap_xml,
    _canonical_url,
    _clean_json,
    _collection_json_ld,
    _first_pin_sentence,
    _format_ingredient,
    _meta_description,
    _minutes_to_iso_duration,
    _page_title,
    _pin_description,
    _pinterest_share_url,
    _public_base_url,
    _recipe_description,
    _recipe_ingredient_groups,
    _recipe_instructions,
    _recipe_json_ld,
    _recipe_keywords,
    _recipe_tags,
    _safe_minutes,
    _save_recipe_payload,
    _site_organization,
    _step_name,
    _tag_labels,
    _trim_to_words,
)
from services.recipe_metadata import SITE_TITLE_SUFFIX, metadata_for_page
from services.tag_hubs import (
    HUBS_BY_SLUG,
    MIN_INDEXABLE_RECIPES,
)

public_bp = Blueprint("public", __name__)

NEWEST_RECIPES_DEFAULT = 8
NEWEST_RECIPES_MAX = 12


@public_bp.app_context_processor
def _inject_footer_hubs() -> dict[str, Any]:
    return {"footer_hubs": _footer_hubs}


def _missing_recipe_response(
    slug: str, endpoint: str, *, canonical: bool, json: bool = False
) -> ResponseReturnValue:
    """What a slug with no live public recipe answers (KAN-288).

    - retired by a delete, or its recipe has since been deleted -> 410 Gone:
      the page existed and was removed on purpose, and never comes back;
    - retired by a rename and that recipe is public under a new slug -> 302;
    - anything else (never existed, or unpublished, which is reversible) -> 404.

    ``json=True`` returns a JSON body for 404/410 (the app-level errorhandlers
    render HTML, which would break clients that expect JSON). The 302 branch
    still redirects — the SPA follows it exactly like the SSR route does.
    """
    retired = db.session.get(RetiredSlug, slug)
    if retired is None:
        return (jsonify({"error": "Recipe not found"}), 404) if json else abort(404)
    if retired.recipe_id is not None:
        target = db.session.get(Recipe, retired.recipe_id)
        if target is not None:
            if target.is_public and target.slug and target.slug != slug:
                if canonical:
                    location = _canonical_url(
                        endpoint,
                        slug=target.slug,
                        **_carried_redirect_params(keep_save=True),
                    )
                else:
                    location = url_for(endpoint, slug=target.slug)
                # This alias is reclaimable by its recipe, so the redirect must
                # not be cached permanently. A cached 301 can redirect away
                # from the alias after it becomes live again (or create a loop).
                return redirect(location, code=302)
            return (jsonify({"error": "Recipe not found"}), 404) if json else abort(404)
    return (jsonify({"error": "Recipe removed"}), 410) if json else abort(410)


@public_bp.route("/r/<slug>", methods=["GET"])
def show_public_recipe(slug):
    """Render the SSR view of a single published recipe.

    Returns 404 when no recipe matches the slug or the recipe is not public,
    410 when the slug belonged to a deleted recipe (KAN-288).
    """
    recipe = (
        Recipe.query.options(joinedload(Recipe.user))
        .filter(Recipe.slug == slug, Recipe.is_public.is_(True))
        .first()
    )
    if recipe is None:
        return _missing_recipe_response(slug, "public.show_public_recipe", canonical=True)

    data = recipe.data or {}
    canonical_url = _canonical_url("public.show_public_recipe", slug=recipe.slug)
    # KAN-195: versioned, so a regenerated photo is not hidden behind the
    # 24-hour Cache-Control on the (otherwise unchanging) image URL.
    image_url, image_variants = _rendered_image(recipe)
    # Pinterest pin media reuses the page's own byte-gated URL: pinning a dead
    # link creates broken pins, and a run of broken pins from a fresh domain
    # trips Pinterest's new-account spam heuristics (Backend #203/#204).
    # Reused rather than recomputed — a second call would re-issue the
    # saved-copy source lookup for no gain. It gets the versioned URL too: a
    # pin whose media is the pre-regeneration photo is the same defect wearing
    # a different hat.
    #
    # KAN-284: when the photo is ours, the pin uses its 2:3 variant instead.
    pinterest_image_url = (image_variants or {}).get("pin") or image_url
    description = _recipe_description(data)
    instructions = _recipe_instructions(data)
    tags = _recipe_tags(data)
    # Only used behind ``pinterest_image_url`` (share button + ``data-pin-*``);
    # skip the tag/regex/trim work on imageless recipes that never emit it.
    pin_description = (
        _pin_description(recipe.name, description, tags) if pinterest_image_url else None
    )
    catalog = _catalog_tag_rows()
    breadcrumbs = _recipe_breadcrumbs(recipe, tags, catalog)
    # KAN-388: the title and description the metadata engine wrote, when it has
    # a version for this content; plain truncation until then.
    metadata = metadata_for_page(recipe)

    return render_template(
        "public/recipe.html",
        recipe=recipe,
        page_title=metadata.seo_title if metadata else _page_title(recipe.name),
        meta_description=(metadata and metadata.meta_description) or _meta_description(description),
        breadcrumbs=breadcrumbs,
        breadcrumb_json_ld=_breadcrumb_json_ld(breadcrumbs),
        recipe_hubs=_recipe_hubs(tags, catalog),
        adjacent=_adjacent_recipes(recipe, catalog),
        related_recipes=[
            {"recipe": related, "image": _card_image(related)}
            for related in _related_recipes(recipe, catalog)
        ],
        card_image_sizes=CARD_IMAGE_SIZES,
        canonical_url=canonical_url,
        image_url=image_url,
        image_variants=image_variants,
        image_sizes=HERO_IMAGE_SIZES,
        description=description,
        ingredient_groups=_recipe_ingredient_groups(data),
        instructions=instructions,
        tags=tags,
        recipe_json_ld=_recipe_json_ld(recipe, canonical_url, image_url),
        pinterest_share_url=(
            _pinterest_share_url(canonical_url, pinterest_image_url, pin_description)
            if pinterest_image_url and pin_description
            else None
        ),
        pin_image_url=pinterest_image_url,
        pin_description=pin_description,
        spa_save_url=f"{_public_base_url()}/?save={recipe.slug}#kitchen",
    )


@public_bp.route("/r/<slug>/", methods=["GET"])
def redirect_trailing_slash_recipe(slug):
    """``/r/<slug>/`` → 301 to the canonical ``/r/<slug>`` (KAN-273).

    A trailing-slash link from another site used to dead-end on a 404. The
    target decides existence, so this never reveals whether a slug is public.

    Carries forward the ``utm_*`` campaign parameters and the SPA ``?save=``
    handoff, and nothing else: only allow-listed parameters, rebuilt by
    ``url_for`` onto the fixed canonical host, never the raw query string
    (CodeQL py/url-redirection).
    """
    carried = _carried_redirect_params(keep_save=True)
    return redirect(_canonical_url("public.show_public_recipe", slug=slug, **carried), code=301)


@public_bp.route("/api/recipes/public/<slug>", methods=["GET"])
def public_recipe_json(slug):
    """JSON payload of a published recipe for the SPA's ?save=<slug> flow.

    Returns 404 when no recipe matches the slug or the recipe is not public,
    410 when the slug belonged to a deleted recipe (KAN-288).
    """
    recipe = Recipe.query.filter(Recipe.slug == slug, Recipe.is_public.is_(True)).first()
    if recipe is None:
        return _missing_recipe_response(
            slug, "public.public_recipe_json", canonical=False, json=True
        )
    payload = _save_recipe_payload(recipe, _recipe_image_url(recipe))
    # KAN-295: the SPA's visible breadcrumb. Not part of the save payload
    # (the SPA mapper allowlists the fields it copies into a saved recipe).
    payload["breadcrumbs"] = _recipe_breadcrumbs(recipe, payload["tags"], _catalog_tag_rows())
    return jsonify(payload)


@public_bp.route("/api/public/newest-recipes", methods=["GET"])
def newest_public_recipes_json():
    """The most recently published public recipes, for the home page row (KAN-348).

    Ordered by ``first_published_at``, not ``created_at``: a recipe created in
    July and published today is new to a reader and to a crawler. Rows without
    a publication date or a slug are left out. Visibility is read on every
    call, so an unpublished recipe drops out as soon as the caller's own cache
    expires.
    """
    limit = request.args.get("limit", type=int)
    if limit is None:
        limit = NEWEST_RECIPES_DEFAULT
    limit = max(1, min(limit, NEWEST_RECIPES_MAX))
    rows = (
        Recipe.query.with_entities(Recipe.slug, Recipe.name, Recipe.first_published_at)
        .filter(
            Recipe.is_public.is_(True),
            Recipe.slug.isnot(None),
            Recipe.first_published_at.isnot(None),
        )
        .order_by(Recipe.first_published_at.desc(), Recipe.id.desc())
        .limit(limit)
        .all()
    )
    return jsonify(
        {
            "recipes": [
                {
                    "slug": row.slug,
                    "name": row.name,
                    "first_published_at": row.first_published_at.isoformat(),
                }
                for row in rows
            ]
        }
    )


@public_bp.route("/browse", methods=["GET"])
def browse_public_recipes():
    """Paginated SSR index of published recipes.

    Uses ``joinedload`` on ``Recipe.user`` so the template can show author
    names without triggering an extra SELECT per row (N+1).

    KAN-296: ``?page=N`` is self-canonical. ``?page=1`` 301s to the bare
    ``/browse`` (older Previous links emitted it); a page past the last, or a
    ``page`` that is not a positive integer, is a 404.

    KAN-298: ``?sort=`` (``BROWSE_SORTS``) orders the listing and ``?tag=``
    narrows it to recipes carrying that tag. Any URL with either key is a view
    of ``/browse``, not a page of its own: it declares ``rel=canonical`` to the
    bare ``/browse`` on every page, so filtered views are never indexed as
    duplicates. The indexable tag pages are the curated hubs at
    ``/browse/tag/<slug>``. An unknown sort falls back to the default order.
    """
    page = _requested_page()
    if page is None:
        abort(404)
    view = _browse_view()
    if page == 1 and "page" in request.args:
        carried = _carried_redirect_params(keep_save=False)
        return redirect(_canonical_url("public.browse_public_recipes", **view, **carried), code=301)

    # slug IS NOT NULL, like the sitemap: a slugless public row (legacy data;
    # publishing always mints a slug now) has no /r/ URL, and url_for on it
    # would fail the whole page.
    base_query = Recipe.query.filter(Recipe.is_public.is_(True), Recipe.slug.isnot(None)).options(
        joinedload(Recipe.user)
    )
    catalog = _catalog_tag_rows()
    tag = view.get("tag")
    if tag:
        tagged = [row.id for row in catalog if _carries_tag(_row_tags(row), tag)]
        base_query = base_query.filter(Recipe.id.in_(tagged))

    total = base_query.with_entities(Recipe.id).count()
    total_pages = max(1, ceil(total / BROWSE_PAGE_SIZE))
    if page > total_pages:
        abort(404)

    # ``Recipe.id`` breaks ties so equal names or timestamps page consistently.
    sort = view.get("sort", "newest")
    if sort == "name":
        ordering = (func.lower(Recipe.name).asc(), Recipe.id.asc())
    elif sort == "oldest":
        ordering = (Recipe.created_at.asc(), Recipe.id.asc())
    else:
        ordering = (Recipe.created_at.desc(), Recipe.id.desc())
    recipes = (
        base_query.order_by(*ordering)
        .limit(BROWSE_PAGE_SIZE)
        .offset((page - 1) * BROWSE_PAGE_SIZE)
        .all()
    )
    if tag:
        # As on the hubs: a recipe retagged between the catalog snapshot and
        # this load is no longer in the view, so it is not rendered.
        recipes = [r for r in recipes if _carries_tag(_recipe_tags(r.data or {}), tag)]

    # KAN-298: a ?sort / ?tag URL is a view of /browse and canonicalises to it.
    filtered = "sort" in request.args or "tag" in request.args
    canonical_url = _canonical_url(
        "public.browse_public_recipes", **({} if filtered else _page_values(page))
    )
    # KAN-273: the title and description say what the page is, with the live
    # count; the social card gets the newest photo on the page instead of none.
    recipe_noun = "Recipe" if total == 1 else "Recipes"
    if page > 1:
        page_title = f"Vegan Recipes, Page {page} of {total_pages}{SITE_TITLE_SUFFIX}"
    else:
        page_title = f"Browse {total} Vegan {recipe_noun}{SITE_TITLE_SUFFIX}"
    description = (
        f"Browse {total} AI-generated vegan {recipe_noun.lower()} with ingredients and method. "
        "Photos are included when available. No ads, no life story. Save any recipe "
        "to your cookbook."
    )
    snippet_description = _meta_description(description)
    og_owner = next((r for r in recipes if _serves_own_image_bytes(r)), None)
    breadcrumbs = _breadcrumbs()
    # On paginated browse pages, the current crumb is the current canonical
    # page, not page 1. Keep recipe-page breadcrumbs pointing to /browse.
    breadcrumbs[-1]["url"] = canonical_url
    hubs = _linkable_hubs(_hub_counts(catalog))

    return render_template(
        "public/browse.html",
        page_title=page_title,
        og_image_url=_versioned_image_url(og_owner) if og_owner else None,
        # The og:image is a specific dish photo (og_owner), not a shot of the
        # browse page — so its alt names that dish, otherwise social cards and
        # screen readers announce "Browse N Vegan Recipes" for a plate of food.
        og_image_alt=og_owner.name if og_owner else None,
        breadcrumb_json_ld=_breadcrumb_json_ld(breadcrumbs),
        collection_json_ld=_collection_json_ld(
            page_title, snippet_description, canonical_url, recipes
        ),
        recipes=recipes,
        hubs=[{"title": hub.title, "url": _hub_url(hub)} for hub in hubs],
        card_images={recipe.id: _card_image(recipe) for recipe in recipes},
        card_image_sizes=CARD_IMAGE_SIZES,
        pagination=_pagination(
            page,
            total_pages,
            lambda number: url_for("public.browse_public_recipes", **view, **_page_values(number)),
        ),
        sort=sort,
        sorts=BROWSE_SORT_LABELS,
        tag=tag or "",
        total=total,
        canonical_url=canonical_url,
        # /browse is subject to the same 155-char result-snippet cap that
        # ``_meta_description`` enforces on /r/<slug> — the boilerplate is
        # already 158 chars at today's 96 recipes and lengthens as ``total``
        # grows. Reuse the bounded copy for metadata and CollectionPage JSON-LD.
        description=snippet_description,
    )


@public_bp.route("/browse/tag/<hub_slug>", methods=["GET"])
def show_tag_hub(hub_slug):
    """A curated category hub: intro copy + every public recipe in it (KAN-274).

    Only allow-listed hubs exist (``services.tag_hubs``); anything else is a
    404, so arbitrary tag filters never become indexable pages. A hub below
    ``MIN_INDEXABLE_RECIPES`` still renders but is ``noindex``.

    KAN-296: paginated like ``/browse``, ``HUB_PAGE_SIZE`` per page, with the
    same ``?page=`` rules: self-canonical, ``?page=1`` 301s to the bare hub
    URL, past-the-last or malformed is a 404.
    """
    hub = HUBS_BY_SLUG.get(hub_slug)
    if hub is None:
        abort(404)
    page = _requested_page()
    if page is None:
        abort(404)
    if page == 1 and "page" in request.args:
        carried = _carried_redirect_params(keep_save=False)
        return redirect(
            _canonical_url("public.show_tag_hub", hub_slug=hub.slug, **carried), code=301
        )

    catalog = _catalog_tag_rows()
    hub_members = _hub_members(catalog)
    counts = _counts_from_members(hub_members)
    # ``row.id`` breaks ties so recipes created in the same second stay in a
    # stable order across requests: ``_catalog_tag_rows`` has no ``ORDER BY``,
    # so heap order alone would let the top cards and CollectionPage positions
    # drift between cache misses.
    members = sorted(
        hub_members[hub.slug],
        key=lambda row: (row.created_at or datetime.min, row.id),
        reverse=True,
    )
    total_pages = max(1, ceil(len(members) / HUB_PAGE_SIZE))
    if page > total_pages:
        abort(404)
    recipes = _hydrate_hub_page(hub, members, page)

    canonical_url = _canonical_url("public.show_tag_hub", hub_slug=hub.slug, **_page_values(page))
    page_title = _page_title(
        f"{hub.title}, Page {page} of {total_pages}" if page > 1 else hub.title
    )
    description = _meta_description(hub.intro)
    breadcrumbs = _breadcrumbs(hub=hub)
    # As on /browse: a later page's own crumb is its canonical, not page 1.
    breadcrumbs[-1]["url"] = canonical_url
    og_owner = next((r for r in recipes if _serves_own_image_bytes(r)), None)

    # Hydration can drop a concurrently unpublished, slug-cleared, or retagged
    # member; indexability must reflect what this response actually renders.
    # A later page exists only because the hub overflows page 1, so a short
    # last page is not a thin hub and stays indexable — unless hydration left
    # it with nothing to render.
    indexable = (page > 1 and bool(recipes)) or len(recipes) >= MIN_INDEXABLE_RECIPES
    body = render_template(
        "public/tag_hub.html",
        hub=hub,
        page_title=page_title,
        description=description,
        canonical_url=canonical_url,
        indexable=indexable,
        recipes=recipes,
        card_images={recipe.id: _card_image(recipe) for recipe in recipes},
        card_image_sizes=CARD_IMAGE_SIZES,
        og_image_url=_versioned_image_url(og_owner) if og_owner else None,
        og_image_alt=og_owner.name if og_owner else None,
        breadcrumbs=breadcrumbs,
        breadcrumb_json_ld=_breadcrumb_json_ld(breadcrumbs),
        collection_json_ld=_collection_json_ld(page_title, hub.intro, canonical_url, recipes),
        pagination=_pagination(
            page,
            total_pages,
            lambda number: url_for(
                "public.show_tag_hub", hub_slug=hub.slug, **_page_values(number)
            ),
        ),
        other_hubs=[
            {"title": other.title, "url": _hub_url(other)}
            for other in _linkable_hubs(counts)
            if other.slug != hub.slug
        ],
    )
    response = Response(body)
    if not indexable:
        # The Express security middleware otherwise supplies a production
        # indexable default. Preserve the template's thin-page decision at the
        # HTTP layer so every crawler receives the same directive.
        response.headers["X-Robots-Tag"] = "noindex, follow"
    return response


@public_bp.route("/browse/tag/<hub_slug>/", methods=["GET"])
def redirect_trailing_slash_hub(hub_slug):
    """``/browse/tag/<slug>/`` → 301 to the canonical hub URL.

    Unknown slugs 404 directly rather than 301→404, so search consoles don't
    log a redirect chain and crawlers don't waste a hop on a stale link.

    Carries the ``utm_*`` campaign params so attribution survives the 301.
    Not ``save``: that handoff is only read on recipe URLs.

    KAN-296: a valid ``?page=N`` survives the 301 (page 1 as the bare URL); a
    malformed one 404s here, as it would on the target, rather than 301→404.
    """
    if hub_slug not in HUBS_BY_SLUG:
        abort(404)
    page = _requested_page()
    if page is None:
        abort(404)
    carried = _carried_redirect_params(keep_save=False)
    return redirect(
        _canonical_url("public.show_tag_hub", hub_slug=hub_slug, **_page_values(page), **carried),
        code=301,
    )


@public_bp.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    """Return an XML sitemap of the public recipe surface."""
    # One catalog scan feeds both the per-recipe entries and the hub-membership
    # lookup below. Tags come along on the same row (no full ``data`` blob),
    # so we avoid a second full-catalog SELECT for the hub loop.
    recipes = (
        Recipe.query.with_entities(
            Recipe.slug,
            Recipe.updated_at,
            Recipe.created_at,
            Recipe.data["tags"].label("tags"),
        )
        .filter(Recipe.is_public.is_(True), Recipe.slug.isnot(None))
        .order_by(Recipe.updated_at.desc(), Recipe.created_at.desc())
        .all()
    )
    xml_body = _build_sitemap_xml(recipes, _sitemap_hub_entries(recipes))
    return Response(xml_body, mimetype="application/xml")


__all__ = [
    "BROWSE_PAGE_SIZE",
    "BROWSE_SORT_LABELS",
    "BROWSE_SORTS",
    "CARD_IMAGE_SIZES",
    "CARD_IMAGE_WIDTHS",
    "DEFAULT_RECIPE_DESCRIPTION",
    "HERO_IMAGE_SIZES",
    "HERO_IMAGE_WIDTHS",
    "HUB_PAGE_SIZE",
    "MAX_PIN_DESCRIPTION_LENGTH",
    "MAX_PIN_TAIL_LENGTH",
    "MAX_TAG_FILTER_LENGTH",
    "NEWEST_RECIPES_DEFAULT",
    "NEWEST_RECIPES_MAX",
    "PAGINATION_WINDOW",
    "PIN_DESCRIPTION_TAGS",
    "RECIPE_CATEGORY_BY_TAG",
    "RECIPE_CUISINE_BY_TAG",
    "RELATED_RECIPE_COUNT",
    "SITE_SOCIAL_PROFILES",
    "STEP_NAME_LIMIT",
    "_PAGE_PARAM",
    "_PIN_ABBREVIATIONS",
    "_PIN_INITIALISM",
    "_SENTENCE_END",
    "_absolute_url",
    "_adjacent_recipes",
    "_breadcrumb_json_ld",
    "_breadcrumbs",
    "_browse_view",
    "_build_sitemap_xml",
    "_canonical_url",
    "_card_image",
    "_carried_redirect_params",
    "_carries_tag",
    "_catalog_tag_rows",
    "_clean_json",
    "_collection_json_ld",
    "_counts_from_members",
    "_first_pin_sentence",
    "_footer_hubs",
    "_format_ingredient",
    "_hub_counts",
    "_hub_members",
    "_hub_url",
    "_hydrate_hub_page",
    "_image_variants",
    "_image_version_source",
    "_image_version_token",
    "_inject_footer_hubs",
    "_linkable_hubs",
    "_meta_description",
    "_minutes_to_iso_duration",
    "_missing_recipe_response",
    "_normalise_tag",
    "_own_image_url",
    "_page_numbers",
    "_page_title",
    "_page_values",
    "_pagination",
    "_pin_description",
    "_pin_image_url",
    "_pinterest_share_url",
    "_public_base_url",
    "_recipe_breadcrumbs",
    "_recipe_description",
    "_recipe_hubs",
    "_recipe_image",
    "_recipe_image_url",
    "_recipe_ingredient_groups",
    "_recipe_instructions",
    "_recipe_json_ld",
    "_recipe_keywords",
    "_recipe_tags",
    "_related_recipes",
    "_rendered_image",
    "_rendered_image_url",
    "_requested_page",
    "_resolve_source_for_image",
    "_row_tags",
    "_safe_minutes",
    "_save_recipe_payload",
    "_serves_own_image_bytes",
    "_site_organization",
    "_sitemap_hub_entries",
    "_step_name",
    "_tag_labels",
    "_trim_to_words",
    "_versioned_image_url",
    "browse_public_recipes",
    "newest_public_recipes_json",
    "public_bp",
    "public_recipe_json",
    "redirect_trailing_slash_hub",
    "redirect_trailing_slash_recipe",
    "show_public_recipe",
    "show_tag_hub",
    "sitemap_xml",
]
