"""Add worker claim token to Recipe.

Revision ID: f4a1c2d3e4b5
Revises: e91b47a2c5d3
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f4a1c2d3e4b5"
down_revision = "e91b47a2c5d3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.add_column(sa.Column("worker_claim_token", sa.String(length=36), nullable=True))


def downgrade():
    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.drop_column("worker_claim_token")
