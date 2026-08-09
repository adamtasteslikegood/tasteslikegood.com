"""Enforce one row per (owner, recipe identity)

KAN-213. Adds two partial unique indexes so the database, not the SPA, refuses
a duplicate saved recipe:

    uq_recipe_user_recipe_identity   UNIQUE (user_id, COALESCE(source_slug, slug))
    uq_recipe_guest_recipe_identity  UNIQUE (guest_session_id, COALESCE(source_slug, slug))
    both WHERE COALESCE(source_slug, slug) IS NOT NULL AND <scope> IS NOT NULL

The key is COALESCE(source_slug, slug), not source_slug alone. A recipe's
identity is `{source_slug, slug}` everywhere else in the codebase —
auth_api_bp._recipe_identity_keys() and the SPA's INV-1
(`r.sourceSlug === slug || r.slug === slug`). Keying on source_slug alone left a
reachable hole, found by Codex review on PR #273: an owner holding the PUBLISHED
row itself (slug='x', source_slug NULL) who saves /r/x again gets a copy with
source_slug='x' that collides with nothing, because the published row sits
outside a source_slug-only partial index. Two rows cannot collide via the slug
side alone -- `slug` is already globally unique -- so every collision this
catches involves at least one saved copy, which is the intent.

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

# A recipe's identity: the public recipe it was saved from, or its own page when
# it is the original. Matches models/recipe.py, _recipe_identity_keys(), and the
# SPA's INV-1 (`r.sourceSlug === slug || r.slug === slug`).
_IDENTITY = "coalesce(source_slug, slug)"


def _clear_blob_key_sql(dialect_name):
    """Dialect-specific SQL to drop ``sourceSlug`` from the ``data`` JSON blob.

    Clearing the column alone is not durable: ``update_recipe`` rebuilds the
    blob as ``{**(recipe.data or {}), **recipe_data}`` and then restages
    ``recipe.source_slug = data.get("sourceSlug")``. A partial PUT that says
    nothing about sourceSlug therefore pulls the stale value back out of the
    untouched blob and writes it to the column — resurrecting the very duplicate
    this pre-pass just cleared, and tripping the new index on the next write.

    ``recipe.data`` is PostgreSQL **json**, not **jsonb** (migration
    b8896f552679 line 37 creates it as ``sa.JSON()``, which SQLAlchemy compiles
    to ``JSON`` on PostgreSQL). The key-deletion ``-`` operator is defined only
    for ``jsonb``, so ``data - 'sourceSlug'`` raises *operator does not exist*
    and aborts the migration — which would abort the deploy. Cast through
    ``jsonb`` and back.

    Worth noting where this would have bitten: the UPDATE only runs when a
    duplicate exists, and production has none. It would have passed in
    production and failed in exactly the dirty environment the pre-pass exists
    to serve. Caught by Copilot review on PR #273.
    """
    if dialect_name == "postgresql":
        return "data = (data::jsonb - 'sourceSlug')::json"
    return "data = json_remove(data, '$.sourceSlug')"


def _clear_duplicate_identities(bind, scope_col):
    """Keep one row per (scope, identity); clear ``source_slug`` on the rest.

    Identity is ``COALESCE(source_slug, slug)`` — the same key the new indexes
    use, and the same one ``_recipe_identity_keys`` and the SPA's INV-1 use.

    Ordering encodes the survivor rule: ``source_slug IS NULL`` first, so the
    author's own published row wins over a saved copy of it, then oldest first.
    That matches runbook §4 rule 2 and is also the only workable choice — the
    loser is resolved by clearing its ``source_slug``, which a slug-derived row
    does not have. Two rows can never collide via the slug side alone, because
    ``slug`` is already globally unique, so a survivor always exists.

    Returns the number of rows cleared so the caller can log a real number
    rather than claiming success blindly.
    """
    rows = bind.execute(
        sa.text(
            f"SELECT id, {scope_col} AS scope, "  # noqa: S608 - fixed literal
            "COALESCE(source_slug, slug) AS identity "
            "FROM recipe "
            f"WHERE COALESCE(source_slug, slug) IS NOT NULL AND {scope_col} IS NOT NULL "
            f"ORDER BY {scope_col}, COALESCE(source_slug, slug), "
            "CASE WHEN source_slug IS NULL THEN 0 ELSE 1 END, created_at, id"
        )
    ).fetchall()

    blob_clear = _clear_blob_key_sql(bind.dialect.name)
    seen: set = set()
    cleared = 0
    for row in rows:
        key = (row.scope, row.identity)
        if key not in seen:
            seen.add(key)
            continue  # survivor for this (scope, identity) — leave it alone
        bind.execute(
            sa.text(  # noqa: S608 - blob_clear is one of two fixed literals
                f"UPDATE recipe SET source_slug = NULL, {blob_clear} WHERE id = :id"
            ),
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

    cleared = _clear_duplicate_identities(bind, "user_id")
    cleared += _clear_duplicate_identities(bind, "guest_session_id")
    if cleared:
        # Expected to be 0 in production (purged by hand, gates verified empty).
        # A non-zero count here means duplicates appeared after that purge and
        # is worth seeing in the job log rather than passing silently.
        logger.warning(
            "KAN-213: cleared source_slug on %d duplicate recipe row(s) before "
            "creating the unique indexes",
            cleared,
        )

    for name, scope in (
        ("uq_recipe_user_recipe_identity", "user_id"),
        ("uq_recipe_guest_recipe_identity", "guest_session_id"),
    ):
        op.create_index(
            name,
            "recipe",
            [sa.text(scope), sa.text(_IDENTITY)],
            unique=True,
            postgresql_where=sa.text(f"{_IDENTITY} IS NOT NULL AND {scope} IS NOT NULL"),
            sqlite_where=sa.text(f"{_IDENTITY} IS NOT NULL AND {scope} IS NOT NULL"),
        )


def downgrade():
    op.drop_index("uq_recipe_guest_recipe_identity", table_name="recipe")
    op.drop_index("uq_recipe_user_recipe_identity", table_name="recipe")
