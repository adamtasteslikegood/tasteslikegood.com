"""add recipe_metadata.cuisine

KAN-423: a recipe's cuisine, decided once and stored with its search metadata,
so the Recipe JSON-LD's ``recipeCuisine`` no longer depends on a tag happening
to name one. NULL until decided; ``none`` when no single cuisine fits.

Revision ID: a7c3e9d1b5f2
Revises: d9b3e7a1c5f4
Create Date: 2026-10-21 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a7c3e9d1b5f2"
down_revision = "d9b3e7a1c5f4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("recipe_metadata") as batch_op:
        batch_op.add_column(sa.Column("cuisine", sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table("recipe_metadata") as batch_op:
        batch_op.drop_column("cuisine")
