"""Guest→login merge runs the duplicate-recipe check (KAN-186 / RCP-61).

A recipe saved during a guest session frequently duplicates one the account
already owns: the user opens a public recipe, clicks "add to cookbook" before
auth resolves, then logs in. The merge used to reassign every guest row with a
single bulk UPDATE, so the account silently gained a second copy; publish-time
confirmation (INV-3) only caught it after the duplicate row already existed.

Acceptance from KAN-186, one test each:
- a guest row duplicating an owned recipe does NOT create a second row
- a guest row genuinely new to the account still merges over
- ownership-refusal behaviour (INV-4 / same_owner) is untouched

Plus the two guards the fix introduces: public guest rows are reassigned rather
than deleted, and cookbook membership is remapped instead of left dangling.
"""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from blueprints.auth_api_bp import _merge_guest_session_into_user  # noqa: E402
from extensions import db  # noqa: E402
from models.cookbook import Cookbook  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.user import User  # noqa: E402

GUEST_SESSION = "guest-session-abc"


@pytest.fixture
def app():
    flask_app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        WTF_CSRF_ENABLED=False,
    )
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def user(app):
    row = User(email="adam@example.com", name="Adam")
    db.session.add(row)
    db.session.commit()
    return row


def _recipe(name, *, owner=None, guest=None, source_slug=None, slug=None, public=False):
    row = Recipe(
        id=str(uuid.uuid4()),
        user_id=owner.id if owner else None,
        guest_session_id=guest,
        name=name,
        source_slug=source_slug,
        slug=slug,
        is_public=public,
        data={"name": name},
    )
    db.session.add(row)
    return row


def _cookbook(name, recipe_ids, *, guest=None, owner=None):
    row = Cookbook(
        id=str(uuid.uuid4()),
        user_id=owner.id if owner else None,
        guest_session_id=guest,
        name=name,
        recipe_ids=recipe_ids,
    )
    db.session.add(row)
    return row


def test_duplicate_guest_recipe_does_not_create_a_second_row(app, user):
    """The KAN-186 repro: same public recipe, saved as guest, then logged in."""
    owned = _recipe("Pizza Dough", owner=user, source_slug="vegan-fried-pizza-dough")
    _recipe("Pizza Dough", guest=GUEST_SESSION, source_slug="vegan-fried-pizza-dough")
    db.session.commit()
    owned_id = owned.id

    _merge_guest_session_into_user(user, GUEST_SESSION)

    rows = Recipe.query.filter_by(user_id=user.id).all()
    assert len(rows) == 1, "merge created a duplicate row"
    assert rows[0].id == owned_id, "merge kept the guest copy instead of the owned one"
    assert Recipe.query.filter_by(guest_session_id=GUEST_SESSION).count() == 0


def test_new_guest_recipe_still_merges_over(app, user):
    """The other half of the acceptance — no over-matching."""
    _recipe("Pizza Dough", owner=user, source_slug="vegan-fried-pizza-dough")
    _recipe("Lentil Soup", guest=GUEST_SESSION, source_slug="hearty-lentil-soup")
    db.session.commit()

    _merge_guest_session_into_user(user, GUEST_SESSION)

    names = sorted(r.name for r in Recipe.query.filter_by(user_id=user.id).all())
    assert names == ["Lentil Soup", "Pizza Dough"]


def test_locally_authored_recipes_are_not_matched_on_null_slugs(app, user):
    """Two recipes with no slugs at all are not "the same recipe".

    Guards the obvious way to get this wrong: keying on ``source_slug`` without
    excluding ``None`` makes every locally-authored guest recipe collide with
    every locally-authored owned one, and the merge silently eats them.
    """
    _recipe("My Own Recipe", owner=user)
    _recipe("A Different Recipe", guest=GUEST_SESSION)
    db.session.commit()

    _merge_guest_session_into_user(user, GUEST_SESSION)

    assert Recipe.query.filter_by(user_id=user.id).count() == 2


def test_slug_matches_source_slug_across_the_pair(app, user):
    """INV-1 matches sourceSlug OR slug; the server check mirrors both."""
    _recipe("Pizza Dough", owner=user, slug="vegan-fried-pizza-dough", public=True)
    _recipe("Pizza Dough", guest=GUEST_SESSION, source_slug="vegan-fried-pizza-dough")
    db.session.commit()

    _merge_guest_session_into_user(user, GUEST_SESSION)

    assert Recipe.query.filter_by(user_id=user.id).count() == 1


def test_public_guest_duplicate_is_reassigned_not_deleted(app, user):
    """Deleting a published row would take a live public page down."""
    _recipe("Pizza Dough", owner=user, source_slug="vegan-fried-pizza-dough")
    _recipe(
        "Pizza Dough",
        guest=GUEST_SESSION,
        source_slug="vegan-fried-pizza-dough",
        slug="vegan-fried-pizza-dough-2",
        public=True,
    )
    db.session.commit()

    _merge_guest_session_into_user(user, GUEST_SESSION)

    rows = Recipe.query.filter_by(user_id=user.id).all()
    assert len(rows) == 2, "a live public page was deleted as a duplicate"
    assert {r.slug for r in rows} == {None, "vegan-fried-pizza-dough-2"}


def test_guest_own_slug_collision_is_left_unreassigned_not_rolled_back(app, user):
    """KAN-223: the legacy public-row clear only handles a source_slug-side match.

    Owned row X is a saved copy pointing at public page "cornbread"
    (``source_slug="cornbread"``). The guest's public row IS that page
    (``slug="cornbread"``) — so ``_recipe_identity_keys()`` matches
    ``existing_id`` via the guest row's own ``slug``, not its ``source_slug``.
    Clearing ``source_slug`` (which is already None here) does nothing: the
    post-clear identity is still ``COALESCE(None, "cornbread") == "cornbread"``,
    the exact value that collided.

    Before the fix this raised IntegrityError on commit — every retry hit the
    same collision — and the exception propagated out of
    ``_merge_guest_session_into_user``, which would have taken every other
    guest recipe and cookbook in the same login merge down with it. The fix
    detects the still-colliding identity before reassigning and leaves this
    one row under its guest session instead.
    """
    _recipe("Cornbread copy", owner=user, source_slug="cornbread")
    guest_original = _recipe("Cornbread", guest=GUEST_SESSION, slug="cornbread", public=True)
    db.session.commit()
    guest_original_id = guest_original.id

    _merge_guest_session_into_user(user, GUEST_SESSION)  # must not raise

    owned_rows = Recipe.query.filter_by(user_id=user.id).all()
    assert len(owned_rows) == 1, "the still-colliding row must not have been reassigned"

    left_behind = Recipe.query.filter_by(id=guest_original_id).one()
    assert left_behind.user_id is None
    assert (
        left_behind.guest_session_id == GUEST_SESSION
    ), "left under the guest session, not lost, not partially merged"


def test_cookbook_membership_is_remapped_to_the_surviving_row(app, user):
    """Cookbook.recipe_ids is a JSON id list — it must not keep a dangling id."""
    owned = _recipe("Pizza Dough", owner=user, source_slug="vegan-fried-pizza-dough")
    guest_dupe = _recipe("Pizza Dough", guest=GUEST_SESSION, source_slug="vegan-fried-pizza-dough")
    guest_new = _recipe("Lentil Soup", guest=GUEST_SESSION, source_slug="hearty-lentil-soup")
    db.session.commit()
    owned_id, guest_dupe_id, guest_new_id = owned.id, guest_dupe.id, guest_new.id

    _cookbook("Weeknight", [guest_dupe_id, guest_new_id], guest=GUEST_SESSION)
    db.session.commit()

    _merge_guest_session_into_user(user, GUEST_SESSION)

    cb = Cookbook.query.filter_by(user_id=user.id).one()
    assert cb.recipe_ids == [owned_id, guest_new_id]
    live_ids = {r.id for r in Recipe.query.filter_by(user_id=user.id).all()}
    assert set(cb.recipe_ids) <= live_ids, "cookbook references a deleted recipe"


def test_remap_does_not_list_the_survivor_twice(app, user):
    """A guest cookbook holding both copies must not end up with a duplicate id."""
    owned = _recipe("Pizza Dough", owner=user, source_slug="vegan-fried-pizza-dough")
    guest_dupe = _recipe("Pizza Dough", guest=GUEST_SESSION, source_slug="vegan-fried-pizza-dough")
    db.session.commit()
    owned_id, guest_dupe_id = owned.id, guest_dupe.id

    _cookbook("Weeknight", [owned_id, guest_dupe_id], guest=GUEST_SESSION)
    db.session.commit()

    _merge_guest_session_into_user(user, GUEST_SESSION)

    cb = Cookbook.query.filter_by(user_id=user.id).one()
    assert cb.recipe_ids == [owned_id]


def test_another_users_rows_are_never_touched(app, user):
    """INV-4 / same_owner is untouched: the merge only ever reads guest rows."""
    other = User(email="someone-else@example.com", name="Someone Else")
    db.session.add(other)
    db.session.commit()

    _recipe("Pizza Dough", owner=other, source_slug="vegan-fried-pizza-dough")
    _recipe("Pizza Dough", guest=GUEST_SESSION, source_slug="vegan-fried-pizza-dough")
    db.session.commit()

    _merge_guest_session_into_user(user, GUEST_SESSION)

    assert Recipe.query.filter_by(user_id=other.id).count() == 1, "another account lost a row"
    assert Recipe.query.filter_by(user_id=user.id).count() == 1, "guest row did not merge"


def test_cookbook_name_collision_still_renames(app, user):
    """The pre-existing uq_cookbook_user_name behaviour must survive the change."""
    _cookbook("Weeknight", [], owner=user)
    _cookbook("Weeknight", [], guest=GUEST_SESSION)
    db.session.commit()

    _merge_guest_session_into_user(user, GUEST_SESSION)

    names = sorted(cb.name for cb in Cookbook.query.filter_by(user_id=user.id).all())
    assert names == ["Weeknight", "Weeknight (2)"]
