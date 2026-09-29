"""retire published slugs: retired_slug table + recipe.first_published_at

KAN-288 (Adam, 2026-09-28): a slug that once served a published /r/<slug>
page is never reassigned to a different recipe. Deleting the row used to free
the slug, and the next recipe with the same name took over the URL, its
backlinks, and every saved copy's ``source_slug`` pointer.

    retired_slug              slug PK, recipe_id (no FK; NULL = deleted,
                              permanent), retired_at. Written by the
                              before_flush hook in models/retired_slug.py.
    recipe.first_published_at When the row first went public. Non-NULL is
                              what makes giving up the slug a retirement.

Backfills:

1. ``first_published_at = created_at`` only where publication has evidence:
   the row is public now, or a saved copy's stable ``source_recipe_id`` points
   at it. Private rows have long been allowed to carry arbitrary payload slugs,
   so slug presence alone is not publication history.
2. Retire every ``source_slug`` without evidence that a matching live row owns
   it. A current public row proves ownership; so does a copy whose stable
   ``source_recipe_id`` points at that row. A private row that merely happens
   to carry the slug is not evidence and must not suppress the tombstone.
3. Protect ambiguous private slug rows with an owner-scoped ``retired_slug``
   marker while leaving ``first_published_at`` NULL. This conservatively keeps
   a possibly historical URL from being reassigned without falsely telling
   KAN-289 that the row was published. The model listener makes this marker
   permanent if the row is later deleted.

``op.add_column``, not ``batch_alter_table``: on SQLite a batch operation
recreates the table and silently drops the KAN-213 expression indexes
(see a3c9e1f4b7d2). A nullable ADD COLUMN is native on both dialects.

Revision ID: b7e2f0c4d9a1
Revises: a3c9e1f4b7d2
Create Date: 2026-09-28 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7e2f0c4d9a1"
down_revision = "a3c9e1f4b7d2"
branch_labels = None
depends_on = None


def _backfill(conn):
    """Run historical backfills on a bare connection for production-faithful tests."""
    conn.execute(
        sa.text(
            "UPDATE recipe SET first_published_at = created_at "
            "WHERE slug IS NOT NULL AND first_published_at IS NULL "
            "AND (is_public OR EXISTS ("
            "SELECT 1 FROM recipe proof WHERE proof.source_recipe_id = recipe.id"
            "))"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO retired_slug (slug, recipe_id, retired_at) "
            "SELECT r.source_slug, "
            "CASE WHEN COUNT(*) = COUNT(live.id) "
            "AND COUNT(DISTINCT live.id) = 1 "
            "THEN MIN(live.id) ELSE NULL END, "
            "CURRENT_TIMESTAMP FROM recipe r "
            "LEFT JOIN recipe live ON live.id = r.source_recipe_id "
            "WHERE r.source_slug IS NOT NULL AND r.source_slug <> '' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM recipe holder WHERE holder.slug = r.source_slug "
            "AND (holder.is_public OR EXISTS ("
            "SELECT 1 FROM recipe proof WHERE proof.source_slug = r.source_slug "
            "AND proof.source_recipe_id = holder.id"
            "))"
            ") "
            "AND NOT EXISTS (SELECT 1 FROM retired_slug rs WHERE rs.slug = r.source_slug) "
            "GROUP BY r.source_slug"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO retired_slug (slug, recipe_id, retired_at) "
            "SELECT holder.slug, holder.id, CURRENT_TIMESTAMP FROM recipe holder "
            "WHERE holder.slug IS NOT NULL AND holder.slug <> '' "
            "AND holder.first_published_at IS NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM retired_slug rs WHERE rs.slug = holder.slug"
            ")"
        )
    )


def upgrade():
    op.create_table(
        "retired_slug",
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("recipe_id", sa.String(length=36), nullable=True),
        sa.Column("retired_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("slug"),
    )
    op.add_column("recipe", sa.Column("first_published_at", sa.DateTime(), nullable=True))
    _backfill(op.get_bind())


def downgrade():
    op.drop_column("recipe", "first_published_at")
    op.drop_table("retired_slug")
