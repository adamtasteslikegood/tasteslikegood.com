"""Which published recipes still have no stored cuisine (KAN-423).

A recipe's cuisine is decided once by the metadata engine and stored on its
current ``recipe_metadata`` version: a name from ``CUISINES``, or ``none`` when
no single cuisine fits. This lists the public recipes that are still undecided,
so the fill can be watched to completion, and exits 1 while any remain.

Two modes, because production's database is reachable only from inside the VPC:

``db`` (default) reads the database the app is configured for and prints every
public recipe in one of three buckets: decided (with the cuisine), ``none``,
and undecided (no stored value, or no metadata version for the current content).
Locally::

    uv run python scripts/recipe_cuisine_report.py

Against production, run the same question in Cloud SQL Studio::

    SELECT r.slug, m.cuisine
    FROM recipe r
    LEFT JOIN recipe_metadata m
      ON m.recipe_id = r.id
     AND m.version = (SELECT max(version) FROM recipe_metadata WHERE recipe_id = r.id)
    WHERE r.is_public
    ORDER BY (m.cuisine IS NULL) DESC, m.cuisine, r.slug;

``site`` reads a live site from outside: every ``/r/<slug>`` in its sitemap
whose Recipe JSON-LD has no ``recipeCuisine``. That cannot tell a stored
``none`` from an undecided recipe, but reading each page also makes the site
queue the fill for the undecided ones, so a second run a day later is the
check that the fill is converging::

    uv run python scripts/recipe_cuisine_report.py site https://www.tasteslikegood.org
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

USER_AGENT = {"User-Agent": "tlg-cuisine-report/1.0 (KAN-423)"}


def _get(url: str) -> str:
    request = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(request, timeout=30) as response:
        text: str = response.read().decode("utf-8", "replace")
        return text


def report_db() -> int:
    from app import create_app
    from models.recipe import Recipe
    from services.recipe_metadata import CUISINE_NONE, metadata_inputs, inputs_hash
    from services import recipe_metadata as engine

    app = create_app()
    decided: list[tuple[str, str]] = []
    none: list[str] = []
    undecided: list[str] = []
    with app.app_context():
        for recipe in Recipe.query.filter_by(is_public=True).order_by(Recipe.slug).all():
            digest = inputs_hash(metadata_inputs(recipe))
            current = engine._usable(engine._versions(recipe.id, digest), digest)
            cuisine = current.cuisine if current is not None else None
            if cuisine is None:
                undecided.append(recipe.slug)
            elif cuisine == CUISINE_NONE:
                none.append(recipe.slug)
            else:
                decided.append((recipe.slug, cuisine))
    print(f"decided   {len(decided)}")
    for slug, cuisine in decided:
        print(f"  {slug:60} {cuisine}")
    print(f"none      {len(none)}")
    for slug in none:
        print(f"  {slug}")
    print(f"undecided {len(undecided)}")
    for slug in undecided:
        print(f"  {slug}")
    return 1 if undecided else 0


def _recipe_json_ld(html: str) -> dict | None:
    for match in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        items = parsed.get("@graph", [parsed]) if isinstance(parsed, dict) else parsed
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Recipe":
                return item
    return None


def report_site(base_url: str) -> int:
    base_url = base_url.rstrip("/")
    sitemap = _get(f"{base_url}/sitemap.xml")
    urls = sorted(set(re.findall(r"<loc>([^<]+/r/[^<]+)</loc>", sitemap)))
    missing: list[str] = []
    errors: list[str] = []
    for url in urls:
        try:
            recipe = _recipe_json_ld(_get(url))
        except Exception as e:  # a report keeps going
            errors.append(f"{url}: {e}")
            continue
        if recipe is None or not recipe.get("recipeCuisine"):
            missing.append(url.rsplit("/", 1)[-1])
    print(f"recipe pages        {len(urls)}")
    print(f"without recipeCuisine {len(missing)}  (stored none or still undecided)")
    for slug in missing:
        print(f"  {slug}")
    for error in errors:
        print(f"error {error}")
    return 1 if missing or errors else 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "db":
        return report_db()
    if argv[0] == "site" and len(argv) == 2:
        return report_site(argv[1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
