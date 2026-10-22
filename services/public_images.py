"""
Public SSR responsive image URLs, version tokens, saved-copy source fallback,
and ``srcset`` / ``sizes`` builders (KAN-461).

Extracted from ``blueprints.public_bp`` to keep public image negotiation and
URL versioning in a dedicated service module.
"""

import hashlib
import sys
from typing import Any, TypeVar, cast

from flask import url_for

from extensions import db
from models import Recipe
from services.public_seo import _absolute_url, _canonical_url
from utils.cache_utils import PIN_VARIANT_VERSION

_T = TypeVar("_T")


def _bp_attr(name: str, default: _T) -> _T:
    """Resolve ``name`` from ``blueprints.public_bp`` when loaded so test monkeypatches apply."""
    bp_mod = sys.modules.get("blueprints.public_bp")
    return cast(_T, getattr(bp_mod, name, default)) if bp_mod is not None else default


# Sized WebP variants (KAN-271). ``sizes`` mirror recipe-site.css: the hero
# spans ``.public-main`` (min(1200px, 100vw - 3rem)); browse cards are a
# 3-column grid above 900px and 2 columns at or below it, phones included
# (KAN-297).
#
# Card ``sizes`` give the image's own slot, not a share of the viewport
# (KAN-371). From recipe-site.css: ``.public-main`` is 100vw - 1.5rem up to
# 768px and 100vw - 3rem above it, capped at 1200px; the grid gap is 0.75rem
# up to 768px and 1.5rem above it; each card has a 1px border either side of
# the image. That makes the image 50vw - 20px, then 50vw - 38px, then a third
# of (100vw - 96px) less 2px, then 382px. "50vw" overstated a 412px phone's
# slot by 20px, which at DPR 2 is the difference between the 400 file and the
# 800 one. The 600 width is for phones at DPR 2.6-3, whose slot is 480-585
# device pixels. tests/test_image_variants.py pins the CSS these come from.
HERO_IMAGE_WIDTHS = (400, 800, 1200)
HERO_IMAGE_SIZES = "(max-width: 1248px) 100vw, 1200px"
CARD_IMAGE_WIDTHS = (400, 600, 800)
CARD_IMAGE_SIZES = (
    "(max-width: 768px) calc(50vw - 20px), (max-width: 900px) calc(50vw - 38px), "
    "(max-width: 1248px) calc(33.4vw - 34px), 382px"
)


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
    canonical_fn = _bp_attr("_canonical_url", _canonical_url)
    absolute_fn = _bp_attr("_absolute_url", _absolute_url)
    if _serves_own_image_bytes(recipe):
        return canonical_fn("generation_api.serve_recipe_image", recipe_id=recipe.id)
    return absolute_fn((recipe.data or {}).get("stock_image_url"))


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
    recipe_image_fn = _bp_attr("_recipe_image", _recipe_image)
    url: str | None = recipe_image_fn(recipe)[0]
    return url


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
    canonical_fn = _bp_attr("_canonical_url", _canonical_url)
    url: str = canonical_fn(
        "generation_api.serve_recipe_image",
        recipe_id=owner.id,
        pin=1,
        v=token,
        pv=PIN_VARIANT_VERSION,
    )
    return url


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
    canonical_fn = _bp_attr("_canonical_url", _canonical_url)
    url: str = canonical_fn("generation_api.serve_recipe_image", recipe_id=owner.id, v=token)
    return url


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
    rendered_fn = _bp_attr("_rendered_image", _rendered_image)
    url: str | None = rendered_fn(recipe)[0]
    return url


def _card_image(recipe: Recipe) -> dict[str, str] | None:
    """Image for a browse card: own bytes as sized variants, else stock, else none.

    Deliberately no saved-copy source fallback — that is a DB lookup per card,
    and /browse is asserted N+1-free. Same gate the template always applied.
    """
    if _serves_own_image_bytes(recipe):
        return _image_variants(recipe, CARD_IMAGE_WIDTHS)
    stock = (recipe.data or {}).get("stock_image_url")
    return {"src": stock} if stock else None
