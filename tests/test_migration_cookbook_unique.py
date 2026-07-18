"""Tests for the cookbook-name de-duplication step in migration b7e2a9c4d1f8.

The migration must rename pre-existing duplicate (scope, name) rows before it can
create the unique index — otherwise ``flask db upgrade`` fails on dirty prod data
and blocks the deploy. This exercises the rename helper directly against a table
built WITHOUT the unique index (the model now carries it, so a normal
``db.create_all`` cannot hold duplicates to test against).
"""

import importlib.util
import sys
from pathlib import Path

import sqlalchemy as sa

sys.path.append(str(Path(__file__).resolve().parent.parent))


def _load_migration():
    path = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "b7e2a9c4d1f8_cookbook_name_unique_indexes.py"
    )
    spec = importlib.util.spec_from_file_location("mig_b7e2a9c4d1f8", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bare_cookbook_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE cookbook ("
                "id TEXT PRIMARY KEY, user_id INTEGER, guest_session_id TEXT, "
                "name TEXT NOT NULL, created_at TEXT)"
            )
        )
    return engine


def test_dedupe_renames_user_duplicates_keeping_earliest(tmp_path):
    mig = _load_migration()
    engine = _bare_cookbook_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO cookbook (id, user_id, name, created_at) VALUES "
                "('a', 1, 'Dooypkiitts', '2026-01-01'),"
                "('b', 1, 'Dooypkiitts', '2026-01-02'),"
                "('c', 1, 'Dooypkiitts', '2026-01-03'),"
                "('d', 2, 'Dooypkiitts', '2026-01-01')"
            )
        )
        mig._dedupe_scope(conn, "user_id")
        u1 = conn.execute(
            sa.text("SELECT name FROM cookbook WHERE user_id=1 ORDER BY created_at")
        ).fetchall()
        u2 = conn.execute(sa.text("SELECT name FROM cookbook WHERE user_id=2")).fetchall()

    # Earliest kept; later collisions suffixed.
    assert [r.name for r in u1] == ["Dooypkiitts", "Dooypkiitts (2)", "Dooypkiitts (3)"]
    # A different user's identical name is untouched.
    assert [r.name for r in u2] == ["Dooypkiitts"]

    # The unique index can now be built without error.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE UNIQUE INDEX uq_cookbook_user_name ON cookbook (user_id, name) "
                "WHERE user_id IS NOT NULL"
            )
        )


def test_dedupe_handles_guests_and_avoids_new_collisions(tmp_path):
    mig = _load_migration()
    engine = _bare_cookbook_engine(tmp_path)
    with engine.begin() as conn:
        # Two guest dupes of "Book", plus a pre-existing "Book (2)" that the
        # naive suffix would collide with — the helper must skip past it.
        conn.execute(
            sa.text(
                "INSERT INTO cookbook (id, guest_session_id, name, created_at) VALUES "
                "('a', 'g1', 'Book', '2026-01-01'),"
                "('b', 'g1', 'Book', '2026-01-02'),"
                "('c', 'g1', 'Book (2)', '2026-01-03')"
            )
        )
        mig._dedupe_scope(conn, "guest_session_id")
        names = conn.execute(
            sa.text("SELECT name FROM cookbook WHERE guest_session_id='g1' ORDER BY created_at")
        ).fetchall()

    resolved = [r.name for r in names]
    assert resolved[0] == "Book"  # earliest kept
    assert resolved[2] == "Book (2)"  # the pre-existing distinct name kept
    assert resolved[1] == "Book (3)"  # renamed past the collision
    assert len(set(resolved)) == 3  # all unique now
