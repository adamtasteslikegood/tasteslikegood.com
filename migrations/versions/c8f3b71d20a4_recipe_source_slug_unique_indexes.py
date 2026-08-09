"""Enforce one saved copy per (owner, source_slug)

KAN-213. Adds two partial unique indexes so the database, not the SPA, refuses
a duplicate saved recipe:

    uq_recipe_user_source_slug   UNIQUE (user_id, source_slug)
        WHERE source_slug IS NOT NULL AND user_id IS NOT NULL
    uq_recipe_guest_source_slug  UNIQUE (guest_session_id, source_slug)
        WHERE source_slug IS NOT NULL AND guest_session_id IS NOT NULL

Exactly one of (user_id, guest_session_id) is non-NULL per row, so two partial
indexes are the correct shape rather than one composite constraint — the same
reasoning as the cookbook pair in b7e2a9c4d1f8. Both ship together or neither
(Sprint 6 R3): guests key on guest_session_id, which is the KAN-186 path.

Partial on ``source_slug IS NOT NULL`` deliberately, and the scope is narrower
than "most of the table is excluded" suggests. Only ``origin = 'saved'`` rows
carry a source_slug (the SPA sets origin and sourceSlug together when saving
from a public page), so:

    These indexes constrain only copies a user took from someone else's public
    page. They do not constrain a single recipe a user authored.

Recorded ratio: user 1 held 112 NULL-source rows against 4 rows in source_slug
duplicate groups — roughly 3% coverage for that user.

That is still the right corner, and not by rationalisation: a saved copy is the
only case where "these two rows are the same recipe" is a machine-checkable
fact, because the copy records what it was copied from. Two separately generated
recipes have no such identity, and a name-based constraint was rejected — two
genuinely different recipes may legitimately share a title.

The evidence agrees the corner is where the duplicates were: all 11 public
duplicate rows carried a source_slug, and both constraint blockers were
source_slug pairs. KAN-220 explains why that is not luck — the ghost-session
path (expired session silently downgraded to guest before a save from a public
page) produces source_slug-bearing rows by construction.

This closes KAN-213's class and does NOT make the table duplicate-free.

--------------------------------------------------------------------------
Why NOT ``CREATE INDEX CONCURRENTLY``
--------------------------------------------------------------------------
The Sprint 6 runbook (specs/KAN-213_DEDUP_QUERIES.md §6) originally specified
CONCURRENTLY inside an ``op.get_context().autocommit_block()``. That is the
wrong tool here, and the cookbook migration already learned why.

``flask-backend-migrate`` runs as a Cloud Run Job *while the previous Flask
revision is still serving traffic*. CONCURRENTLY takes no write lock, so a
duplicate can be inserted during the build — and a unique index build that
meets a violation does not merely fail, it leaves an **INVALID** index behind
that silently enforces nothing until someone reindexes it. That is precisely
the project's recurring failure mode: a guard that appears to exist and does
not fire.

An ACCESS EXCLUSIVE lock closes that window. It is affordable because `recipe`
is a small table (hundreds of rows), where the index build is sub-second.
CONCURRENTLY earns its complexity on large hot tables; this is not one.

--------------------------------------------------------------------------
Why the pre-pass NULLs rather than deletes
--------------------------------------------------------------------------
Creating a unique index over dirty data fails and aborts the deploy. Production
was purged by hand on 2026-08-08/09 and both gates returned zero rows, so this
pre-pass is expected to be a no-op there. It exists because (a) other
environments may hold duplicates, and (b) nothing prevents a new duplicate
between that purge and this deploy — the guard being added here is the very
thing that does not exist yet.

It clears ``source_slug`` on the later rows instead of deleting them. A
migration must not destroy user content unattended, and the recipe body is
fully preserved; only the "saved from" pointer is dropped on the loser, which
takes that row out of the partial index's coverage. Deleting duplicate recipes
in production is Adam's call (Sprint 6 D4), not a migration's.

Revision ID: c8f3b71d20a4
Revises: e4a7b2d9c5f1
Create Date: 2026-08-09 00:00:00.000000

"""

import logging

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c8f3b71d20a4"
down_revision = "e4a7b2d9c5f1"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def _clear_duplicate_source_slugs(bind, scope_col):
    """Keep the earliest row per (scope, source_slug); NULL source_slug on the rest.

    Returns the number of rows cleared, so the caller can log a real number
    rather than claiming success blindly.
    """
    rows = bind.execute(
        sa.text(
            f"SELECT id, {scope_col} AS scope, source_slug "  # noqa: S608 - fixed literal
            "FROM recipe "
            f"WHERE source_slug IS NOT NULL AND {scope_col} IS NOT NULL "
            f"ORDER BY {scope_col}, source_slug, created_at, id"
        )
    ).fetchall()

    seen: set = set()
    cleared = 0
    for row in rows:
        key = (row.scope, row.source_slug)
        if key not in seen:
            seen.add(key)
            continue  # earliest row for this (scope, source_slug) — keep it
        bind.execute(
            sa.text("UPDATE recipe SET source_slug = NULL WHERE id = :id"),
            {"id": row.id},
        )
        cleared += 1
    return cleared


def upgrade():
    bind = op.get_bind()

    # Hold writers out of the gap between the de-dup pass and the index build.
    # Held until this migration's transaction commits, i.e. past index creation.
    # Postgres only: SQLite has no LOCK TABLE and runs single-connection anyway.
    if bind.dialect.name == "postgresql":
        op.execute("LOCK TABLE recipe IN ACCESS EXCLUSIVE MODE")

    cleared = _clear_duplicate_source_slugs(bind, "user_id")
    cleared += _clear_duplicate_source_slugs(bind, "guest_session_id")
    if cleared:
        # Expected to be 0 in production (purged by hand, gates verified empty).
        # A non-zero count here means duplicates appeared after that purge and
        # is worth seeing in the job log rather than passing silently.
        logger.warning(
            "KAN-213: cleared source_slug on %d duplicate recipe row(s) before "
            "creating the unique indexes",
            cleared,
        )

    op.create_index(
        "uq_recipe_user_source_slug",
        "recipe",
        ["user_id", "source_slug"],
        unique=True,
        postgresql_where=sa.text("source_slug IS NOT NULL AND user_id IS NOT NULL"),
        sqlite_where=sa.text("source_slug IS NOT NULL AND user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_recipe_guest_source_slug",
        "recipe",
        ["guest_session_id", "source_slug"],
        unique=True,
        postgresql_where=sa.text("source_slug IS NOT NULL AND guest_session_id IS NOT NULL"),
        sqlite_where=sa.text("source_slug IS NOT NULL AND guest_session_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("uq_recipe_guest_source_slug", table_name="recipe")
    op.drop_index("uq_recipe_user_source_slug", table_name="recipe")
