"""add recipe_feedback and user.feedback_trusted

KAN-356: recipe feedback, Phase 1 (docs/designs/recipe-feedback.md). One entry
per account per public recipe while active (partial unique index); deleted
entries are kept for the moderator. ``user.feedback_trusted`` stores whether the
account's notes publish without review (eng review A1).

Revision ID: a7f3c9e1b2d4
Revises: d9b3e7a1c5f4
Create Date: 2026-10-30 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a7f3c9e1b2d4"
down_revision = "d9b3e7a1c5f4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "recipe_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recipe_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("stars", sa.SmallInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("note_status", sa.String(length=16), nullable=True),
        sa.Column("note_reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("entry_hidden", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("hidden_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("stars BETWEEN 1 AND 5", name="ck_recipe_feedback_stars"),
        sa.ForeignKeyConstraint(
            ["recipe_id"],
            ["recipe.id"],
            name="fk_recipe_feedback_recipe_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_recipe_feedback_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recipe_feedback_user_id", "recipe_feedback", ["user_id"], unique=False)
    op.create_index(
        "uq_recipe_feedback_active",
        "recipe_feedback",
        ["recipe_id", "user_id"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("feedback_trusted", sa.Boolean(), server_default="0", nullable=False)
        )


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("feedback_trusted")
    op.drop_index("uq_recipe_feedback_active", table_name="recipe_feedback")
    op.drop_index("ix_recipe_feedback_user_id", table_name="recipe_feedback")
    op.drop_table("recipe_feedback")
