"""Tests for the de-duplication pre-pass in migration c8f3b71d20a4 (KAN-213).

The migration must clear ``source_slug`` on pre-existing duplicate
(scope, COALESCE(source_slug, slug)) rows before it can create the unique
indexes — otherwise
``flask db upgrade`` fails on dirty data, which aborts the flask-backend-migrate
job and blocks the deploy while the old Flask revision keeps serving.

Production was purged by hand on 2026-08-08/09 and both gates returned zero
rows, so this pre-pass is expected to be a no-op there. It is tested precisely
*because* it is expected never to fire in production: an untested pre-pass that
only runs on the one day the data is dirty is the same failure mode as a guard
nobody ever watched fire.

Exercised against a table built WITHOUT the indexes (the model now carries them,
so a normal ``db.create_all`` cannot hold duplicates to test against) — the same
approach as ``test_migration_cookbook_unique.py``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

sys.path.append(str(Path(__file__).resolve().parent.parent))


def _load_migration():
    path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "c8f3b71d20a4_recipe_source_slug_unique_indexes.py"
    )
    spec = importlib.util.spec_from_file_location("mig_c8f3b71d20a4", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bare_recipe_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE recipe ("
                "id TEXT PRIMARY KEY, user_id INTEGER, guest_session_id TEXT, "
                "name TEXT NOT NULL, slug TEXT, is_public INTEGER DEFAULT 0, "
                "source_slug TEXT, data TEXT NOT NULL DEFAULT '{}', created_at TEXT)"
            )
        )
    return engine


def _insert(conn, rows):
    """Rows are (id, user_id, guest_session_id, source_slug, created_at[, slug]).

    The blob mirrors source_slug, as production rows do — the pre-pass has to
    clear both or the next partial PUT restages the stale value.
    """
    for row in rows:
        rid, user_id, guest, source_slug, created = row[:5]
        slug = row[5] if len(row) > 5 else None
        blob = '{"sourceSlug": "%s"}' % source_slug if source_slug else "{}"
        conn.execute(
            sa.text(
                "INSERT INTO recipe (id, user_id, guest_session_id, name, source_slug, "
                "slug, data, created_at) VALUES (:id, :u, :g, 'R', :s, :sl, :d, :c)"
            ),
            {
                "id": rid,
                "u": user_id,
                "g": guest,
                "s": source_slug,
                "sl": slug,
                "d": blob,
                "c": created,
            },
        )


def _build_indexes(conn):
    """The statements the migration issues after the pre-pass."""
    for name, scope in (
        ("uq_recipe_user_recipe_identity", "user_id"),
        ("uq_recipe_guest_recipe_identity", "guest_session_id"),
    ):
        conn.execute(
            sa.text(
                f"CREATE UNIQUE INDEX {name} ON recipe "
                f"({scope}, coalesce(source_slug, slug)) "
                f"WHERE coalesce(source_slug, slug) IS NOT NULL AND {scope} IS NOT NULL"
            )
        )


def test_keeps_earliest_row_and_clears_the_later_duplicates(tmp_path):
    mig = _load_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _insert(
            conn,
            [
                ("a", 1, None, "vegan-cornbread", "2026-01-01"),
                ("b", 1, None, "vegan-cornbread", "2026-01-02"),
                ("c", 1, None, "vegan-cornbread", "2026-01-03"),
                ("d", 2, None, "vegan-cornbread", "2026-01-01"),
            ],
        )
        cleared = mig._clear_duplicate_identities(conn, "user_id")

        kept = conn.execute(
            sa.text("SELECT id FROM recipe WHERE source_slug IS NOT NULL ORDER BY id")
        ).fetchall()

    assert cleared == 2
    # Earliest per (user, source_slug) survives; a different user is untouched.
    assert [r.id for r in kept] == ["a", "d"]

    # The whole point: the indexes can now be built.
    with engine.begin() as conn:
        _build_indexes(conn)


def test_guest_scope_is_deduped_independently(tmp_path):
    """R3 — the guest index needs its own pass; user_id is NULL on those rows."""
    mig = _load_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _insert(
            conn,
            [
                ("g1", None, "sess-a", "vegan-cornbread", "2026-01-01"),
                ("g2", None, "sess-a", "vegan-cornbread", "2026-01-02"),
                ("g3", None, "sess-b", "vegan-cornbread", "2026-01-01"),
            ],
        )
        assert mig._clear_duplicate_identities(conn, "user_id") == 0
        assert mig._clear_duplicate_identities(conn, "guest_session_id") == 1

        kept = conn.execute(
            sa.text("SELECT id FROM recipe WHERE source_slug IS NOT NULL ORDER BY id")
        ).fetchall()

    assert [r.id for r in kept] == ["g1", "g3"]
    with engine.begin() as conn:
        _build_indexes(conn)


def test_clean_data_is_a_no_op(tmp_path):
    """The expected production case: purged already, nothing to do.

    Pinned so the pre-pass cannot start quietly rewriting rows on every deploy.
    """
    mig = _load_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _insert(
            conn,
            [
                ("a", 1, None, "vegan-cornbread", "2026-01-01"),
                ("b", 1, None, "vegan-pot-pie", "2026-01-02"),
                ("c", 1, None, None, "2026-01-03"),
                ("d", 1, None, None, "2026-01-04"),
            ],
        )
        cleared = mig._clear_duplicate_identities(conn, "user_id")
        cleared += mig._clear_duplicate_identities(conn, "guest_session_id")

        remaining = conn.execute(
            sa.text("SELECT count(*) AS n FROM recipe WHERE source_slug IS NOT NULL")
        ).scalar()

    assert cleared == 0
    # NULL-source rows are untouched and stay unconstrained by design.
    assert remaining == 2
    with engine.begin() as conn:
        _build_indexes(conn)


def test_indexes_actually_refuse_a_duplicate_after_the_migration(tmp_path):
    """Seen to fail for the reason it exists.

    An index that is created but never observed refusing anything is the
    project's most expensive recurring failure mode (cf. the Alembic head-check
    and check_sprint_lane.sh before it was wired into gate.needs). This asserts
    the refusal itself, and asserts the two cases that must NOT be refused.
    """
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _build_indexes(conn)
        _insert(conn, [("a", 1, None, "vegan-cornbread", "2026-01-01")])

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            _insert(conn, [("b", 1, None, "vegan-cornbread", "2026-01-02")])

    with engine.begin() as conn:
        # A different owner saving the same public recipe is the product working.
        _insert(conn, [("c", 2, None, "vegan-cornbread", "2026-01-02")])
        # Generated recipes carry no source_slug and stay unconstrained.
        _insert(conn, [("d", 1, None, None, "2026-01-03")])
        _insert(conn, [("e", 1, None, None, "2026-01-04")])
        assert conn.execute(sa.text("SELECT count(*) FROM recipe")).scalar() == 4


def test_published_original_wins_over_a_saved_copy_of_itself(tmp_path):
    """The identity case Codex found on PR #273.

    An owner who holds the published row itself (``slug='x'``,
    ``source_slug`` NULL) and also a copy saved from ``/r/x``
    (``source_slug='x'``) has two rows for one recipe. Keying on
    ``source_slug`` alone missed it entirely — the published row sits outside
    that index. Under ``COALESCE(source_slug, slug)`` both rows share a key.

    The survivor must be the published original, regardless of dates: the loser
    is resolved by clearing its ``source_slug``, and a row whose key comes from
    ``slug`` has none to clear. Ordering encodes that, which is why the copy
    here is deliberately the OLDER row.
    """
    mig = _load_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _insert(
            conn,
            [
                ("copy", 1, None, "vegan-cornbread", "2026-01-01"),
                ("published", 1, None, None, "2026-01-02", "vegan-cornbread"),
            ],
        )
        cleared = mig._clear_duplicate_identities(conn, "user_id")

        rows = dict(conn.execute(sa.text("SELECT id, source_slug FROM recipe")).fetchall())
        blob = conn.execute(sa.text("SELECT data FROM recipe WHERE id = 'copy'")).scalar()

    assert cleared == 1
    assert rows["published"] is None, "the published original must be untouched"
    assert rows["copy"] is None, "the saved copy's source_slug must be cleared"
    assert "sourceSlug" not in blob, (
        "the mirrored blob key must be cleared too, or the next partial PUT "
        "restages the stale value onto the column"
    )

    with engine.begin() as conn:
        _build_indexes(conn)


def test_single_pass_misses_a_chained_collision_that_looping_resolves(tmp_path):
    """KAN-223: clearing a loser can expose a new collision the snapshot never re-checks.

    Same user scope, three rows:
      A: source_slug='orig'              (a saved copy pointing at /r/orig)
      B: source_slug='orig', slug='copy' (a saved copy of /r/orig, later
                                           published at its own /r/copy)
      C: source_slug='copy'              (a saved copy pointing at /r/copy — i.e. of B)

    One call groups strictly by the pre-call snapshot: {A, B} collide on
    identity 'orig' (B loses, cleared), {C} is alone on 'copy' — the snapshot
    was taken before B's clear, so it never saw that B's post-clear identity
    (COALESCE(NULL, 'copy') = 'copy') now collides with C. Building the
    indexes on that residual state still fails.

    A second call (fresh SELECT) sees B's now-NULL source_slug and correctly
    re-groups {B, C} on 'copy' — and B rightly wins: it is the actual page at
    /r/copy (case=0, NULL-source first), C is a copy of it (case=1). Looping
    per scope until a call clears nothing (``upgrade()``'s fix) reaches this
    fixed point in general, not just for this one extra round.
    """
    mig = _load_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _insert(
            conn,
            [
                ("a", 1, None, "orig", "2026-01-01"),
                ("b", 1, None, "orig", "2026-01-02", "copy"),
                ("c", 1, None, "copy", "2026-01-03"),
            ],
        )
        first_pass = mig._clear_duplicate_identities(conn, "user_id")
        residual = conn.execute(
            sa.text(
                "SELECT count(*) FROM ("
                "  SELECT coalesce(source_slug, slug) AS identity, count(*) AS n"
                "  FROM recipe WHERE user_id = 1 GROUP BY identity"
                ") t WHERE t.n > 1"
            )
        ).scalar()

    assert first_pass == 1, "one pass only resolves the collision the pre-call snapshot saw"
    assert residual == 1, (
        "a single pass leaves B's newly-exposed collision with C unresolved — "
        "this is the bug: building the indexes here would still fail"
    )

    # The fix: loop the same call, per scope, until it clears nothing. Calls
    # the exact helper upgrade() calls (Copilot review on PR #277) — a
    # hand-rolled loop here wouldn't notice if upgrade() regressed to a
    # single pass.
    with engine.begin() as conn:
        mig._clear_scope_to_fixed_point(conn, "user_id")
        rows = dict(conn.execute(sa.text("SELECT id, source_slug FROM recipe")).fetchall())

    assert rows["a"] == "orig", "A is untouched — it won its group outright"
    assert rows["b"] is None, "B lost 'orig' to A, then won 'copy' outright (it's the real page)"
    assert rows["c"] is None, "C lost 'copy' to B once B's post-clear identity was re-evaluated"

    # The whole point: the indexes can now be built without a residual collision.
    with engine.begin() as conn:
        _build_indexes(conn)


def test_index_refuses_a_copy_of_your_own_published_recipe(tmp_path):
    """The refusal itself, for the identity case — seen to fire."""
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _build_indexes(conn)
        _insert(conn, [("published", 1, None, None, "2026-01-01", "vegan-cornbread")])

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            _insert(conn, [("copy", 1, None, "vegan-cornbread", "2026-01-02")])

    with engine.begin() as conn:
        # Another owner saving that same public recipe is still fine.
        _insert(conn, [("other", 2, None, "vegan-cornbread", "2026-01-02")])
        assert conn.execute(sa.text("SELECT count(*) FROM recipe")).scalar() == 2


def test_postgres_blob_clear_casts_through_jsonb():
    """`recipe.data` is PostgreSQL json, not jsonb — the `-` operator is jsonb-only.

    SQLite cannot catch this: the whole suite runs on SQLite, where the blob
    clear is `json_remove`. On PostgreSQL, `data - 'sourceSlug'` raises
    *operator does not exist* and aborts the migration, taking the deploy with
    it (migration b8896f552679 creates `data` as `sa.JSON()`, which SQLAlchemy
    compiles to `JSON`).

    Worse, the UPDATE only runs when a duplicate exists. Production has none, so
    it would have passed there and failed in exactly the dirty environment the
    pre-pass exists to serve — a guard broken only on the day it is needed.
    Asserting the emitted SQL is weak coverage, but it is the coverage available
    without a PostgreSQL fixture, and it fails if someone drops the casts.
    """
    mig = _load_migration()

    pg = mig._clear_blob_key_sql("postgresql")
    assert (
        "::jsonb" in pg and "::json" in pg
    ), f"PostgreSQL blob clear must cast through jsonb, got: {pg}"
    assert "- 'sourceSlug'" in pg

    assert mig._clear_blob_key_sql("sqlite") == "data = json_remove(data, '$.sourceSlug')"
