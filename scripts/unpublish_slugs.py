"""Unpublish public recipes by slug (idempotent, reversible).

Sets ``is_public = False`` on each recipe whose slug is passed as an
argument. Unpublishing removes the recipe from /browse, /r/<slug> (404) and
the sitemap automatically; nothing is deleted, so a recipe is re-published
by flipping ``is_public`` back (e.g. via the SPA publish toggle).

Written for the cookbook #3164 audit to retire junk/test slugs and the two
recipes whose ``ai_image_url`` has no persisted bytes (dead hero/og:image).
Re-publish those two only after their images are backfilled.

Usage (local, against the DATABASE_URL in .env / SQLite dev DB):

    uv run python scripts/unpublish_slugs.py slug-one slug-two ...

In production, run inside a one-off Cloud Run job or ``flask shell``
environment with the production ``DATABASE_URL``:

    python scripts/unpublish_slugs.py slug-one slug-two ...

The script is idempotent: already-private and unknown slugs are reported and
skipped, and the exit code is 0 as long as every requested slug was either
unpublished now or already private. Unknown slugs exit 1 so typos are loud.
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402


def unpublish_slugs(app, slugs):
    """Set is_public=False for each slug. Returns (unpublished, already, missing)."""
    unpublished: list[str] = []
    already_private: list[str] = []
    missing: list[str] = []

    with app.app_context():
        for slug in slugs:
            recipe = Recipe.query.filter(Recipe.slug == slug).first()
            if recipe is None:
                missing.append(slug)
                print(f"MISSING   {slug} — no recipe with this slug")
                continue
            if not recipe.is_public:
                already_private.append(slug)
                print(f"ALREADY   {slug} — is_public was already False")
                continue
            recipe.is_public = False
            # The recipes API returns recipe.data verbatim and full saves write
            # data["is_public"] back to the column (db_recipe_repository.py:534,
            # 653), so a column-only unpublish would show stale is_public: true
            # in the SPA and get silently republished by the next save. Keep the
            # blob in sync, same as scripts/gate_guest_public_recipes.py.
            if isinstance(recipe.data, dict) and recipe.data.get("is_public"):
                recipe.data["is_public"] = False
            unpublished.append(slug)
            print(f"UNPUBLISH {slug} — is_public set to False (recipe id {recipe.id})")
        db.session.commit()

    print(
        f"Done: {len(unpublished)} unpublished, "
        f"{len(already_private)} already private, {len(missing)} missing."
    )
    return unpublished, already_private, missing


def main(argv):
    slugs = [slug for slug in argv if slug.strip()]
    if not slugs:
        print("Usage: python scripts/unpublish_slugs.py <slug> [<slug> ...]", file=sys.stderr)
        return 2

    app = create_app()
    _, _, missing = unpublish_slugs(app, slugs)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
