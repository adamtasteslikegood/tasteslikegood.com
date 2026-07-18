"""Enforce unique cookbook name per owner scope

Adds two partial unique indexes so the app can no longer persist the duplicate
cookbooks produced by rapid repeat "Create" clicks:

    uq_cookbook_user_name   UNIQUE (user_id, name)          WHERE user_id IS NOT NULL
    uq_cookbook_guest_name  UNIQUE (guest_session_id, name) WHERE guest_session_id IS NOT NULL

Exactly one of (user_id, guest_session_id) is non-NULL on every row, so two
partial indexes (rather than one composite constraint) are the correct shape.

Existing rows may already violate the new constraint (the duplicate-cookbook
race shipped duplicates to production). Creating a unique index over dirty data
fails, which would abort the flask-backend-migrate job and block every deploy.
So ``upgrade()`` first de-duplicates by RENAMING later collisions — it keeps the
earliest row per (scope, name) untouched and suffixes the rest ("Name (2)", …).
No rows are deleted; all data is preserved.

Revision ID: b7e2a9c4d1f8
Revises: f4a1c2d3e4b5
Create Date: 2026-07-18 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7e2a9c4d1f8"
down_revision = "f4a1c2d3e4b5"
branch_labels = None
depends_on = None

_NAME_MAX = 200  # cookbook.name is String(200)


def _suffixed(base, n):
    """Return ``"base (n)"`` trimmed to fit the name column."""
    tag = f" ({n})"
    return base[: max(0, _NAME_MAX - len(tag))] + tag


def _dedupe_scope(bind, scope_col):
    """Rename duplicate (scope, name) rows so a unique index can be created.

    Keeps the earliest row (by created_at, then id) per (scope, name) and
    appends an incrementing suffix to the rest, avoiding new collisions.
    """
    rows = bind.execute(
        sa.text(
            f"SELECT id, {scope_col} AS scope, name "  # noqa: S608 - scope_col is a fixed literal
            "FROM cookbook "
            f"WHERE {scope_col} IS NOT NULL "
            f"ORDER BY {scope_col}, name, created_at, id"
        )
    ).fetchall()

    taken = {(r.scope, r.name) for r in rows}
    seen: dict[tuple, int] = {}

    for r in rows:
        key = (r.scope, r.name)
        count = seen.get(key, 0)
        seen[key] = count + 1
        if count == 0:
            continue  # earliest row for this (scope, name) — leave it alone

        suffix = count + 1
        new_name = _suffixed(r.name, suffix)
        while (r.scope, new_name) in taken:
            suffix += 1
            new_name = _suffixed(r.name, suffix)
        taken.add((r.scope, new_name))
        bind.execute(
            sa.text("UPDATE cookbook SET name = :name WHERE id = :id"),
            {"name": new_name, "id": r.id},
        )


def upgrade():
    bind = op.get_bind()

    # The prod migrate job (flask-backend-migrate) runs while the OLD Flask
    # revision is still serving traffic, so it could INSERT a fresh duplicate
    # between the de-dup pass below and CREATE UNIQUE INDEX — which would make
    # the index build fail and abort the deploy. Take an exclusive table lock
    # (held until this transaction commits, i.e. past index creation) so no
    # write can slip into that window. Postgres only: SQLite has no LOCK TABLE
    # and runs the migration single-connection anyway.
    if bind.dialect.name == "postgresql":
        op.execute("LOCK TABLE cookbook IN ACCESS EXCLUSIVE MODE")

    _dedupe_scope(bind, "user_id")
    _dedupe_scope(bind, "guest_session_id")

    op.create_index(
        "uq_cookbook_user_name",
        "cookbook",
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
        sqlite_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_cookbook_guest_name",
        "cookbook",
        ["guest_session_id", "name"],
        unique=True,
        postgresql_where=sa.text("guest_session_id IS NOT NULL"),
        sqlite_where=sa.text("guest_session_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("uq_cookbook_guest_name", table_name="cookbook")
    op.drop_index("uq_cookbook_user_name", table_name="cookbook")
