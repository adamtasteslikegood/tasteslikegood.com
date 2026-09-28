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

import hashlib
import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime
from math import ceil
from typing import Any
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, SubElement, tostring

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
from sqlalchemy.orm import joinedload

from extensions import db
from models import Recipe, RetiredSlug
from services.image_variants import VARIANT_WIDTHS
from services.tag_hubs import (
    HUBS_BY_SLUG,
    MIN_INDEXABLE_RECIPES,
    TAG_HUBS,
    TagHub,
    hubs_for_tags,
)

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


def _serves_own_image_bytes(recipe: Recipe) -> bool:
    """True when ``/api/recipes/<id>/image`` has bytes for this recipe.

    The one signal that separates "our endpoint serves this" from "this is an
    external stock image URL" — which is what decides whether the URL is ours
    to version (see ``_rendered_image_url``).

    Restricted to non-empty strings so this gate agrees with
    ``_image_version_token`` (which hashes ``str`` sources only). A legacy row
    whose ``ai_image_data`` decoded to a non-string truthy value would
    otherwise pass this gate but produce a versionless URL, silently
    reintroducing the KAN-195 stale-image defect on the og:image and hero.
    """
    data = recipe.data or {}
    gcs = data.get("ai_image_gcs")
    payload = data.get("ai_image_data")
    return (isinstance(gcs, str) and bool(gcs)) or (isinstance(payload, str) and bool(payload))


def _own_image_url(recipe: Recipe) -> str | None:
    """Image URL from the recipe's own data blob — no source fallback.

    Derives the page media from the signal the image endpoint actually serves
    from (real GCS/base64 bytes), else an external stock image — never from
    the unverified ``ai_image_url`` (cookbook #3164).
    """
    if _serves_own_image_bytes(recipe):
        return _canonical_url("generation_api.serve_recipe_image", recipe_id=recipe.id)
    return _absolute_url((recipe.data or {}).get("stock_image_url"))


def _resolve_source_for_image(recipe: Recipe) -> Recipe | None:
    """Look up the source recipe for image fallback.

    Returns ``None`` when the recipe is not a saved copy, when the source is
    no longer public, or when no source can be identified with confidence.
    Both paths require ``is_public`` — without that gate a private source's
    image URL would leak into a public page's ``<meta og:image>`` and
    Pinterest pin.

    Prefers the immutable ``source_recipe_id`` FK; falls back to
    ``source_slug`` for legacy copies whose FK was never backfilled.

    The slug arm carries a causality guard.  ``source_recipe_id`` is
    ``ON DELETE SET NULL`` (``models/recipe.py``), so a copy whose source was
    deleted is indistinguishable by column state from a legacy copy the
    a3c9e1f4b7d2 backfill could not resolve — and slugs are reusable once
    freed.  Without the guard, an unrelated recipe that later takes the freed
    slug is silently attributed as the source, putting its image on the
    copy's hero, og:image, JSON-LD and Pinterest pin at once.

    A source must predate the copy made from it, so a slug match that was
    created *after* the copy cannot be that copy's source.  This resolves the
    ambiguity from data already on the row: a persisted "was it ever
    resolved" flag could not, because at migration time the FK-cleared and
    never-resolved rows are already indistinguishable — the very ambiguity
    such a flag would exist to remove — so it could only protect rows created
    afterwards and would misclassify every historical delete-cleared row as
    fallback-eligible.

    The guard fails safe: refusing a match costs a missing image, never a
    wrong one.  One narrow case survives — an older recipe *renamed* into the
    freed slug still predates the copy (KAN-251).
    """
    if recipe.source_recipe_id:
        source = db.session.get(Recipe, recipe.source_recipe_id)
        if source is not None and source.is_public:
            return source
        return None
    if recipe.source_slug:
        source_by_slug: Recipe | None = Recipe.query.filter(
            Recipe.slug == recipe.source_slug, Recipe.is_public.is_(True)
        ).first()
        if source_by_slug is None:
            return None
        # Strict <: same-timestamp rows cannot be ordered, so they fail safe.
        if (
            recipe.created_at is None
            or source_by_slug.created_at is None
            or not source_by_slug.created_at < recipe.created_at
        ):
            return None
        return source_by_slug
    return None


def _recipe_image_url(recipe: Recipe) -> str | None:
    """URL of an image the site can actually serve, or ``None`` to omit it.

    KAN-215: saved copies (recipes with ``source_recipe_id`` or
    ``source_slug``) often lack their own image data because the SPA save
    flow does not propagate ``ai_image_gcs`` / ``ai_image_data``.  When the
    recipe's own data has no serveable image, fall back to the source
    recipe's image — resolving its *current* state so the pointer is always
    live (no stale GCS URI if the source regenerates).

    The same gate feeds the Pinterest share button, so the pin media and the
    page media can never disagree.

    A copy's own image always wins.  That is only sound because nothing
    copies the source's ``stock_image_url`` onto the copy at save time — if
    anything did, the inherited URL would be indistinguishable from an image
    the copy genuinely owns, would be returned here before the source was
    ever resolved, and would pin the copy to the stock image permanently,
    including after the source gained an AI image.  Resolve live instead.
    """
    return _recipe_image(recipe)[0]


def _recipe_image(recipe: Recipe) -> tuple[str | None, Recipe | None]:
    """``(url, the recipe whose bytes that url serves)``.

    The owner is the half ``_recipe_image_url`` used to throw away, and
    KAN-195 needs it: for a saved copy the bytes belong to the SOURCE, so the
    version marker has to be read off the source's row, not the copy's.
    Returned alongside the URL rather than resolved a second time — the
    saved-copy source lookup is a DB round trip with a causality guard, and
    the render path already goes out of its way not to repeat it.
    """
    url = _own_image_url(recipe)
    if url is not None:
        return url, recipe
    # Saved copy with no image of its own — resolve from source.
    source = _resolve_source_for_image(recipe)
    if source is not None:
        return _own_image_url(source), source
    return None, None


def _image_version_source(owner: Recipe) -> str | None:
    """Canonical identity of every stored source the image endpoint can serve."""
    data = owner.data or {}
    gcs = data.get("ai_image_gcs")
    payload = data.get("ai_image_data")
    sources = [source for source in (gcs, payload) if isinstance(source, str) and source]
    return "|".join(sources) if sources else None


def _image_version_token(owner: Recipe) -> str | None:
    """A short marker derived from every stored source the endpoint can serve.

    The generic recipe PUT can retain image-generation metadata while changing
    ``ai_image_gcs`` or legacy ``ai_image_data``. The full-size loader prefers
    GCS but falls back to the legacy payload when that read fails, so both
    non-empty string sources must participate when both are stored. Otherwise a
    fallback-only PUT would keep the same public URL and browsers/CDNs could
    serve stale fallback bytes for the full one-day ``max-age``.

    Hashed and truncated rather than emitted raw: storage identifiers and image
    payloads are internal state and do not belong in a public URL.

    Uses the same per-field ``isinstance(..., str)`` gate as
    ``_serves_own_image_bytes``, preserving the invariant that every image the
    endpoint can serve receives a versioned public URL.
    """
    source = _image_version_source(owner)
    if source is None:
        return None
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def _rendered_image_url(recipe: Recipe) -> str | None:
    """The image URL for the PUBLIC PAGE, versioned (KAN-195).

    ``/api/recipes/<id>/image`` is served with ``Cache-Control: public,
    max-age=86400`` for a public recipe, and its URL never changes. So when a
    user regenerated a photo, every browser and CDN that had already fetched
    the old bytes kept serving them for up to 24 hours — the new photo was
    live on the server (the worker invalidates the Valkey entry, see
    ``worker_api_bp``) and invisible on ``/r/<slug>``.

    A ``?v=<marker>`` query param makes the regenerated image a different
    cache key, so it is fetched immediately, while the long ``max-age`` keeps
    doing its job for the bytes that did not change. The SPA already solves
    the same problem with its own display-only ``_t=`` marker
    (``RecipeStateService``, KAN-243); this is the server-rendered half.

    Two things this deliberately does NOT do:

    * It does not touch the ``Cache-Control`` header. Shortening it would trade
      a permanent CDN/browser cache for a correctness fix that a new URL
      already delivers.
    * It is not used by ``_save_recipe_payload``. That payload becomes the
      SPA's ``ai_image_url``, which is persisted, and a display-only marker in
      persisted state is exactly the trap KAN-243 documents (a later full save
      writes it back as the canonical URL). Versioning is a render concern and
      stays on the render path.

    An external stock image is left alone: it is not served by us, appending a
    param cannot help, and it could break a signed URL.
    """
    return _rendered_image(recipe)[0]


def _rendered_image(recipe: Recipe) -> tuple[str | None, dict[str, str] | None]:
    """``(versioned full-size URL, sized variants)`` for the recipe page.

    One ``_recipe_image`` call feeds both, so a saved copy's source lookup is
    not repeated. Variants are ``None`` when the image is not served by us.
    Their ``src`` and ``srcset`` entries are host-relative; ``pin`` is
    deliberately absolute because Pinterest fetches it from its own servers.
    """
    url, owner = _recipe_image(recipe)
    if url is None or owner is None or not _serves_own_image_bytes(owner):
        return url, None
    variants = _image_variants(owner, HERO_IMAGE_WIDTHS)
    variants["pin"] = _pin_image_url(owner)
    return _versioned_image_url(owner), variants


def _pin_image_url(owner: Recipe) -> str:
    """Absolute URL of the 2:3 Pinterest pin JPEG (KAN-284), versioned like the hero.

    Absolute because Pinterest fetches it from its own servers. Same gate and
    ``?v=`` marker as ``_versioned_image_url``: a pin of a replaced photo must
    not be the old bytes. Fail loud on a missing token for the same reason
    ``_versioned_image_url`` does (KAN-195): a versionless URL is permanently
    CDN-cached.
    """
    token = _image_version_token(owner)
    if token is None:
        raise RuntimeError(
            "_pin_image_url called for owner without an image-version token; "
            "callers must gate on _serves_own_image_bytes."
        )
    return _canonical_url(
        "generation_api.serve_recipe_image",
        recipe_id=owner.id,
        pin=1,
        v=token,
    )


def _versioned_image_url(owner: Recipe) -> str:
    """Absolute full-size URL of an image ``owner`` serves itself, with ``?v=``.

    Callers gate on ``_serves_own_image_bytes(owner)`` first, and
    ``_image_version_token`` uses the same per-field ``isinstance`` check, so
    ``token`` is never ``None`` here. Fail loud rather than silently emitting a
    versionless (permanently CDN-cached) URL — that path is the KAN-195 defect.
    """
    token = _image_version_token(owner)
    if token is None:
        raise RuntimeError(
            "_versioned_image_url called for owner without an image-version token; "
            "callers must gate on _serves_own_image_bytes."
        )
    return _canonical_url("generation_api.serve_recipe_image", recipe_id=owner.id, v=token)


# Sized WebP variants (KAN-271). ``sizes`` mirror recipe-site.css: the hero
# spans ``.public-main`` (min(1200px, 100vw - 3rem)); browse cards are a
# 3 / 2 / 1-column grid at >900px / >768px / phones.
HERO_IMAGE_WIDTHS = VARIANT_WIDTHS
HERO_IMAGE_SIZES = "(max-width: 1248px) 100vw, 1200px"
CARD_IMAGE_WIDTHS = (400, 800)
CARD_IMAGE_SIZES = "(max-width: 768px) 100vw, (max-width: 900px) 50vw, 400px"


def _image_variants(owner: Recipe, widths: tuple[int, ...]) -> dict[str, str]:
    """``src`` (smallest width) and ``srcset`` for an image ``owner`` serves itself.

    Host-relative, like the browse cards' URLs have always been, and carrying
    the same ``?v=`` marker as the full-size URL, so a replaced image gets a new
    URL instead of waiting out the image route's one-day ``max-age``.
    """
    token = _image_version_token(owner)

    def at(width: int) -> str:
        params: dict[str, Any] = {"recipe_id": owner.id, "w": width}
        if token is not None:
            params["v"] = token
        return url_for("generation_api.serve_recipe_image", **params)

    return {
        "src": at(widths[0]),
        "srcset": ", ".join(f"{at(width)} {width}w" for width in widths),
    }


def _card_image(recipe: Recipe) -> dict[str, str] | None:
    """Image for a browse card: own bytes as sized variants, else stock, else none.

    Deliberately no saved-copy source fallback — that is a DB lookup per card,
    and /browse is asserted N+1-free. Same gate the template always applied.
    """
    if _serves_own_image_bytes(recipe):
        return _image_variants(recipe, CARD_IMAGE_WIDTHS)
    stock = (recipe.data or {}).get("stock_image_url")
    return {"src": stock} if stock else None


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
    raw_instructions = data.get("instructions")
    if not isinstance(raw_instructions, list):
        return []
    instructions: list[str] = []
    for step in raw_instructions:
        if isinstance(step, dict):
            text = str(step.get("description", "") or "").strip()
        elif isinstance(step, str):
            text = str(step).strip()
        else:
            continue
        if text:
            instructions.append(text)
    return instructions


def _recipe_tags(data: dict[str, Any]) -> list[str]:
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list):
        return []
    return [tag.strip() for tag in raw_tags if isinstance(tag, str) and tag.strip()]


DEFAULT_RECIPE_DESCRIPTION = "A vegan recipe from TastesLikeGood."


def _recipe_description(data: dict[str, Any]) -> str:
    """Persisted recipe JSON is legacy-tolerant; metadata always needs text."""
    value = data.get("description")
    return value if isinstance(value, str) and value.strip() else DEFAULT_RECIPE_DESCRIPTION


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
        "description": _recipe_description(data),
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
        "keywords": ", ".join(_recipe_tags(data)) or None,
        "recipeCategory": _tag_labels(data, RECIPE_CATEGORY_BY_TAG) or None,
        "recipeCuisine": _tag_labels(data, RECIPE_CUISINE_BY_TAG) or None,
        "suitableForDiet": "https://schema.org/VeganDiet",
    }
    cleaned: dict[str, Any] = _clean_json(json_ld)
    return cleaned


# ── Internal linking + snippet hygiene (KAN-273) ─────────────────────────────

# Course and cuisine from the tags the generator already writes. Every recipe
# here is vegan, so "Vegan" was never a category: it is ``suitableForDiet``.
# Keys are lower-cased tags; a recipe gets every distinct label its tags map to.
RECIPE_CATEGORY_BY_TAG: dict[str, str] = {
    "breakfast": "Breakfast",
    "brunch": "Breakfast",
    "lunch": "Lunch",
    "dinner": "Dinner",
    "main": "Main course",
    "main course": "Main course",
    "main dish": "Main course",
    "entree": "Main course",
    "dessert": "Dessert",
    "desserts": "Dessert",
    "appetizer": "Appetizer",
    "appetizers": "Appetizer",
    "starter": "Appetizer",
    "snack": "Snack",
    "snacks": "Snack",
    "side": "Side dish",
    "side dish": "Side dish",
    "soup": "Soup",
    "salad": "Salad",
    "sandwich": "Sandwich",
    "sandwiches": "Sandwich",
    "drink": "Drink",
    "beverage": "Drink",
    "smoothie": "Drink",
}

RECIPE_CUISINE_BY_TAG: dict[str, str] = {
    "american": "American",
    "southern": "Southern",
    "cajun": "Cajun",
    "tex-mex": "Tex-Mex",
    "mexican": "Mexican",
    "italian": "Italian",
    "french": "French",
    "spanish": "Spanish",
    "greek": "Greek",
    "mediterranean": "Mediterranean",
    "middle eastern": "Middle Eastern",
    "british": "British",
    "english": "British",
    "indian": "Indian",
    "thai": "Thai",
    "vietnamese": "Vietnamese",
    "korean": "Korean",
    "japanese": "Japanese",
    "chinese": "Chinese",
    "caribbean": "Caribbean",
    "ethiopian": "Ethiopian",
}


def _tag_labels(data: dict[str, Any], mapping: Mapping[str, str]) -> list[str]:
    labels: list[str] = []
    for tag in _recipe_tags(data):
        label = mapping.get(tag.lower())
        if label and label not in labels:
            labels.append(label)
    return labels


SITE_TITLE_SUFFIX = " · TastesLikeGood"
MAX_TITLE_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 155
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _page_title(name: str) -> str:
    """``name · TastesLikeGood`` when that fits a result line, else just ``name``.

    Results truncate near 60 characters, and 25 of 96 recipe titles lost their
    ending to the unconditional suffix (SEO audit 2026-09-13, O3).
    """
    titled = f"{name}{SITE_TITLE_SUFFIX}"
    return titled if len(titled) <= MAX_TITLE_LENGTH else name


def _meta_description(text: str) -> str:
    """``text`` cut to fit a result snippet: at the last sentence end, else a word.

    Recipe pages and Recipe JSON-LD keep their full descriptions. Browse
    reuses the bounded copy for both metadata and CollectionPage JSON-LD.
    """
    text = " ".join(text.split())
    if len(text) <= MAX_DESCRIPTION_LENGTH:
        return text
    head = text[: MAX_DESCRIPTION_LENGTH + 1]
    ends = [m.end() for m in _SENTENCE_END.finditer(head) if m.end() <= MAX_DESCRIPTION_LENGTH]
    if ends and ends[-1] >= 60:
        return head[: ends[-1]]
    cut = head[: MAX_DESCRIPTION_LENGTH - 1].rsplit(" ", 1)[0].rstrip(" ,;:-–—")
    return f"{cut}…"


RELATED_RECIPE_COUNT = 6


def _catalog_tag_rows() -> list[Any]:
    """``(id, created_at, updated_at, tags)`` for every public recipe.

    The one catalog scan the related-recipes block and the tag hubs share. It
    reads ``data -> 'tags'`` only — never the full ``data`` blob, which can still
    carry legacy base64 images.
    """
    rows: list[Any] = (
        Recipe.query.with_entities(
            Recipe.id,
            Recipe.created_at,
            Recipe.updated_at,
            Recipe.data["tags"].label("tags"),
        )
        .filter(Recipe.is_public.is_(True), Recipe.slug.isnot(None))
        .all()
    )
    return rows


def _row_tags(row: Any) -> list[Any]:
    return row.tags if isinstance(row.tags, list) else []


def _related_recipes(recipe: Recipe, catalog: list[Any]) -> list[Recipe]:
    """Public recipes that share the most tags with ``recipe``, newest first on ties.

    Recipe pages linked to no other recipe, so link equity stopped at every page
    (SEO audit O2). Scoring uses the lightweight catalog rows, then loads the
    chosen few in full for their cards.
    """
    own_tags = {tag.lower() for tag in _recipe_tags(recipe.data or {})}
    rows = [row for row in catalog if row.id != recipe.id]

    def rank(row: Any) -> tuple[int, datetime]:
        tags = _row_tags(row)
        shared = own_tags & {tag.strip().lower() for tag in tags if isinstance(tag, str)}
        return len(shared), row.created_at or datetime.min

    chosen = [row.id for row in sorted(rows, key=rank, reverse=True)[:RELATED_RECIPE_COUNT]]
    if not chosen:
        return []
    # Mirror the filters from the scoring query: under READ COMMITTED, a recipe
    # unpublished (or slug-nulled) between the two queries would otherwise be
    # linked from a public page and 404 on click.
    by_id = {
        related.id: related
        for related in Recipe.query.filter(
            Recipe.id.in_(chosen), Recipe.is_public.is_(True), Recipe.slug.isnot(None)
        ).all()
    }
    return [by_id[recipe_id] for recipe_id in chosen if recipe_id in by_id]


def _hub_members(catalog: list[Any]) -> dict[str, list[Any]]:
    """Map each curated hub to its catalog rows in one catalog pass."""
    members: dict[str, list[Any]] = {hub.slug: [] for hub in TAG_HUBS}
    for row in catalog:
        for hub in hubs_for_tags(_row_tags(row)):
            members[hub.slug].append(row)
    return members


def _counts_from_members(members: dict[str, list[Any]]) -> dict[str, int]:
    return {slug: len(rows) for slug, rows in members.items()}


def _hub_counts(catalog: list[Any]) -> dict[str, int]:
    return _counts_from_members(_hub_members(catalog))


def _linkable_hubs(counts: dict[str, int]) -> list[TagHub]:
    """Hubs big enough to index, and so to link, list and put in the sitemap (KAN-274)."""
    return [hub for hub in TAG_HUBS if counts[hub.slug] >= MIN_INDEXABLE_RECIPES]


def _hub_url(hub: TagHub) -> str:
    return _canonical_url("public.show_tag_hub", hub_slug=hub.slug)


def _breadcrumbs(recipe: Recipe | None = None, hub: TagHub | None = None) -> list[dict[str, str]]:
    """Home → Browse [→ hub] [→ recipe]: the visible trail and its BreadcrumbList."""
    crumbs = [
        {"name": "Home", "url": f"{_public_base_url()}/"},
        {"name": "Browse", "url": _canonical_url("public.browse_public_recipes")},
    ]
    if hub is not None:
        crumbs.append({"name": hub.title, "url": _hub_url(hub)})
    if recipe is not None:
        crumbs.append(
            {
                "name": recipe.name,
                "url": _canonical_url("public.show_public_recipe", slug=recipe.slug),
            }
        )
    return crumbs


def _breadcrumb_json_ld(crumbs: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": index + 1,
                "name": crumb["name"],
                "item": crumb["url"],
            }
            for index, crumb in enumerate(crumbs)
        ],
    }


def _collection_json_ld(
    name: str, description: str, canonical_url: str, recipes: list[Recipe]
) -> dict[str, Any]:
    """``CollectionPage`` whose ``ItemList`` is the recipes on this page, in order."""
    return {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": name,
        "description": description,
        "url": canonical_url,
        "mainEntity": {
            "@type": "ItemList",
            "numberOfItems": len(recipes),
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": index + 1,
                    "url": _canonical_url("public.show_public_recipe", slug=recipe.slug),
                    "name": recipe.name,
                }
                for index, recipe in enumerate(recipes)
            ],
        },
    }


MAX_PIN_DESCRIPTION_LENGTH = 500
PIN_DESCRIPTION_TAGS = 3
# The "Vegan recipe: tags." tail's share of the 500. Recipe.name is String(200),
# so name + separators + tail always fit and the tail is never cut. Tags are
# unbounded strings (recipe_schema.json), so one that does not fit is skipped.
MAX_PIN_TAIL_LENGTH = 200
# Common title/word abbreviations must not terminate the pin's first sentence.
# Multi-initial abbreviations such as "e.g." and "U.S." are recognized separately.
_PIN_ABBREVIATIONS = frozenset({"dr.", "jr.", "mr.", "mrs.", "ms.", "prof.", "sr.", "st.", "vs."})
_PIN_INITIALISM = re.compile(r"(?:[a-z]\.){2,}")


def _trim_to_words(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters at a word boundary, with an ellipsis.

    Sentence terminators are stripped from the cut word so punctuation does not
    double up before the ellipsis (``"Done."`` → ``"Done…"``).
    """
    if len(text) <= limit:
        return text
    prefix = text[: limit - 1]
    cut = prefix.rsplit(" ", 1)[0].rstrip(" .,;:!?-–—") if " " in prefix else ""
    return f"{cut}…" if cut else "…"


def _first_pin_sentence(text: str) -> str:
    """Return the first sentence without stopping at common abbreviations."""
    for end in _SENTENCE_END.finditer(text):
        token = text[: end.end()].rsplit(" ", 1)[-1].lower().lstrip("(\"'“‘")
        if token in _PIN_ABBREVIATIONS or _PIN_INITIALISM.fullmatch(token):
            continue
        return text[: end.end()]
    return f"{text}."


def _pin_description(name: str, description: str, tags: list[str]) -> str:
    """Keyword pin text (KAN-284): name, first sentence, "Vegan recipe", up to 3 tags.

    Pinterest search indexes the pin description (up to 500 characters; the
    first 50-60 show in the feed), so the name leads. The placeholder
    description is skipped rather than pinned, and "vegan" is said once, not
    repeated from the tags. The keyword tail is kept whole: when the text is
    too long, the sentence gives way.
    """
    seen = {"vegan"}
    keywords: list[str] = []
    for tag in tags:
        tag = " ".join(tag.split())
        if not tag:
            continue
        if tag.lower() in seen or len(keywords) == PIN_DESCRIPTION_TAGS:
            continue
        if len(f"Vegan recipe: {', '.join([*keywords, tag])}.") > MAX_PIN_TAIL_LENGTH:
            continue
        seen.add(tag.lower())
        keywords.append(tag)
    tail = "Vegan recipe" + (f": {', '.join(keywords)}." if keywords else ".")
    head = " ".join(name.split())

    text = " ".join(description.split())
    if text == DEFAULT_RECIPE_DESCRIPTION or not text:
        # "Yum!" / "Ready?" already end a sentence; don't append a second stop.
        stop = "" if head.endswith((".", "!", "?", "…")) else "."
        return _trim_to_words(f"{head}{stop} {tail}", MAX_PIN_DESCRIPTION_LENGTH)
    sentence = _first_pin_sentence(text)
    budget = MAX_PIN_DESCRIPTION_LENGTH - len(head) - len(tail) - len(" — ") - 1
    return _trim_to_words(
        f"{head} — {_trim_to_words(sentence, max(budget, 1))} {tail}", MAX_PIN_DESCRIPTION_LENGTH
    )


def _pinterest_share_url(canonical_url: str, image_url: str | None, description: str) -> str:
    params = {
        "url": canonical_url,
        "description": description,
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
        "instructions": _recipe_instructions(data),
        "notes": data.get("notes"),
        "tags": _recipe_tags(data),
        "stock_image_url": data.get("stock_image_url"),
        "ai_image_url": image_url,
        "image": image_url,
        "slug": recipe.slug,
        "is_public": recipe.is_public,
    }


def _missing_recipe_response(slug: str, endpoint: str, *, canonical: bool) -> ResponseReturnValue:
    """What a slug with no live public recipe answers (KAN-288).

    - retired by a delete, or its recipe has since been deleted -> 410 Gone:
      the page existed and was removed on purpose, and never comes back;
    - retired by a rename and that recipe is public under a new slug -> 301;
    - anything else (never existed, or unpublished, which is reversible) -> 404.
    """
    retired = db.session.get(RetiredSlug, slug)
    if retired is None:
        abort(404)
    if retired.recipe_id is not None:
        target = db.session.get(Recipe, retired.recipe_id)
        if target is not None:
            if target.is_public and target.slug and target.slug != slug:
                carried = _carried_redirect_params(keep_save=True)
                location = (
                    _canonical_url(endpoint, slug=target.slug, **carried)
                    if canonical
                    else url_for(endpoint, slug=target.slug)
                )
                return redirect(location, code=301)
            abort(404)
    abort(410)


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
    linkable = {hub.slug for hub in _linkable_hubs(_hub_counts(catalog))}
    # KAN-274: the trail runs through the recipe's first indexable hub.
    category = next((hub for hub in hubs_for_tags(tags) if hub.slug in linkable), None)
    breadcrumbs = _breadcrumbs(recipe, category)

    return render_template(
        "public/recipe.html",
        recipe=recipe,
        page_title=_page_title(recipe.name),
        meta_description=_meta_description(description),
        breadcrumbs=breadcrumbs,
        breadcrumb_json_ld=_breadcrumb_json_ld(breadcrumbs),
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
            if pinterest_image_url
            else None
        ),
        pin_image_url=pinterest_image_url,
        pin_description=pin_description,
        spa_save_url=f"{_public_base_url()}/?save={recipe.slug}#kitchen",
    )


def _carried_redirect_params(*, keep_save: bool) -> dict[str, str]:
    """Query params a trailing-slash 301 may carry: ``utm_*`` and, optionally, ``save``.

    Only allow-listed keys, rebuilt by ``url_for`` onto the canonical host, never
    the raw query string (CodeQL py/url-redirection). ``save`` is the SPA's
    save-to-cookbook handoff and only means something on a recipe URL.
    """
    return {
        key: value
        for key, value in request.args.items()
        if (keep_save and key == "save")
        or (key.startswith("utm_") and key.replace("_", "").isalnum())
    }


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
        return _missing_recipe_response(slug, "public.public_recipe_json", canonical=False)
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

    # slug IS NOT NULL, like the sitemap: a slugless public row (legacy data;
    # publishing always mints a slug now) has no /r/ URL, and url_for on it
    # would fail the whole page.
    base_query = Recipe.query.filter(Recipe.is_public.is_(True), Recipe.slug.isnot(None)).options(
        joinedload(Recipe.user)
    )

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
    hubs = _linkable_hubs(_hub_counts(_catalog_tag_rows()))

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
        page=page,
        total_pages=total_pages,
        total=total,
        page_size=BROWSE_PAGE_SIZE,
        canonical_url=canonical_url,
        # /browse is subject to the same 155-char result-snippet cap that
        # ``_meta_description`` enforces on /r/<slug> — the boilerplate is
        # already 158 chars at today's 96 recipes and lengthens as ``total``
        # grows. Reuse the bounded copy for metadata and CollectionPage JSON-LD.
        description=snippet_description,
    )


HUB_PAGE_LIMIT = 60


@public_bp.route("/browse/tag/<hub_slug>", methods=["GET"])
def show_tag_hub(hub_slug):
    """A curated category hub: intro copy + every public recipe in it (KAN-274).

    Only allow-listed hubs exist (``services.tag_hubs``); anything else is a
    404, so arbitrary tag filters never become indexable pages. A hub below
    ``MIN_INDEXABLE_RECIPES`` still renders but is ``noindex``.
    """
    hub = HUBS_BY_SLUG.get(hub_slug)
    if hub is None:
        abort(404)

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
    )[:HUB_PAGE_LIMIT]
    ids = [row.id for row in members]
    # Recheck the catalog predicates during hydration: under READ COMMITTED, a row
    # can be unpublished or lose its slug after the lightweight catalog query.
    by_id = (
        {
            recipe.id: recipe
            for recipe in Recipe.query.filter(
                Recipe.id.in_(ids),
                Recipe.is_public.is_(True),
                Recipe.slug.isnot(None),
            ).all()
        }
        if ids
        else {}
    )
    recipes = []
    for recipe_id in ids:
        recipe = by_id.get(recipe_id)
        if recipe is None:
            continue
        # Tags can change between the catalog snapshot and hydration too; only
        # render recipes that still belong to this hub in their current data.
        if hub not in hubs_for_tags(_recipe_tags(recipe.data or {})):
            continue
        recipes.append(recipe)

    canonical_url = _hub_url(hub)
    page_title = _page_title(hub.title)
    description = _meta_description(hub.intro)
    breadcrumbs = _breadcrumbs(hub=hub)
    og_owner = next((r for r in recipes if _serves_own_image_bytes(r)), None)

    # Hydration can drop a concurrently unpublished, slug-cleared, or retagged
    # member; indexability must reflect what this response actually renders.
    indexable = len(recipes) >= MIN_INDEXABLE_RECIPES
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
    """
    if hub_slug not in HUBS_BY_SLUG:
        abort(404)
    carried = _carried_redirect_params(keep_save=False)
    return redirect(_canonical_url("public.show_tag_hub", hub_slug=hub_slug, **carried), code=301)


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
        # KAN-272: the static About page Express serves (author + E-E-A-T).
        {
            "loc": f"{_public_base_url()}/about",
            "changefreq": "monthly",
            "priority": "0.5",
        },
    ]

    # KAN-274: indexable hubs, lastmod from their newest-changed member. Reuses
    # the ``recipes`` rows already fetched above.
    hub_members = _hub_members(recipes)
    counts = _counts_from_members(hub_members)
    for hub in _linkable_hubs(counts):
        stamps = [
            row.updated_at or row.created_at
            for row in hub_members[hub.slug]
            if row.updated_at or row.created_at
        ]
        entries.append(
            {
                "loc": _hub_url(hub),
                "lastmod": max(stamps).date().isoformat() if stamps else None,
                "changefreq": "weekly",
                "priority": "0.7",
            }
        )

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
