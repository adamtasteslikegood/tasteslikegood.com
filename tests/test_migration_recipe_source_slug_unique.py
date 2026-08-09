"""Tests for the de-duplication pre-pass in migration c8f3b71d20a4 (KAN-213).

The migration must clear ``source_slug`` on pre-existing duplicate
(scope, source_slug) rows before it can create the unique indexes — otherwise
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
                "source_slug TEXT, created_at TEXT)"
            )
        )
    return engine


def _insert(conn, rows):
    for rid, user_id, guest, source_slug, created in rows:
        conn.execute(
            sa.text(
                "INSERT INTO recipe (id, user_id, guest_session_id, name, source_slug, created_at) "
                "VALUES (:id, :u, :g, 'R', :s, :c)"
            ),
            {"id": rid, "u": user_id, "g": guest, "s": source_slug, "c": created},
        )


def _build_indexes(conn):
    """The statements the migration issues after the pre-pass."""
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_recipe_user_source_slug ON recipe (user_id, source_slug) "
            "WHERE source_slug IS NOT NULL AND user_id IS NOT NULL"
        )
    )
    conn.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_recipe_guest_source_slug "
            "ON recipe (guest_session_id, source_slug) "
            "WHERE source_slug IS NOT NULL AND guest_session_id IS NOT NULL"
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
        cleared = mig._clear_duplicate_source_slugs(conn, "user_id")

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
        assert mig._clear_duplicate_source_slugs(conn, "user_id") == 0
        assert mig._clear_duplicate_source_slugs(conn, "guest_session_id") == 1

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
        cleared = mig._clear_duplicate_source_slugs(conn, "user_id")
        cleared += mig._clear_duplicate_source_slugs(conn, "guest_session_id")

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
