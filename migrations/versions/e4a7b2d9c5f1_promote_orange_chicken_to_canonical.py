"""Promote the orange-chicken seitan recipe to canonical

Adds ``vegan-orange-chicken-style-seitan-with-white-rice`` to the canonical
set curated in the cookbook repo's specs/canonical-recipes.json (KAN-158).

Two reasons, both Adam's call:

- It is the set's first Asian/takeout-style entry, and it already earns its
  keep — a partial-slug Google query returns it as the top result with its
  hero image intact, so the public page is both indexed and rendering.
- ``is_canonical`` is the durability guarantee the promotion is really for.
  The repository guards (``_guard_canonical``) reject unpublish, re-slug, and
  delete for canonical rows, so the public /r/<slug> page survives the SPA
  copy being deleted later — which is exactly how a linked, indexed page
  would otherwise go 404 after inbound links exist.

Content edits still pass the guard; only publish state, slug, and row
deletion are locked.

Follows the same pattern as c8d2f6a1e9b3: curation happens by migration, and
the column is never writable through the API.

Revision ID: e4a7b2d9c5f1
Revises: d1e5a9c3f7b2
Create Date: 2026-07-25 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e4a7b2d9c5f1"
down_revision = "d1e5a9c3f7b2"
branch_labels = None
depends_on = None

# specs/canonical-recipes.json v1 (cookbook repo, updated 2026-07-25) — the
# single slug this revision adds on top of the seven seeded by c8d2f6a1e9b3.
PROMOTED_SLUG = "vegan-orange-chicken-style-seitan-with-white-rice"


def upgrade():
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE recipe SET is_canonical = :flag WHERE slug = :slug"),
        {"flag": True, "slug": PROMOTED_SLUG},
    )


def downgrade():
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE recipe SET is_canonical = :flag WHERE slug = :slug"),
        {"flag": False, "slug": PROMOTED_SLUG},
    )
