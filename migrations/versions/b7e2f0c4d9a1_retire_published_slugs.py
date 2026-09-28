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

1. ``first_published_at = created_at`` for every row that holds a slug. Slugs
   are assigned by publishing (``_resolve_public_slug``) and kept on
   unpublish, so a slugged row is treated as having been published. The
   exact moment is not recorded anywhere, and only NULL vs non-NULL matters.
2. Retire every ``source_slug`` that no live row holds: the source a copy was
   saved from was deleted (or renamed) before this table existed, and that
   URL must not go to the next recipe that happens to share the name.

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
    """Both backfills on a bare connection, so tests can execute the real thing."""
    conn.execute(
        sa.text(
            "UPDATE recipe SET first_published_at = created_at "
            "WHERE slug IS NOT NULL AND first_published_at IS NULL"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO retired_slug (slug, recipe_id, retired_at) "
            "SELECT r.source_slug, "
            "CASE WHEN COUNT(*) = COUNT(r.source_recipe_id) "
            "AND COUNT(DISTINCT r.source_recipe_id) = 1 "
            "THEN MIN(r.source_recipe_id) ELSE NULL END, "
            "CURRENT_TIMESTAMP FROM recipe r "
            "WHERE r.source_slug IS NOT NULL AND r.source_slug <> '' "
            "AND NOT EXISTS (SELECT 1 FROM recipe r2 WHERE r2.slug = r.source_slug) "
            "AND NOT EXISTS (SELECT 1 FROM retired_slug rs WHERE rs.slug = r.source_slug) "
            "GROUP BY r.source_slug"
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
