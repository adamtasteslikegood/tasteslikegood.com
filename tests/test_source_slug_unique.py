"""KAN-213 — the duplicate invariant lives in the database, not in the client.

Six duplicate-recipe tickets were filed and fixed between 2026-07-18 and
2026-08-08 (KAN-137, -156, -157, -186, -194). Every one patched the layer the
symptom appeared in; none touched the schema, so the bug kept coming back. The
uniqueness rule (INV-1) lived only in SPA code
(``src/services/ssr-entry.service.ts``), and a client-side check cannot close a
cross-context race by construction: two tabs, or a tab and a phone, both read
"you don't have this yet" before either writes.

These tests pin the fix at the only layer that can refuse the second write —
two partial unique indexes (re-keyed by KAN-221 / migration a3c9e1f4b7d2 from
COALESCE(source_slug, slug) onto the stable source-recipe id):

    uq_recipe_user_recipe_identity
        UNIQUE (user_id, COALESCE(source_recipe_id, source_slug, id))
        WHERE user_id IS NOT NULL
    uq_recipe_guest_recipe_identity
        UNIQUE (guest_session_id, COALESCE(source_recipe_id, source_slug, id))
        WHERE guest_session_id IS NOT NULL

Both ship together or neither (Sprint 6 R3): ``user_id`` is nullable and guests
key on ``guest_session_id``, which is the KAN-186 path.

The race is injected deterministically rather than with threads — the same
idiom as ``test_publish_gate.py``'s slug-race tests. A thread race would
reproduce this interleaving only sometimes; monkeypatching the commit boundary
reproduces it every run, and the competing row is written on a *separate
connection* so it is a genuine second writer, not a same-session artifact.
"""

import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402

SOURCE_SLUG = "vegan-cornbread"
GUEST_SESSION = "guest-session-abc"


@pytest.fixture
def app(tmp_path):
    # File-backed rather than :memory: — the race test opens a SECOND
    # connection to play the competing writer, and separate connections to an
    # in-memory SQLite database do not necessarily see the same data.
    app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'kan213.db'}",
        WTF_CSRF_ENABLED=False,
    )
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user(app):
    u = User(email="owner@example.com", name="Owner")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def logged_in(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id
    return user


@pytest.fixture
def guest(client):
    # The app keys the guest scope on session["session_id"]
    # (utils/session_utils.get_or_create_session_id), NOT "guest_session_id" —
    # which is the name it carries once it reaches the model. Setting the wrong
    # key mints a fresh random id instead and the test silently passes against
    # an unconstrained pair.
    with client.session_transaction() as sess:
        sess["session_id"] = GUEST_SESSION
    return GUEST_SESSION


def _payload(name="Vegan Cornbread", source_slug=SOURCE_SLUG):
    """The body the SPA sends when saving a public recipe to a cookbook."""
    body = {"name": name, "ingredients": ["cornmeal"], "instructions": ["bake"]}
    if source_slug is not None:
        body["sourceSlug"] = source_slug
    return body


def _insert_competing_row(user_id=None, guest_session_id=None, source_slug=SOURCE_SLUG):
    """Write the other tab's row on a SEPARATE connection.

    Deliberately raw SQL on its own connection rather than ``db.session.add``:
    the point of the test is a second writer that our session has not seen, and
    staging it on the shared session would prove nothing about the index.
    """
    with db.engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO recipe (id, user_id, guest_session_id, name, status, "
                "slug, is_public, is_canonical, source_slug, data, created_at, updated_at) "
                "VALUES (:id, :user_id, :guest, :name, 'ready', NULL, 0, 0, :source_slug, "
                ":data, '2026-08-09 00:00:00', '2026-08-09 00:00:00')"
            ),
            {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "guest": guest_session_id,
                "name": "Vegan Cornbread",
                "source_slug": source_slug,
                "data": '{"name": "Vegan Cornbread"}',
            },
        )


def _race_on_commit(monkeypatch, **competitor):
    """Land the competing row once, immediately before our first commit."""
    real_commit = db.session.commit
    state = {"raced": False}

    def racing_commit():
        if not state["raced"]:
            state["raced"] = True
            _insert_competing_row(**competitor)
        return real_commit()

    monkeypatch.setattr(db.session, "commit", racing_commit)
    return state


# ─── The anchor gate (Sprint 6 D1) ───────────────────────────────────────────


def test_concurrent_saves_of_same_source_slug_persist_one_row_and_return_409(
    client, logged_in, monkeypatch
):
    """Two concurrent POSTs with the same sourceSlug: one row, and the loser
    gets a 409 — not a 500.

    This is Sprint 6's definition of done. The 409 half is as load-bearing as
    the one-row half: R1 named "the new constraint surfaces as a raw 500" as
    the dominant risk, because the repository catches IntegrityError broadly and
    returns None, which the blueprint answers with 500. A 500 tells the SPA to
    show "check your connection" for what is really "you already have this".
    """
    state = _race_on_commit(monkeypatch, user_id=logged_in.id)

    response = client.post("/api/recipes", json=_payload())

    assert state["raced"], "the competing writer never ran — test is not exercising the race"
    assert response.status_code == 409, (
        f"expected 409 (deliberate refusal), got {response.status_code}. "
        "A 500 here is R1: the constraint fired but the API reported an internal error."
    )

    rows = Recipe.query.filter_by(user_id=logged_in.id, source_slug=SOURCE_SLUG).all()
    assert len(rows) == 1, f"expected exactly one persisted row, found {len(rows)}"


def test_duplicate_refusal_body_carries_a_code_and_no_exception_text(
    client, logged_in, monkeypatch
):
    """The refusal must be actionable by the SPA and must not leak internals.

    Same rule as the sibling refusals (KAN-155): a fixed message plus a machine
    -readable ``code``, never ``str(e)`` — exception text must not reach clients
    (CodeQL py/stack-trace-exposure).
    """
    _race_on_commit(monkeypatch, user_id=logged_in.id)

    body = client.post("/api/recipes", json=_payload()).get_json()

    assert "code" in body, "SPA cannot distinguish this refusal without a code"
    for leak in ("Traceback", "IntegrityError", "UNIQUE constraint", "INSERT INTO"):
        assert leak not in str(body), f"response leaked internals: {leak!r}"


def test_update_that_collides_also_returns_409_not_500(client, logged_in):
    """The PUT route must refuse the same way the POST route does.

    Covers the sibling path of R1: the repository re-raises
    RecipeDuplicateError from update_recipe, so without a handler on the PUT
    route the refusal falls into the generic 500. Unlike the create case this
    needs no race — editing one recipe to carry another's sourceSlug trips the
    index directly.
    """
    first = client.post("/api/recipes", json=_payload()).get_json()
    second = client.post(
        "/api/recipes", json=_payload(name="Vegan Pot Pie", source_slug="pot-pie")
    ).get_json()
    assert first["id"] != second["id"]

    # Point the second recipe at the first's source — now a duplicate pair.
    response = client.put(f"/api/recipes/{second['id']}", json=_payload(name="Vegan Pot Pie"))

    assert response.status_code == 409, (
        f"expected 409, got {response.status_code} — an update that trips the "
        "index must be a deliberate refusal, not an internal error"
    )
    assert "code" in response.get_json()
    # The write must not have landed.
    assert Recipe.query.filter_by(user_id=logged_in.id, source_slug=SOURCE_SLUG).count() == 1


def test_publish_of_saved_copy_is_refused_before_slug_resolution_can_race(
    client, logged_in, monkeypatch
):
    """RCP-74 made the PR #273 slug-race x duplicate interaction unreachable.

    HISTORY: this test used to prove that a write which lost a slug race AND
    carried a duplicate source_slug still ended in 409 rather than burning all
    retries into a raw 500 (a disagreement between two #273 reviews; the answer
    was "one extra retry, then 409"). That interleaving required *publishing* a
    row that carries a sourceSlug — exactly what the RCP-74 guard now refuses
    up front, so the scenario the old test name promised can no longer execute
    (Copilot B4 on PR #279).

    The interaction is structurally gone, not just untested:

    - Publishing is the only path into ``_resolve_public_slug``, and
      ``_gate_is_public`` raises before ``_commit_publish_retrying`` is ever
      entered when the write carries provenance (persisted or staged). No
      publish, no slug probe, no slug race.
    - A publishable row (no provenance) cannot trip the duplicate index at
      all under the KAN-221 key: it is indexed by its own unique id, so the
      slug-race retry loop and the duplicate refusal no longer share a
      reachable write.

    What this test now pins is the boundary itself: the refusal fires BEFORE
    slug resolution runs, the response is the deliberate 403 (not a 500 or a
    retry storm), and neither the competing row nor its provenance is touched.
    """
    from repositories import db_recipe_repository

    _insert_competing_row(user_id=logged_in.id)  # owner already holds SOURCE_SLUG

    real_resolve = db_recipe_repository._resolve_public_slug
    state = {"resolves": 0}

    def counting_resolve(data, recipe_id, current_slug=None, skip=frozenset()):
        state["resolves"] += 1
        return real_resolve(data, recipe_id, current_slug, skip=skip)

    monkeypatch.setattr(db_recipe_repository, "_resolve_public_slug", counting_resolve)

    response = client.post("/api/recipes", json={**_payload(), "is_public": True})

    assert (
        response.status_code == 403
    ), f"expected 403 (saved copy cannot publish, RCP-74), got {response.status_code}"
    assert state["resolves"] == 0, (
        "slug resolution ran — the guard must refuse before "
        "_commit_publish_retrying can enter the slug-race window"
    )
    rows = Recipe.query.filter_by(user_id=logged_in.id, source_slug=SOURCE_SLUG).all()
    assert len(rows) == 1, "the refused write must not have persisted anything"


def test_guest_saves_are_constrained_too(client, guest, monkeypatch):
    """R3 — both indexes ship together or neither.

    Guests key on ``guest_session_id`` with ``user_id`` NULL, so the
    authenticated index does not cover them. This is the KAN-186 path, and
    leaving it unconstrained would reopen the bug for exactly the users who hit
    it most: someone saving a recipe before signing in.
    """
    state = _race_on_commit(monkeypatch, guest_session_id=guest)

    response = client.post("/api/recipes", json=_payload())

    assert state["raced"]
    assert response.status_code == 409
    rows = Recipe.query.filter_by(guest_session_id=guest, source_slug=SOURCE_SLUG).all()
    assert len(rows) == 1


# ─── The constraint must be narrow: these are NOT duplicates ─────────────────


def test_two_users_may_each_save_the_same_public_recipe(client, logged_in, monkeypatch):
    """The index is scoped per owner. Two people saving the same public recipe
    is the product working, not a duplicate — a global unique index on
    source_slug would break the core save flow."""
    other = User(email="other@example.com", name="Other")
    db.session.add(other)
    db.session.commit()
    _insert_competing_row(user_id=other.id)

    response = client.post("/api/recipes", json=_payload())

    assert response.status_code == 201
    assert Recipe.query.filter_by(source_slug=SOURCE_SLUG).count() == 2


def test_generated_recipes_are_unconstrained(client, logged_in):
    """The documented coverage limit, pinned so it cannot regress silently.

    Both indexes are partial on ``source_slug IS NOT NULL``, and only
    ``origin='saved'`` rows carry one — so they constrain copies a user took
    from someone else's public page and **do not constrain a single recipe a
    user authored** (~3% of user 1's rows: 4 constrained against 112 not).

    That is deliberate, not an oversight. A saved copy is the only case where
    "these two rows are the same recipe" is a machine-checkable fact; two
    separately generated recipes have no such identity, and a name-based
    constraint was rejected because two genuinely different recipes may share a
    title.

    So KAN-213's class is closed, and the table is NOT duplicate-free. If this
    test ever fails, someone widened the constraint beyond what was agreed —
    most likely onto authored recipes, where it cannot be correct without a
    content hash (Sprint 7).
    """
    first = client.post("/api/recipes", json=_payload(source_slug=None))
    second = client.post("/api/recipes", json=_payload(source_slug=None))

    assert (first.status_code, second.status_code) == (201, 201)
    assert Recipe.query.filter_by(source_slug=None).count() == 2


def test_same_user_may_save_two_different_public_recipes(client, logged_in):
    """Sanity floor: the constraint keys on (owner, source_slug), not owner."""
    assert client.post("/api/recipes", json=_payload()).status_code == 201
    second = client.post("/api/recipes", json=_payload(name="Vegan Pot Pie", source_slug="pot-pie"))

    assert second.status_code == 201
    assert Recipe.query.filter_by(user_id=logged_in.id).count() == 2


# ─── KAN-213 × KAN-186: the guest-merge public-row exemption ─────────────────


def test_guest_cannot_create_the_row_that_would_collide(client, guest):
    """The reason the merge exemption is not a live conflict with this index.

    KAN-186's merge deletes a guest row that duplicates an owned recipe —
    *except* when the guest row is published, which is reassigned instead
    because deleting would take a live page down. Reassigning a duplicate is
    exactly what the new index refuses, so that reads like a conflict.

    It is not, because **a guest cannot publish at all.** The SPA replaces the
    publish toggle with a "log in to publish" link, and the server does not
    trust the SPA: ``_gate_is_public`` forces ``is_public=False`` whenever
    ``user_id is None``. Asserted here through the API rather than the ORM —
    constructing the row directly with ``Recipe(is_public=True, ...)`` bypasses
    the gate and would "prove" a state the product cannot reach.
    """
    response = client.post("/api/recipes", json={**_payload(), "is_public": True})

    assert response.status_code == 201
    row = Recipe.query.filter_by(guest_session_id=guest).one()
    assert row.is_public is False, "a guest published a recipe — the gate is gone"
    assert row.data["is_public"] is False, "blob and column must not disagree"


def test_legacy_public_guest_row_cannot_roll_back_a_login_merge(app, user):
    """Defence for rows predating the publish gate — not a live path.

    Migration e91b47a2c5d3 (2026-07-07) reassigned or unpublished the
    guest-published rows that existed before ``_gate_is_public``, so production
    holds none. This builds one directly through the ORM *on purpose*, to prove
    what happens if one ever reappears: without the ``source_slug`` clear, the
    reassignment raises IntegrityError, which rolls back the ENTIRE merge and
    orphans the guest's recipes and cookbooks at the moment of login.

    A single legacy row must not be able to cost someone their data.
    """
    from blueprints.auth_api_bp import _merge_guest_session_into_user

    owned = Recipe(
        id="owned",
        user_id=user.id,
        name="Pizza Dough",
        source_slug="vegan-fried-pizza-dough",
        is_public=False,
        data={"name": "Pizza Dough"},
    )
    published_guest_copy = Recipe(
        id="guest-published",
        guest_session_id=GUEST_SESSION,
        name="Pizza Dough",
        slug="vegan-fried-pizza-dough-2",
        source_slug="vegan-fried-pizza-dough",
        is_public=True,
        data={"name": "Pizza Dough"},
    )
    db.session.add_all([owned, published_guest_copy])
    db.session.commit()

    _merge_guest_session_into_user(user, GUEST_SESSION)

    rows = Recipe.query.filter_by(user_id=user.id).all()
    assert len(rows) == 2, "the live public page was dropped by the merge"
    survivor = next(r for r in rows if r.id == "guest-published")
    assert survivor.is_public is True, "the public page must stay live"
    assert survivor.slug == "vegan-fried-pizza-dough-2", "its own address is unchanged"
    assert survivor.source_slug is None, (
        "the reassigned row must leave the partial index's coverage, "
        "or the whole merge rolls back at login"
    )


# ─── Codex review findings, PR #273 — reproduced before fixing ──────────────


def test_clearing_the_column_survives_a_later_partial_update(client, logged_in):
    """P2 — the column clear must not be undone by the mirrored JSON blob.

    ``source_slug`` is a mirror of ``data['sourceSlug']``. Both the migration
    pre-pass and the guest-merge legacy guard clear only the **column**.
    ``update_recipe`` then rebuilds the blob as
    ``{**(recipe.data or {}), **recipe_data}`` and ``stage_update`` restages
    ``recipe.source_slug = data.get('sourceSlug')`` — so the next ordinary
    partial PUT pulls the stale value back out of the untouched blob and writes
    it to the column.

    That resurrects the duplicate the clear just removed, and the row then trips
    the unique index on a write the user did not intend as a save.
    """
    created = client.post("/api/recipes", json=_payload()).get_json()
    row = db.session.get(Recipe, created["id"])
    assert row.data.get("sourceSlug") == SOURCE_SLUG, "precondition: blob mirrors the column"

    # What the migration pre-pass and the merge guard do today.
    row.source_slug = None
    db.session.commit()

    # An ordinary partial PUT that says nothing about sourceSlug.
    response = client.put(f"/api/recipes/{created['id']}", json={"name": "Renamed"})
    assert response.status_code == 200

    refreshed = db.session.get(Recipe, created["id"])
    assert refreshed.source_slug is None, (
        "the stale data['sourceSlug'] was restaged onto the column — the clear "
        "is not durable, so the migration pre-pass undoes itself on the next PUT"
    )


def test_saving_your_own_published_recipe_again_is_refused(client, logged_in):
    """P1 — the index must cover the identity the rest of the codebase uses.

    ``_recipe_identity_keys`` (``auth_api_bp.py``) and the SPA's INV-1 both
    treat a recipe's identity as ``{source_slug, slug}`` —
    ``r.sourceSlug === slug || r.slug === slug``. The indexes key only on
    ``source_slug``.

    So when a user already owns the **published row itself**
    (``slug='x'``, ``source_slug`` NULL) and saves ``/r/x`` again, the copy gets
    ``source_slug='x'`` and collides with nothing: the published row is outside
    the partial index entirely. The database accepts a duplicate that this PR's
    own helper would classify as the same recipe — which is the exact case the
    PR claims to close, reachable by any caller that bypasses the SPA or holds a
    stale recipe list.
    """
    published = Recipe(
        id="own-published",
        user_id=logged_in.id,
        name="Vegan Cornbread",
        slug=SOURCE_SLUG,
        source_slug=None,
        is_public=True,
        data={"name": "Vegan Cornbread", "slug": SOURCE_SLUG, "is_public": True},
    )
    db.session.add(published)
    db.session.commit()

    # The author opens their own /r/vegan-cornbread and saves it.
    response = client.post("/api/recipes", json=_payload())

    assert response.status_code == 409, (
        f"expected 409, got {response.status_code} — the owner now holds two rows "
        "for one recipe, which is the duplicate this PR is meant to refuse"
    )
    assert Recipe.query.filter_by(user_id=logged_in.id).count() == 1


@pytest.mark.xfail(
    reason="Known dual-identity gap, still open after the KAN-221 re-key. A row "
    "holding BOTH a source pointer and its own published page (a saved copy "
    "that was later published) is indexed only by its source pointer "
    "(COALESCE picks source_recipe_id/source_slug over the id arm), so saving "
    "that row's own public URL is accepted. A unique index keys one value per "
    "row and cannot express 'alias sets must be disjoint' — that needs a "
    "side table, which is out of KAN-221's scope. Note the ROW ITSELF is now "
    "largely historical: the RCP-74 guard refuses publishing saved copies, so "
    "new copy-then-published rows can no longer be created through the API. "
    "Documented rather than patched.",
    strict=True,
)
def test_saving_your_own_published_copy_is_not_yet_refused(client, logged_in):
    """The third hole in the same model — recorded so it cannot be forgotten.

    Found by Copilot on PR #273 after the COALESCE fix closed the second one.
    Three rounds found three holes in one identity model, which is the finding:
    a single-valued key cannot cover a row aliased by two identities. The
    KAN-221 re-key moved the key to the stable source id (closing the slug-
    mutation holes); this dual-alias corner is the one it leaves, and the
    RCP-74 publish guard is what starves it of new rows.
    """
    copy_then_published = Recipe(
        id="copy-published",
        user_id=logged_in.id,
        name="Vegan Cornbread",
        source_slug="someone-elses-original",
        slug="my-copy",
        is_public=True,
        data={"name": "Vegan Cornbread"},
    )
    db.session.add(copy_then_published)
    db.session.commit()

    # The owner saves their OWN published copy's page.
    response = client.post("/api/recipes", json=_payload(source_slug="my-copy"))

    assert response.status_code == 409
