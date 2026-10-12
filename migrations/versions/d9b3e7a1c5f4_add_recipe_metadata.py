"""add recipe_metadata

KAN-388: search title and meta description written by a server-side pass after
a recipe is published, versioned, in their own table rather than in
``recipe.data`` (which the app overwrites from the browser's copy).

Revision ID: d9b3e7a1c5f4
Revises: c4d8e2a6f1b3
Create Date: 2026-10-12 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d9b3e7a1c5f4"
down_revision = "c4d8e2a6f1b3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "recipe_metadata",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recipe_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("seo_title", sa.String(length=200), nullable=False),
        sa.Column("meta_description", sa.String(length=400), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("inputs_hash", sa.String(length=64), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name="fk_recipe_metadata_recipe_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recipe_id", "version", name="uq_recipe_metadata_recipe_version"),
    )


def downgrade():
    op.drop_table("recipe_metadata")
