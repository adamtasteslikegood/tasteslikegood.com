"""merge status and slug heads

Revision ID: c60f6530f4ff
Revises: 03da1e46c9a5, fc014cd27ab4
Create Date: 2026-04-29 14:07:45.307268

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c60f6530f4ff"
down_revision = ("03da1e46c9a5", "fc014cd27ab4")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
