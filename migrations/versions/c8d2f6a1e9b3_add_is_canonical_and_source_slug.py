"""Add is_canonical lock + source_slug reference to recipe

Publish state must resolve to one DB row (KAN-139 / cookbook #3217):

- ``is_canonical`` locks the recipes curated in the cookbook repo's
  specs/canonical-recipes.json: their publish state, slug, and row cannot be
  changed through the API (repository guards return 400) — only content
  edits. The column is never writable via the API; this migration seeds it
  for the approved slugs, and future curation happens by migration/script.
- ``source_slug`` mirrors the data blob's ``sourceSlug`` key (the public
  slug a saved copy came from) into a real column, so the server can answer
  "was this saved from a published recipe?" without parsing JSON, and the
  SPA gets an authoritative value instead of trusting its own blob.

Revision ID: c8d2f6a1e9b3
Revises: b7e2a9c4d1f8
Create Date: 2026-07-24 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c8d2f6a1e9b3"
down_revision = "b7e2a9c4d1f8"
branch_labels = None
depends_on = None

# specs/canonical-recipes.json v1 (cookbook repo, updated 2026-07-23).
CANONICAL_SLUGS = (
    "classic-vegan-margherita-pizza",
    "vegan-cornbread",
    "vegan-seitan-fried-chicken-and-waffles",
    "vegan-spaghetti-and-meatballs",
    "classic-vegan-chocolate-chip-cookies",
    "maple-smoked-tempeh-blt",
    "vegan-street-style-tofu-tacos",
)


def upgrade():
    bind = op.get_bind()

    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_canonical", sa.Boolean(), server_default="0", nullable=False)
        )
        batch_op.add_column(sa.Column("source_slug", sa.String(length=255), nullable=True))

    # Backfill source_slug from the JSON blob's sourceSlug key. The blob is
    # the historical origin of the value (written by the SPA when saving a
    # public recipe), so the column can only ever lag the blob, never lead it.
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE recipe SET source_slug = data->>'sourceSlug' "
            "WHERE data->>'sourceSlug' IS NOT NULL"
        )
    else:
        op.execute(
            "UPDATE recipe SET source_slug = json_extract(data, '$.sourceSlug') "
            "WHERE json_extract(data, '$.sourceSlug') IS NOT NULL"
        )

    bind.execute(
        sa.text("UPDATE recipe SET is_canonical = :flag WHERE slug IN :slugs").bindparams(
            sa.bindparam("slugs", expanding=True)
        ),
        {"flag": True, "slugs": list(CANONICAL_SLUGS)},
    )


def downgrade():
    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.drop_column("source_slug")
        batch_op.drop_column("is_canonical")
