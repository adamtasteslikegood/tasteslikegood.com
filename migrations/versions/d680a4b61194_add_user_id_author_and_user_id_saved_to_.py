"""add user_id_author and user_id_saved_to to recipe

Revision ID: d680a4b61194
Revises: c8f3b71d20a4
Create Date: 2026-08-10 07:16:43.476435

KAN-221: Split user_id into explicit author and saver identities.

- user_id_author  = the user who created/generated the recipe (immutable)
- user_id_saved_to = the user who saved a copy from a public page (NULL for originals)

Backfill logic:
  - Originals (source_slug IS NULL): user_id_author = user_id, user_id_saved_to = NULL
  - Saved copies (source_slug IS NOT NULL): look up source recipe's user_id
    as author; current user_id becomes user_id_saved_to
  - Orphaned copies (source deleted): user_id_author = NULL, user_id_saved_to = user_id
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d680a4b61194"
down_revision = "c8f3b71d20a4"
branch_labels = None
depends_on = None


def _backfill_author_saver(conn):
    """The backfill, on a bare connection so tests can execute the real thing
    (the pattern set by c8f3b71d20a4's helpers + its migration test)."""
    # 1. Originals (source_slug IS NULL): author = owner, saved_to = NULL
    conn.execute(sa.text("UPDATE recipe SET user_id_author = user_id " "WHERE source_slug IS NULL"))

    # 2. Saved copies: author = source recipe's user_id, saved_to = this row's user_id
    conn.execute(
        sa.text(
            "UPDATE recipe SET "
            "  user_id_author = ("
            "    SELECT r2.user_id FROM recipe r2 "
            "    WHERE r2.slug = recipe.source_slug "
            "    AND r2.is_public "
            "    LIMIT 1"
            "  ), "
            "  user_id_saved_to = user_id "
            "WHERE source_slug IS NOT NULL"
        )
    )

    # 3. Orphaned saved copies (source recipe deleted/unpublished):
    #    author stays NULL, but ensure saved_to is set
    conn.execute(
        sa.text(
            "UPDATE recipe SET user_id_saved_to = user_id "
            "WHERE source_slug IS NOT NULL "
            "AND user_id_author IS NULL "
            "AND user_id_saved_to IS NULL"
        )
    )


def upgrade():
    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id_author", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("user_id_saved_to", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_recipe_user_id_author", "user", ["user_id_author"], ["id"])
        batch_op.create_foreign_key(
            "fk_recipe_user_id_saved_to", "user", ["user_id_saved_to"], ["id"]
        )

    _backfill_author_saver(op.get_bind())

    # Optional index for author lookups
    op.create_index(
        "idx_recipe_user_id_author",
        "recipe",
        ["user_id_author"],
        unique=False,
    )


def downgrade():
    op.drop_index("idx_recipe_user_id_author", table_name="recipe")

    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.drop_constraint("fk_recipe_user_id_saved_to", type_="foreignkey")
        batch_op.drop_constraint("fk_recipe_user_id_author", type_="foreignkey")
        batch_op.drop_column("user_id_saved_to")
        batch_op.drop_column("user_id_author")
