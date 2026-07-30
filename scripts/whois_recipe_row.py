"""Report who owns a recipe row. READ-ONLY — this script never writes (KAN-155).

KAN-155 refuses a write when the target row is owned by another account or
guest session. That refusal is correct (KAN-181 INV-4). Deciding whether a
*specific* refusal is also *repairable* needs one fact the application cannot
supply to an agent: who actually owns the row in production.

There are exactly two dispositions, and they are opposite:

* ``user`` — the row belongs to a real, separately-registered account. **The
  Google email is the identity. ``User.name`` is a display convenience and
  carries no identity meaning for any user.** Two accounts sharing a name are
  two different people as far as this system is concerned, and one person may
  hold several accounts; neither is a duplicate-account defect. The refusal is
  correct, there is nothing to repair, and reassigning the row would be the
  cross-account write KAN-155's own risk register exists to prevent.
* ``orphaned guest`` — ``user_id IS NULL``. This is the only repairable case:
  the guest→user merge at OAuth login never claimed the row server-side.

Usage (local, against the DATABASE_URL in .env):

    uv run python scripts/whois_recipe_row.py <recipe-id> [<recipe-id> ...]

In production, run inside a one-off Cloud Run job or a ``flask shell``
environment carrying the production ``DATABASE_URL``:

    python scripts/whois_recipe_row.py 62ccf6fc-d097-4a1c-8412-afa53c16faeb

Exit codes: 0 when every requested id was found, 1 when any was missing (so a
typo is loud rather than silently reported as "no owner").

Safety: the only database verbs used are SELECT, via read-only ORM queries.
There is no commit, no flush, no attribute assignment on any loaded row.
"""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402


def describe(recipe_id):
    """Return a human-readable ownership report for one recipe id, or None."""
    recipe = Recipe.query.filter(Recipe.id == recipe_id).first()
    if recipe is None:
        return None

    lines = [
        f"id                : {recipe.id}",
        f"name              : {recipe.name!r}",
        f"slug              : {recipe.slug!r}",
        f"is_public         : {recipe.is_public}",
        f"is_canonical      : {getattr(recipe, 'is_canonical', None)}",
        f"origin            : {getattr(recipe, 'origin', None)}",
        f"source_slug       : {getattr(recipe, 'source_slug', None)}",
        f"created_at        : {recipe.created_at}",
        f"updated_at        : {recipe.updated_at}",
        f"user_id           : {recipe.user_id}",
        f"guest_session_id  : {recipe.guest_session_id}",
    ]

    if recipe.user_id is not None:
        owner = User.query.filter(User.id == recipe.user_id).first()
        if owner is None:
            lines.append("owner             : user_id set but no matching user row (dangling)")
            lines.append("DISPOSITION       : dangling owner — needs a decision, not a reassign")
        else:
            lines.append(f"owner email       : {owner.email}")
            lines.append(f"owner name        : {owner.name!r}")
            lines.append(
                "DISPOSITION       : owned by a registered account. If this email differs "
                "from the acting user's, the refusal is CORRECT and there is nothing to "
                "repair. Identity is the Google email; the name above is display-only and "
                "means nothing for ownership — never compare names to decide this."
            )
    else:
        lines.append(
            "DISPOSITION       : orphaned guest row. This is the repairable case — the "
            "guest→user merge at OAuth login never claimed it server-side."
        )

    return "\n".join(lines)


def main(argv):
    recipe_ids = argv[1:]
    if not recipe_ids:
        print(__doc__)
        return 1

    app = create_app()
    missing = []

    with app.app_context():
        for recipe_id in recipe_ids:
            print("=" * 72)
            report = describe(recipe_id)
            if report is None:
                missing.append(recipe_id)
                print(f"id                : {recipe_id}\nNOT FOUND")
            else:
                print(report)
        print("=" * 72)

    if missing:
        print(f"\n{len(missing)} id(s) not found: {', '.join(missing)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
