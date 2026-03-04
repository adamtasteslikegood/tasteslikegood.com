"""Add guest_session_id scoping for anonymous recipes and cookbooks

Revision ID: 71f3f5f0c2b1
Revises: d4f8c2e19a73
Create Date: 2026-03-03 12:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "71f3f5f0c2b1"
down_revision = "d4f8c2e19a73"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("recipe", sa.Column("guest_session_id", sa.String(length=64), nullable=True))
    op.create_index("ix_recipe_guest_session_id", "recipe", ["guest_session_id"], unique=False)

    op.add_column("cookbook", sa.Column("guest_session_id", sa.String(length=64), nullable=True))
    op.create_index("ix_cookbook_guest_session_id", "cookbook", ["guest_session_id"], unique=False)


def downgrade():
    op.drop_index("ix_cookbook_guest_session_id", table_name="cookbook")
    op.drop_column("cookbook", "guest_session_id")

    op.drop_index("ix_recipe_guest_session_id", table_name="recipe")
    op.drop_column("recipe", "guest_session_id")
