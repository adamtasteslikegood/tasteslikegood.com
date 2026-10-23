"""
Public SSR tag-hub aggregation, related/adjacent internal linking, breadcrumb
trails, and browse/hub pagination helpers (KAN-461).

Extracted from ``blueprints.public_bp`` to keep catalog tag scans, curated hub
membership, and browse query/pagination mechanics in a cohesive service module.
"""

import logging
import re
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar, cast

from flask import g, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from models import Recipe
from services.public_seo import _canonical_url, _public_base_url, _recipe_tags
from services.tag_hubs import (
    MIN_INDEXABLE_RECIPES,
    TAG_HUBS,
    TagHub,
    hubs_for_tags,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _bp_attr(name: str, default: _T) -> _T:
    """Resolve ``name`` from ``blueprints.public_bp`` when loaded so test monkeypatches apply."""
    bp_mod = sys.modules.get("blueprints.public_bp")
    return cast(_T, getattr(bp_mod, name, default)) if bp_mod is not None else default


BROWSE_PAGE_SIZE = 20
# KAN-296: hubs page at the same size as /browse, so a hub's cards and the
# browse index read as one paginated surface.
HUB_PAGE_SIZE = BROWSE_PAGE_SIZE
# Numbered anchors shown either side of the current page; the first and last
# page are always shown, and a longer run collapses to an ellipsis.
PAGINATION_WINDOW = 2
# ``?page=`` in its one canonical spelling: a positive integer, no sign, no
# leading zero, no whitespace. Nine digits bounds the int() conversion; a page
# that large is past the last page anyway, so it 404s like any other.
_PAGE_PARAM = re.compile(r"[1-9][0-9]{0,8}")
# KAN-298: the ``/browse?sort=`` orderings. ``newest`` is the default listing.
BROWSE_SORT_LABELS = {"newest": "Newest first", "oldest": "Oldest first", "name": "Name A to Z"}
BROWSE_SORTS = tuple(BROWSE_SORT_LABELS)
# A recipe tag is a short label; anything longer in ``?tag=`` is cut here.
MAX_TAG_FILTER_LENGTH = 60

RELATED_RECIPE_COUNT = 6


def _catalog_tag_rows() -> list[Any]:
    """``(id, created_at, updated_at, tags)`` for every public recipe.

    The one catalog scan the related-recipes block and the tag hubs share. It
    reads ``data -> 'tags'`` only — never the full ``data`` blob, which can still
    carry legacy base64 images.
    """
    if "public_catalog_tag_rows" not in g:
        # One scan per request: the page's own blocks and the footer's hub
        # links (``_footer_hubs``) read the same rows.
        g.public_catalog_tag_rows = (
            Recipe.query.with_entities(
                Recipe.id,
                Recipe.created_at,
                Recipe.updated_at,
                Recipe.data["tags"].label("tags"),
            )
            .filter(Recipe.is_public.is_(True), Recipe.slug.isnot(None))
            .all()
        )
    rows: list[Any] = g.public_catalog_tag_rows
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


def _adjacent_recipes(recipe: Recipe, catalog: list[Any]) -> dict[str, Any] | None:
    """The recipes listed directly before and after this one on ``/browse`` (KAN-342).

    Default browse order: newest first, ``id`` breaking ties. ``previous`` is
    the newer neighbour and ``next`` the older one; either is ``None`` at an
    end of the list. Returns ``None`` when there is no neighbour at all.
    """
    ordered = sorted(
        catalog, key=lambda row: (row.created_at or datetime.min, row.id), reverse=True
    )
    index = next((i for i, row in enumerate(ordered) if row.id == recipe.id), None)
    if index is None:
        return None
    wanted = {
        "previous": ordered[index - 1].id if index > 0 else None,
        "next": ordered[index + 1].id if index + 1 < len(ordered) else None,
    }
    ids = [value for value in wanted.values() if value]
    if not ids:
        return None
    found = {
        row.id: {"name": row.name, "url": url_for("public.show_public_recipe", slug=row.slug)}
        for row in Recipe.query.with_entities(Recipe.id, Recipe.name, Recipe.slug).filter(
            Recipe.id.in_(ids), Recipe.is_public.is_(True), Recipe.slug.isnot(None)
        )
    }
    if not found:
        # Both neighbours stopped being public between the two queries.
        return None
    return {side: found.get(value) for side, value in wanted.items()}


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


def _footer_hubs() -> list[dict[str, str]]:
    """Hub links for the site footer of every page on ``base_public.html`` (KAN-319).

    The same rule as every other hub link: a thin hub is ``noindex`` and gets
    no link. Paths are literal, like the rest of the footer set. The error
    pages extend the base template too, so a failed catalog read (the 500 page
    after a database error) yields no hub links rather than a second failure.
    """
    catalog_fn = _bp_attr("_catalog_tag_rows", _catalog_tag_rows)
    try:
        linkable = _linkable_hubs(_hub_counts(catalog_fn()))
    except SQLAlchemyError:
        logger.warning("footer hub links skipped: catalog read failed", exc_info=True)
        return []
    return [{"href": f"/browse/tag/{hub.slug}", "label": hub.label} for hub in linkable]


def _hub_url(hub: TagHub) -> str:
    canonical_fn = _bp_attr("_canonical_url", _canonical_url)
    url: str = canonical_fn("public.show_tag_hub", hub_slug=hub.slug)
    return url


def _breadcrumbs(recipe: Recipe | None = None, hub: TagHub | None = None) -> list[dict[str, str]]:
    """Home → Browse [→ hub] [→ recipe]: the visible trail and its BreadcrumbList."""
    base_url_fn = _bp_attr("_public_base_url", _public_base_url)
    canonical_fn = _bp_attr("_canonical_url", _canonical_url)
    crumbs = [
        {"name": "Home", "url": f"{base_url_fn()}/"},
        {"name": "Browse", "url": canonical_fn("public.browse_public_recipes")},
    ]
    if hub is not None:
        crumbs.append({"name": hub.title, "url": _hub_url(hub)})
    if recipe is not None:
        crumbs.append(
            {
                "name": recipe.name,
                "url": canonical_fn("public.show_public_recipe", slug=recipe.slug),
            }
        )
    return crumbs


def _recipe_breadcrumbs(
    recipe: Recipe, tags: list[str], catalog: list[Any]
) -> list[dict[str, str]]:
    """A published recipe's trail. KAN-274: it runs through the first indexable hub.

    Shared by the SSR page and the public JSON payload, so the SPA's visible
    breadcrumb (KAN-295) is the same trail as the page's BreadcrumbList.
    """
    linkable = {hub.slug for hub in _linkable_hubs(_hub_counts(catalog))}
    category = next((hub for hub in hubs_for_tags(tags) if hub.slug in linkable), None)
    return _breadcrumbs(recipe, category)


def _recipe_hubs(tags: list[str], catalog: list[Any]) -> list[dict[str, str]]:
    """Every indexable hub a recipe belongs to, for its hub links (KAN-349).

    The breadcrumb names only the first; the page links them all, at the top
    and at the bottom, so each recipe page is a way into each of its hubs.
    """
    linkable = {hub.slug for hub in _linkable_hubs(_hub_counts(catalog))}
    return [
        {"title": hub.title, "url": _hub_url(hub)}
        for hub in hubs_for_tags(tags)
        if hub.slug in linkable
    ]


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


def _requested_page() -> int | None:
    """The ``?page=`` number: 1 when absent, ``None`` when not a canonical page (KAN-296).

    Only the ``page`` key is read; any other query parameter is left alone. A
    repeated key (``?page=2&page=3``) is not a canonical spelling either.
    """
    values = request.args.getlist("page")
    if not values:
        return 1
    if len(values) > 1:
        return None
    raw = values[0]
    return int(raw) if _PAGE_PARAM.fullmatch(raw) else None


def _normalise_tag(value: str) -> str:
    """A tag as ``?tag=`` compares it: inner whitespace collapsed, lower-cased."""
    return " ".join(value.split()).lower()


def _browse_view() -> dict[str, str]:
    """``url_for`` values for the requested ``/browse`` view: ``sort`` and ``tag`` (KAN-298).

    Only a known sort and a non-blank tag survive, so page links and the
    ``?page=1`` redirect are rebuilt from allow-listed keys, never the raw
    query string. The tag is matched case-insensitively and is cut to
    ``MAX_TAG_FILTER_LENGTH``.
    """
    view = {}
    sort = request.args.get("sort", "")
    if sort in BROWSE_SORTS:
        view["sort"] = sort
    tag = _normalise_tag(request.args.get("tag", ""))[:MAX_TAG_FILTER_LENGTH].rstrip()
    if tag:
        view["tag"] = tag
    return view


def _carries_tag(tags: list[Any], tag: str) -> bool:
    """Whether ``tags`` holds ``tag``, compared the way ``_browse_view`` normalises it."""
    return any(isinstance(t, str) and _normalise_tag(t) == tag for t in tags)


def _page_values(page: int) -> dict[str, int]:
    """``url_for`` values for a listing page: page 1 is the bare URL, never ``?page=1``."""
    return {"page": page} if page > 1 else {}


def _page_numbers(page: int, total_pages: int) -> list[int | None]:
    """Page numbers to link, ``None`` for an elided run (KAN-296).

    Always the first and last page plus ``PAGINATION_WINDOW`` either side of
    the current one. A gap of a single page shows that page rather than an
    ellipsis that would hide just one number.
    """
    shown = {1, total_pages}
    shown.update(
        range(max(1, page - PAGINATION_WINDOW), min(total_pages, page + PAGINATION_WINDOW) + 1)
    )
    numbers: list[int | None] = []
    previous = 0
    for number in sorted(shown):
        if number - previous == 2:
            numbers.append(previous + 1)
        elif number - previous > 2:
            numbers.append(None)
        numbers.append(number)
        previous = number
    return numbers


def _pagination(
    page: int, total_pages: int, page_url: Callable[[int], str]
) -> dict[str, Any] | None:
    """Template data for the numbered pagination nav; ``None`` on a single page."""
    if total_pages <= 1:
        return None
    return {
        "page": page,
        "total_pages": total_pages,
        "prev_url": page_url(page - 1) if page > 1 else None,
        "next_url": page_url(page + 1) if page < total_pages else None,
        "pages": [
            (
                {"gap": True}
                if number is None
                else {"number": number, "url": page_url(number), "current": number == page}
            )
            for number in _page_numbers(page, total_pages)
        ],
    }


def _hydrate_hub_page(hub: TagHub, members: list[Any], page: int) -> list[Recipe]:
    """Hydrate and re-verify ``Recipe`` rows for one page of a curated tag hub."""
    start = (page - 1) * HUB_PAGE_SIZE
    ids = [row.id for row in members[start : start + HUB_PAGE_SIZE]]
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
    recipes: list[Recipe] = []
    for recipe_id in ids:
        recipe = by_id.get(recipe_id)
        if recipe is None:
            continue
        # Tags can change between the catalog snapshot and hydration too; only
        # render recipes that still belong to this hub in their current data.
        if hub not in hubs_for_tags(_recipe_tags(recipe.data or {})):
            continue
        recipes.append(recipe)
    return recipes


def _sitemap_hub_entries(recipes: list[Any]) -> list[dict[str, Any]]:
    """Build sitemap entry dicts for indexable hubs from the catalog scan."""
    # KAN-274: indexable hubs, lastmod from their newest-changed member. Reuses
    # the ``recipes`` rows ``sitemap_xml`` already fetched.
    hub_members = _hub_members(recipes)
    counts = _counts_from_members(hub_members)
    entries: list[dict[str, Any]] = []
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
    return entries
