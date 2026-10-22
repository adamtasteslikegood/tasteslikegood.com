"""
Public SSR SEO, Schema.org JSON-LD, Pinterest share, and XML sitemap helpers.

Extracted from ``blueprints.public_bp`` (KAN-461) to keep structured-data
builders, snippet hygiene, canonical URL helpers, and sitemap XML assembly in a
cohesive service module.
"""

import os
import re
import sys
from collections.abc import Mapping
from typing import Any, TypeVar, cast
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, SubElement, tostring

from flask import request, url_for

from models import Recipe
from services.recipe_metadata import (
    MAX_TITLE_LENGTH,
    SITE_TITLE_SUFFIX,
    truncate_description,
)

_T = TypeVar("_T")


def _bp_attr(name: str, default: _T) -> _T:
    """Resolve ``name`` from ``blueprints.public_bp`` when loaded so test monkeypatches apply."""
    bp_mod = sys.modules.get("blueprints.public_bp")
    return cast(_T, getattr(bp_mod, name, default)) if bp_mod is not None else default


def _public_base_url() -> str:
    configured = os.environ.get("FRONTEND_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return request.url_root.rstrip("/")


# KAN-433: the site's own social profiles. Keep this list identical to the
# cookbook's (index.html and server/public/about.html), which name the same
# Organization on the Express pages.
SITE_SOCIAL_PROFILES = (
    "https://www.facebook.com/tasteslikegood.org",
    "https://www.instagram.com/tasteslikegooddotorg/",
    "https://www.pinterest.com/tasteslikegood/",
)


def _site_organization() -> dict[str, Any]:
    """The site as a schema.org ``Organization``, with its social profiles."""
    base_url_fn = _bp_attr("_public_base_url", _public_base_url)
    return {
        "@type": "Organization",
        "name": "Tasteslikegood.org",
        "url": f"{base_url_fn()}/",
        "sameAs": list(SITE_SOCIAL_PROFILES),
    }


def _absolute_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("//"):
        return f"{request.scheme}:{value}"
    if not value.startswith("/"):
        value = f"/{value}"
    base_url_fn = _bp_attr("_public_base_url", _public_base_url)
    return f"{base_url_fn()}{value}"


def _canonical_url(endpoint: str, **values: Any) -> str:
    base_url_fn = _bp_attr("_public_base_url", _public_base_url)
    return f"{base_url_fn()}{url_for(endpoint, **values)}"


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


DEFAULT_RECIPE_DESCRIPTION = "A vegan recipe from Tasteslikegood.org."


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


def _recipe_keywords(data: dict[str, Any]) -> str | None:
    """Tags minus the ones already sent as ``recipeCategory`` or ``recipeCuisine`` (KAN-320).

    Google: don't use a tag that's actually a recipeCategory or recipeCuisine.
    """
    already_typed = RECIPE_CATEGORY_BY_TAG.keys() | RECIPE_CUISINE_BY_TAG.keys()
    seen: set[str] = set()
    keywords: list[str] = []
    for tag in _recipe_tags(data):
        key = tag.lower()
        if key in already_typed or key in seen:
            continue
        seen.add(key)
        keywords.append(tag)
    return ", ".join(keywords) or None


STEP_NAME_LIMIT = 60

MAX_PIN_DESCRIPTION_LENGTH = 500
PIN_DESCRIPTION_TAGS = 3
# The "Vegan recipe: tags." tail's share of the 500. Recipe.name is String(200),
# so name + separators + tail always fit and the tail is never cut. Tags are
# unbounded strings (recipe_schema.json), so one that does not fit is skipped.
MAX_PIN_TAIL_LENGTH = 200
# Common title/cooking abbreviations must not terminate the first sentence.
# Multi-initial abbreviations such as "e.g." and "U.S." are recognized separately.
_PIN_ABBREVIATIONS = frozenset(
    {
        "approx.",
        "dr.",
        "jr.",
        "mr.",
        "mrs.",
        "ms.",
        "oz.",
        "prof.",
        "sr.",
        "st.",
        "tbsp.",
        "tsp.",
        "vs.",
    }
)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
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
    # A trailing comma/semicolon/colon plus the added stop reads as ",." (KAN-291).
    return f"{text.rstrip(',;: ')}."


def _step_name(text: str) -> str:
    """A short ``HowToStep.name`` from the step's first clause (KAN-320).

    A step that opens with a heading (``"Make the batter: Whisk…"``) is named by
    the heading; any other step by its first sentence, cut at a word boundary.
    """
    heading, colon, _ = text.partition(": ")
    if (
        colon
        and len(heading) <= STEP_NAME_LIMIT
        and not heading[-1:].isdigit()
        and not _SENTENCE_END.search(heading)
    ):
        return heading.strip()
    return _trim_to_words(_first_pin_sentence(text).rstrip(".!?"), STEP_NAME_LIMIT)


def _tag_labels(data: dict[str, Any], mapping: Mapping[str, str]) -> list[str]:
    labels: list[str] = []
    for tag in _recipe_tags(data):
        label = mapping.get(tag.lower())
        if label and label not in labels:
            labels.append(label)
    return labels


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
            {"@type": "Person", "name": author_name} if author_name else _site_organization()
        ),
        "publisher": _site_organization(),
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
            {
                "@type": "HowToStep",
                "position": index + 1,
                "name": _step_name(text),
                "text": text,
                "url": f"{canonical_url}#step-{index + 1}",
            }
            for index, text in enumerate(instructions)
        ]
        or None,
        "keywords": _recipe_keywords(data),
        "recipeCategory": _tag_labels(data, RECIPE_CATEGORY_BY_TAG) or None,
        "recipeCuisine": _tag_labels(data, RECIPE_CUISINE_BY_TAG) or None,
        "suitableForDiet": "https://schema.org/VeganDiet",
    }
    cleaned: dict[str, Any] = _clean_json(json_ld)
    return cleaned


def _page_title(name: str) -> str:
    """``name · Tasteslikegood.org`` when that fits a result line, else just ``name``.

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
    return truncate_description(text)


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
    canonical_fn = _bp_attr("_canonical_url", _canonical_url)
    return {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": name,
        "description": description,
        "url": canonical_url,
        "publisher": _site_organization(),
        "mainEntity": {
            "@type": "ItemList",
            "numberOfItems": len(recipes),
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": index + 1,
                    "url": canonical_fn("public.show_public_recipe", slug=recipe.slug),
                    "name": recipe.name,
                }
                for index, recipe in enumerate(recipes)
            ],
        },
    }


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


def _build_sitemap_xml(
    recipes: list[Any],
    hub_entries: list[dict[str, Any]],
) -> bytes:
    """Build XML sitemap bytes for the public recipe surface and indexable tag hubs."""
    base_url_fn = _bp_attr("_public_base_url", _public_base_url)
    canonical_fn = _bp_attr("_canonical_url", _canonical_url)

    latest_recipe_update = next(
        (
            recipe.updated_at or recipe.created_at
            for recipe in recipes
            if recipe.updated_at or recipe.created_at
        ),
        None,
    )

    entries: list[dict[str, Any]] = [
        {
            "loc": f"{base_url_fn()}/",
            "lastmod": latest_recipe_update.date().isoformat() if latest_recipe_update else None,
            "changefreq": "daily",
            "priority": "1.0",
        },
        {
            "loc": canonical_fn("public.browse_public_recipes"),
            "lastmod": latest_recipe_update.date().isoformat() if latest_recipe_update else None,
            "changefreq": "daily",
            "priority": "0.9",
        },
        # KAN-272: the static About page Express serves (author + E-E-A-T).
        {
            "loc": f"{base_url_fn()}/about",
            "changefreq": "monthly",
            "priority": "0.5",
        },
        *hub_entries,
    ]

    for recipe in recipes:
        last_modified = recipe.updated_at or recipe.created_at
        entries.append(
            {
                "loc": canonical_fn("public.show_public_recipe", slug=recipe.slug),
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

    xml_body: bytes = tostring(urlset, encoding="utf-8", xml_declaration=True)
    return xml_body
