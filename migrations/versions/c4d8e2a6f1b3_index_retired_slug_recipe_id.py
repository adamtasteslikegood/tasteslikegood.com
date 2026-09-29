"""index retired_slug.recipe_id

KAN-288: the before_flush hook looks up every recipe deletion's owned aliases
by ``retired_slug.recipe_id``. The table only grows, so without an index each
delete (drafts included) scans the whole tombstone history.

A new revision rather than an edit to b7e2f0c4d9a1, so any database that has
already applied that revision still gets the index.

Revision ID: c4d8e2a6f1b3
Revises: b7e2f0c4d9a1
Create Date: 2026-09-28 00:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c4d8e2a6f1b3"
down_revision = "b7e2f0c4d9a1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_retired_slug_recipe_id", "retired_slug", ["recipe_id"], unique=False)


def downgrade():
    op.drop_index("ix_retired_slug_recipe_id", table_name="retired_slug")
