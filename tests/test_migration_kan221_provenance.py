"""Tests executing the KAN-221 migration backfills (Copilot B3 on PR #279).

Two migrations carry KAN-221's schema split, and both ship data backfills that
run exactly once, unattended, inside the flask-backend-migrate Cloud Run Job:

    d680a4b61194  adds user_id_author / user_id_saved_to and backfills them
    a3c9e1f4b7d2  adds source_recipe_id, backfills it from source_slug, and
                  re-keys the KAN-213 identity indexes onto it

An untested backfill is the project's named failure mode: code that runs only
on the one day it matters, with nobody watching. These tests execute the REAL
backfill helpers the migrations call — not a re-implementation, and not
``db.create_all()`` — against a bare table, the pattern set by
``test_migration_recipe_source_slug_unique.py``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

sys.path.append(str(Path(__file__).resolve().parent.parent))

_VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _author_saver_migration():
    return _load("d680a4b61194_add_user_id_author_and_user_id_saved_to_.py", "mig_d680a4b61194")


def _source_recipe_id_migration():
    return _load("a3c9e1f4b7d2_add_source_recipe_id_and_rekey_identity.py", "mig_a3c9e1f4b7d2")


def _bare_recipe_engine(tmp_path):
    """The recipe table as it stands when each backfill runs (post column-add,
    pre index re-key), without the model's indexes so dirty states can be
    staged freely."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig221.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE recipe ("
                "id TEXT PRIMARY KEY, user_id INTEGER, guest_session_id TEXT, "
                "name TEXT NOT NULL, slug TEXT, is_public INTEGER DEFAULT 0, "
                "source_slug TEXT, source_recipe_id TEXT, "
                "user_id_author INTEGER, user_id_saved_to INTEGER, "
                "data TEXT NOT NULL DEFAULT '{}', created_at TEXT)"
            )
        )
    return engine


def _insert(conn, rows):
    """Rows are dicts; unset keys default to NULL/0."""
    for row in rows:
        params = {
            "id": row["id"],
            "user_id": row.get("user_id"),
            "guest": row.get("guest_session_id"),
            "slug": row.get("slug"),
            "public": 1 if row.get("is_public") else 0,
            "source_slug": row.get("source_slug"),
            "source_recipe_id": row.get("source_recipe_id"),
        }
        conn.execute(
            sa.text(
                "INSERT INTO recipe (id, user_id, guest_session_id, name, slug, "
                "is_public, source_slug, source_recipe_id, data, created_at) "
                "VALUES (:id, :user_id, :guest, 'R', :slug, :public, "
                ":source_slug, :source_recipe_id, '{}', '2026-01-01')"
            ),
            params,
        )


def _rows(conn, columns):
    cols = ", ".join(("id",) + columns)
    fetched = conn.execute(sa.text(f"SELECT {cols} FROM recipe")).fetchall()  # noqa: S608
    return {row.id: tuple(row[1:]) for row in fetched}


# ─── d680a4b61194: user_id_author / user_id_saved_to backfill ────────────────


def test_author_saver_backfill_covers_all_three_row_shapes(tmp_path):
    """Originals, resolved copies, and orphans — the three UPDATE passes."""
    mig = _author_saver_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _insert(
            conn,
            [
                # An original the author published.
                {"id": "src", "user_id": 1, "slug": "chili", "is_public": True},
                # A copy of it another user saved.
                {"id": "copy", "user_id": 2, "source_slug": "chili"},
                # An unpublished original (never had a page).
                {"id": "draft", "user_id": 2},
                # A copy whose source is gone.
                {"id": "orphan", "user_id": 3, "source_slug": "deleted-recipe"},
            ],
        )
        mig._backfill_author_saver(conn)
        rows = _rows(conn, ("user_id_author", "user_id_saved_to"))

    assert rows["src"] == (1, None), "original: author = owner, no saver"
    assert rows["copy"] == (1, 2), "copy: author = SOURCE owner, saved_to = holder"
    assert rows["draft"] == (2, None), "unpublished original: still its owner's work"
    assert rows["orphan"] == (None, 3), "orphan: author unknown stays NULL, saver set"


def test_author_saver_backfill_ignores_unpublished_sources(tmp_path):
    """The resolution requires the source to be PUBLIC — a private row with a
    matching slug must not be claimed as the author (mirrors the runtime
    resolution, which only ever sees /r/<slug> pages)."""
    mig = _author_saver_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _insert(
            conn,
            [
                {"id": "private", "user_id": 1, "slug": "chili", "is_public": False},
                {"id": "copy", "user_id": 2, "source_slug": "chili"},
            ],
        )
        mig._backfill_author_saver(conn)
        rows = _rows(conn, ("user_id_author", "user_id_saved_to"))

    assert rows["copy"] == (None, 2), "a non-public slug match is treated as an orphan"


# ─── a3c9e1f4b7d2: source_recipe_id backfill + index re-key ──────────────────


def _build_v2_indexes(conn, mig):
    """The statements the migration issues after the backfill — expression
    taken from the migration module itself so this test cannot drift from
    what production runs."""
    for name, scope in mig._INDEXES:
        conn.execute(
            sa.text(
                f"CREATE UNIQUE INDEX {name} ON recipe "
                f"({scope}, {mig._IDENTITY}) "
                f"WHERE {scope} IS NOT NULL"
            )
        )


def test_source_recipe_id_backfill_resolves_public_sources_only(tmp_path):
    mig = _source_recipe_id_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _insert(
            conn,
            [
                {"id": "src", "user_id": 1, "slug": "chili", "is_public": True},
                {"id": "copy", "user_id": 2, "source_slug": "chili"},
                {"id": "guest-copy", "guest_session_id": "sess-a", "source_slug": "chili"},
                {"id": "orphan", "user_id": 3, "source_slug": "deleted-recipe"},
                {"id": "private", "user_id": 1, "slug": "secret", "is_public": False},
                {"id": "copy-of-private", "user_id": 2, "source_slug": "secret"},
                {"id": "original", "user_id": 2},
            ],
        )
        mig._backfill_source_recipe_id(conn)
        rows = _rows(conn, ("source_recipe_id", "source_slug"))

    assert rows["copy"] == ("src", "chili"), "resolved: key moves to the source id"
    assert rows["guest-copy"] == ("src", "chili"), "guest copies resolve identically"
    assert rows["orphan"] == (None, "deleted-recipe"), "unresolvable: slug pointer kept"
    assert rows["copy-of-private"] == (None, "secret"), "non-public source does not resolve"
    assert rows["original"] == (None, None), "originals are untouched"


def test_rekeyed_index_refuses_the_id_keyed_duplicates(tmp_path):
    """Seen to fail for the reason it exists — under the NEW key.

    The two collisions the re-key must refuse: a second resolved copy of the
    same source, and the source row's own holder saving it again (the Codex
    #273 identity case, now id-keyed instead of slug-keyed).
    """
    mig = _source_recipe_id_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _build_v2_indexes(conn, mig)
        _insert(
            conn,
            [
                {"id": "src", "user_id": 1, "slug": "chili", "is_public": True},
                {"id": "copy", "user_id": 2, "source_slug": "chili", "source_recipe_id": "src"},
            ],
        )

    # A second resolved copy under the same owner.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            _insert(
                conn,
                [{"id": "copy2", "user_id": 2, "source_slug": "chili", "source_recipe_id": "src"}],
            )

    # The author saving their own published page: copy keys on source id,
    # the source row keys on its own id — same key, same owner, refused.
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            _insert(
                conn,
                [{"id": "self", "user_id": 1, "source_slug": "chili", "source_recipe_id": "src"}],
            )

    with engine.begin() as conn:
        # A different owner saving the same recipe is the product working.
        _insert(
            conn,
            [{"id": "other", "user_id": 3, "source_slug": "chili", "source_recipe_id": "src"}],
        )
        # Authored originals stay unconstrained (their own ids never collide).
        _insert(conn, [{"id": "a", "user_id": 2}, {"id": "b", "user_id": 2}])
        assert conn.execute(sa.text("SELECT count(*) FROM recipe")).scalar() == 5


def test_rekeyed_index_keeps_the_slug_fallback_for_unresolved_copies(tmp_path):
    """KAN-213's original dedup survives for copies whose source never resolved."""
    mig = _source_recipe_id_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _build_v2_indexes(conn, mig)
        _insert(conn, [{"id": "c1", "user_id": 1, "source_slug": "gone"}])

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            _insert(conn, [{"id": "c2", "user_id": 1, "source_slug": "gone"}])


def test_data_satisfying_the_old_key_builds_the_new_indexes(tmp_path):
    """The migration ships NO de-dup pre-pass, on the argument that every group
    the new key forms is a subset of a group the old key formed — so data the
    c8f3b71d20a4 indexes accepted cannot violate the re-keyed ones. This stages
    the shapes that satisfy the OLD constraint and proves backfill + index
    build succeed on them.
    """
    mig = _source_recipe_id_migration()
    engine = _bare_recipe_engine(tmp_path)
    with engine.begin() as conn:
        _insert(
            conn,
            [
                # Author's published original; two other owners each hold a copy.
                {"id": "src", "user_id": 1, "slug": "chili", "is_public": True},
                {"id": "u2-copy", "user_id": 2, "source_slug": "chili"},
                {"id": "u3-copy", "user_id": 3, "source_slug": "chili"},
                # A guest copy of the same page.
                {"id": "g-copy", "guest_session_id": "sess-a", "source_slug": "chili"},
                # Orphaned copies of the same dead slug, one per owner.
                {"id": "u2-orphan", "user_id": 2, "source_slug": "gone"},
                {"id": "u3-orphan", "user_id": 3, "source_slug": "gone"},
                # Plain authored rows.
                {"id": "u2-draft", "user_id": 2},
                {"id": "u2-page", "user_id": 2, "slug": "tacos", "is_public": True},
            ],
        )
        mig._backfill_source_recipe_id(conn)
        _build_v2_indexes(conn, mig)

        resolved = _rows(conn, ("source_recipe_id",))

    assert resolved["u2-copy"] == ("src",)
    assert resolved["u3-copy"] == ("src",)
    assert resolved["g-copy"] == ("src",)
    assert resolved["u2-orphan"] == (None,)
