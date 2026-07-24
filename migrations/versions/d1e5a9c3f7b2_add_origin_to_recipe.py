"""Add origin column to recipe; backfill manual-entry rows

Manually entered recipes must not be publishable (KAN-140): the manual-entry
form is the only UI surface where a user types arbitrary full recipe content
with no AI mediation, and publishing puts that content on the public
/r/<slug> pages. The ``origin`` column records how each recipe entered the
system ('manual' | 'generated' | 'saved', NULL = legacy/unknown); the
repository's publish gate rejects ``is_public=true`` for 'manual' rows.

Backfill: the manual-entry modal has always written the blob signature
``image_keywords == [<name>, 'homemade']`` (exactly two elements, second
fixed). Rows matching it are marked 'manual'. Other rows stay NULL — the
gate treats NULL as publishable, so no legacy generated recipe loses
anything.

Revision ID: d1e5a9c3f7b2
Revises: c8d2f6a1e9b3
Create Date: 2026-07-24 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d1e5a9c3f7b2"
down_revision = "c8d2f6a1e9b3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.add_column(sa.Column("origin", sa.String(length=20), nullable=True))

    if bind.dialect.name == "postgresql":
        # CASE guarantees the array-type check runs before json_array_length
        # (plain AND does not short-circuit in PG, and json_array_length
        # raises on scalars).
        op.execute(
            "UPDATE recipe SET origin = 'manual' "
            "WHERE CASE WHEN json_typeof(data->'image_keywords') = 'array' "
            "THEN json_array_length(data->'image_keywords') = 2 "
            "AND data->'image_keywords'->>1 = 'homemade' "
            "ELSE false END"
        )
    else:
        # Same CASE guard: json_extract unwraps a scalar string to plain
        # text, which json_array_length then rejects as malformed JSON.
        op.execute(
            "UPDATE recipe SET origin = 'manual' "
            "WHERE CASE WHEN json_type(data, '$.image_keywords') = 'array' "
            "THEN json_array_length(data, '$.image_keywords') = 2 "
            "AND json_extract(data, '$.image_keywords[1]') = 'homemade' "
            "ELSE 0 END"
        )


def downgrade():
    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.drop_column("origin")
