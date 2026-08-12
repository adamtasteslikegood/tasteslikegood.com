"""add source_recipe_id and re-key the KAN-213 identity indexes onto it

KAN-221 (locked 2026-08-10/11): the provenance key moves from the mutable slug
string to the stable source row id.

    source_recipe_id  String(36) FK -> recipe.id, ON DELETE SET NULL.
                      Immutable once set; resolved server-side, never
                      client-writable. source_slug is KEPT — it still builds
                      the /r/<slug> link — only the KEY moves.

The KAN-213 partial unique indexes are re-keyed from

    (scope, COALESCE(source_slug, slug))            [c8f3b71d20a4]

to

    (scope, COALESCE(source_recipe_id, source_slug, id))

For saved copies this IS the locked (user_id_saved_to, source_recipe_id)
constraint: user_id == user_id_saved_to on every saved copy by construction
(create sets both to the saver; login-merge sets both to the new user), and the
scope columns stay user_id / guest_session_id so guest copies and non-copy rows
keep their coverage. Reading the new COALESCE:

  - a resolved copy keys on its source's immutable id, so it collides with the
    source row itself (which keys on its own id — the `id` arm) and with every
    other resolved copy of it under the same owner, regardless of slug renames;
  - an unresolved copy (source gone before this backfill, raw legacy rows)
    falls back to the slug pointer, preserving c8f3b71d20a4's dedup between
    such copies;
  - every other row keys on its own primary key, which is vacuously unique —
    the constraint still only ever refuses pairs involving a saved copy, so
    authored recipes remain unconstrained exactly as agreed on KAN-213.

Why NO de-dup pre-pass this time: every group the new key forms is a subset of
a group the old key formed (a resolved copy's source id replaces the slug both
sides shared; the id arm only joins rows the slug arm already joined via the
globally-unique slug). Data that satisfies the c8f3b71d20a4 indexes therefore
cannot violate these, and alembic guarantees c8f3b71d20a4 ran first.

Same operational rules as c8f3b71d20a4: NO ``CREATE INDEX CONCURRENTLY``
(an invalid index that silently enforces nothing is this project's most
expensive recurring failure mode); an ACCESS EXCLUSIVE lock instead, held for
the whole transaction, affordable because `recipe` is a small table. The old
indexes are dropped before the batch column-add so SQLite's batch table
recreation never has to copy the expression indexes.

Revision ID: a3c9e1f4b7d2
Revises: d680a4b61194
Create Date: 2026-08-11 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a3c9e1f4b7d2"
down_revision = "d680a4b61194"
branch_labels = None
depends_on = None

# Matches models/recipe.py._IDENTITY — the model declares the same indexes so
# db.create_all() (tests, fresh dev) matches what this migration builds.
_IDENTITY = "coalesce(source_recipe_id, source_slug, id)"
_IDENTITY_V1 = "coalesce(source_slug, slug)"

_INDEXES = (
    ("uq_recipe_user_recipe_identity", "user_id"),
    ("uq_recipe_guest_recipe_identity", "guest_session_id"),
)


def _backfill_source_recipe_id(conn):
    """Resolve each copy's source_slug to the source row's id.

    The same resolution d680a4b61194 used for the author backfill (slug match
    on a public row), so the two provenance columns can never disagree about
    which row a copy came from. Unresolvable pointers stay NULL: the row keeps
    its source_slug, stays a saved copy for the publish guard, and falls back
    to the slug arm of the identity key. On a bare connection so tests can
    execute the real thing (the c8f3b71d20a4 pattern).
    """
    conn.execute(
        sa.text(
            "UPDATE recipe SET "
            "  source_recipe_id = ("
            "    SELECT r2.id FROM recipe r2 "
            "    WHERE r2.slug = recipe.source_slug "
            "    AND r2.is_public "
            "    LIMIT 1"
            "  ) "
            "WHERE source_slug IS NOT NULL"
        )
    )


def upgrade():
    bind = op.get_bind()

    # Hold writers out for the whole drop -> add -> backfill -> re-key
    # sequence; released when this migration's transaction commits.
    # Postgres only: SQLite has no LOCK TABLE and runs single-connection.
    if bind.dialect.name == "postgresql":
        op.execute("LOCK TABLE recipe IN ACCESS EXCLUSIVE MODE")

    # IF EXISTS (portable across PostgreSQL and SQLite), not op.drop_index: on
    # SQLite, d680a4b61194's batch_alter_table recreated the recipe table and
    # silently dropped these expression indexes (alembic batch mode cannot
    # reflect them), so a local dev chain reaches this point without them. On
    # PostgreSQL batch mode is a plain ALTER and they exist. Either way this
    # migration ends by (re)creating the re-keyed pair below, which also heals
    # the SQLite drift.
    for name, _scope in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")

    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_recipe_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_recipe_source_recipe_id",
            "recipe",
            ["source_recipe_id"],
            ["id"],
            ondelete="SET NULL",
        )

    _backfill_source_recipe_id(bind)

    for name, scope in _INDEXES:
        op.create_index(
            name,
            "recipe",
            [sa.text(scope), sa.text(_IDENTITY)],
            unique=True,
            postgresql_where=sa.text(f"{scope} IS NOT NULL"),
            sqlite_where=sa.text(f"{scope} IS NOT NULL"),
        )


def downgrade():
    for name, _scope in _INDEXES:
        op.drop_index(name, table_name="recipe")

    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.drop_constraint("fk_recipe_source_recipe_id", type_="foreignkey")
        batch_op.drop_column("source_recipe_id")

    # Restore the c8f3b71d20a4 shape.
    for name, scope in _INDEXES:
        op.create_index(
            name,
            "recipe",
            [sa.text(scope), sa.text(_IDENTITY_V1)],
            unique=True,
            postgresql_where=sa.text(f"{_IDENTITY_V1} IS NOT NULL AND {scope} IS NOT NULL"),
            sqlite_where=sa.text(f"{_IDENTITY_V1} IS NOT NULL AND {scope} IS NOT NULL"),
        )
