"""Recipe feedback, Phase 1 (KAN-356, docs/designs/recipe-feedback.md).

Covers the KAN-356 acceptance list on the server side:
- eligibility at submission (saved copy by source_recipe_id, no slug fallback; author rule)
- one active entry per account per recipe; identical replay no-op; changed payload 409
- caps (1..5 stars, 1,000-character note, 50 notes on the page)
- the 24 hours (R8), worked out at read time with one predicate (E4)
- delete and post again (R7); a hide survives it (O1)
- moderation: held notes, trust (A1, R3-6), moderator list fails closed (R2)
- mail never fails a write; disabled mail logs feedback_moderation_email_skipped (R3)
- the public page states (R9, R10), aggregateRating only at 3 cooks, escaping,
  the Markdown view (R16)
- regressions: no-entry JSON-LD unchanged; guest merge keeps source_recipe_id (T6)
"""

import json
import logging
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from blueprints.auth_api_bp import _merge_guest_session_into_user  # noqa: E402
from blueprints.feedback_bp import public_feedback_summary  # noqa: E402
from extensions import db  # noqa: E402
from models.recipe import Recipe  # noqa: E402
from models.recipe_feedback import RecipeFeedback  # noqa: E402
from models.user import User  # noqa: E402
from services import feedback_mail  # noqa: E402

MODERATOR_EMAIL = "adam@example.com"
OLD = timedelta(days=30)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    monkeypatch.setenv("FEEDBACK_MODERATOR_EMAILS", MODERATOR_EMAIL)
    monkeypatch.delenv("FEEDBACK_MODERATION_EMAIL_ENABLED", raising=False)
    monkeypatch.setenv("ADMIN_API_TOKEN", "admin-token")
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
def client(app):
    return app.test_client()


def _user(email, name="Sam Rivera", age=OLD):
    row = User(email=email, name=name, created_at=datetime.utcnow() - age)
    db.session.add(row)
    db.session.commit()
    return row


def _recipe(name="Classic Meringue Cookies", slug="classic-meringue-cookies", owner=None, **kw):
    row = Recipe(
        id=str(uuid.uuid4()),
        user_id=owner.id if owner else None,
        name=name,
        slug=slug,
        is_public=kw.pop("public", True),
        data={"name": name, "description": f"{name} description"},
        **kw,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _copy(target, cook, *, by_id=True):
    """A cook's saved copy. ``by_id=False`` is an older slug-only copy (A4)."""
    row = Recipe(
        id=str(uuid.uuid4()),
        user_id=cook.id,
        name=target.name,
        is_public=False,
        source_recipe_id=target.id if by_id else None,
        source_slug=target.slug,
        data={"name": target.name},
    )
    db.session.add(row)
    db.session.commit()
    return row


def _sign_in(client, user):
    with client.session_transaction() as sess:
        sess["user_id"] = user.id


def _post(client, target, stars=5, note=None):
    body = {"stars": stars}
    if note is not None:
        body["note"] = note
    return client.post(f"/api/feedback/{target.id}", json=body)


@pytest.fixture
def world(app):
    author = _user("author@example.com", name="Ada Author")
    target = _recipe(owner=author)
    return author, target


def _cook_with_copy(target, email, name="Sam Rivera", age=OLD):
    cook = _user(email, name=name, age=age)
    _copy(target, cook)
    return cook


# --------------------------------------------------------------------------
# Eligibility and the entry
# --------------------------------------------------------------------------


def test_guest_is_refused_everywhere(client, world):
    _, target = world
    assert client.get(f"/api/feedback/{target.id}").status_code == 401
    assert _post(client, target).status_code == 401
    assert client.delete(f"/api/feedback/{target.id}").status_code == 401


def test_cook_without_saved_copy_is_refused(client, world):
    _, target = world
    cook = _user("cook@example.com")
    _sign_in(client, cook)
    assert _post(client, target).status_code == 403
    assert client.get(f"/api/feedback/{target.id}").status_code == 404


def test_cook_with_slug_only_copy_is_refused(client, world):
    """No slug fallback: a copy without source_recipe_id gets no control (A4)."""
    _, target = world
    cook = _user("cook@example.com")
    _copy(target, cook, by_id=False)
    _sign_in(client, cook)
    response = _post(client, target)
    assert response.status_code == 403
    assert response.get_json()["code"] == "not_eligible"


def test_cook_with_saved_copy_posts_and_note_is_held(client, world):
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    response = _post(client, target, stars=4, note="Used oat milk.")
    assert response.status_code == 201
    entry = response.get_json()["entry"]
    assert entry["stars"] == 4
    assert entry["note_status"] == "held"


def test_copy_of_renamed_source_is_still_eligible(client, world):
    """O6: the copy carries the old slug but the same source_recipe_id."""
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    target.slug = "renamed-meringue-cookies"
    db.session.commit()
    _sign_in(client, cook)
    assert _post(client, target).status_code == 201


def test_author_posts_without_a_copy(client, world):
    author, target = world
    _sign_in(client, author)
    response = _post(client, target, stars=5)
    assert response.status_code == 201
    state = client.get(f"/api/feedback/{target.id}").get_json()
    assert state["role"] == "author"
    assert state["entry"]["stars"] == 5
    assert state["can_post"] is False


@pytest.mark.parametrize("stars", [0, 6, "5", True, None, 4.5])
def test_stars_outside_one_to_five_are_400(client, world, stars):
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    response = client.post(f"/api/feedback/{target.id}", json={"stars": stars})
    assert response.status_code == 400


def test_note_cap_is_one_thousand_characters(client, world):
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    assert _post(client, target, note="x" * 1001).status_code == 400
    assert _post(client, target, note="x" * 1000).status_code == 201


def test_identical_replay_is_a_noop_and_changed_payload_is_409(client, world):
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    assert _post(client, target, stars=4, note="Nice").status_code == 201
    replay = _post(client, target, stars=4, note="Nice")
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    changed = _post(client, target, stars=3, note="Nice")
    assert changed.status_code == 409
    assert changed.get_json()["code"] == "entry_exists"
    assert RecipeFeedback.query.filter_by(user_id=cook.id).count() == 1


def test_unpublished_target_is_409(client, world):
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    target.is_public = False
    db.session.commit()
    _sign_in(client, cook)
    response = _post(client, target)
    assert response.status_code == 409
    assert response.get_json()["code"] == "recipe_not_public"


def test_source_unpublished_entry_still_shown_and_deletable(client, world):
    """R3-4: the entry, its status and delete; no new entry can be made."""
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    _post(client, target, note="Good")
    target.is_public = False
    db.session.commit()
    state = client.get(f"/api/feedback/{target.id}").get_json()
    assert state["target_public"] is False
    assert state["entry"]["note_status"] == "held"
    assert state["can_post"] is False
    assert client.delete(f"/api/feedback/{target.id}").status_code == 200


def test_delete_keeps_the_row_and_allows_a_new_entry(client, world):
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    _post(client, target, stars=2)
    assert client.delete(f"/api/feedback/{target.id}").status_code == 200
    assert client.get(f"/api/feedback/{target.id}").get_json()["entry"] is None
    assert _post(client, target, stars=5).status_code == 201
    rows = RecipeFeedback.query.filter_by(user_id=cook.id).all()
    assert len(rows) == 2
    assert sum(1 for row in rows if row.deleted_at is None) == 1


def test_delete_needs_no_saved_copy(client, world):
    """An entry outlives its saved copy; delete is ownership only."""
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    _post(client, target)
    Recipe.query.filter_by(user_id=cook.id).delete()
    db.session.commit()
    assert client.delete(f"/api/feedback/{target.id}").status_code == 200


def test_trusted_account_note_publishes_without_review(client, world):
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    cook.feedback_trusted = True
    db.session.commit()
    _sign_in(client, cook)
    assert _post(client, target, note="Great").get_json()["entry"]["note_status"] == "published"


def test_new_account_posts_and_is_told_when_it_shows(client, world):
    """R8: account age never refuses; the cook gets the date and time."""
    _, target = world
    cook = _cook_with_copy(target, "new@example.com", age=timedelta(hours=1))
    _sign_in(client, cook)
    assert _post(client, target).status_code == 201
    state = client.get(f"/api/feedback/{target.id}").get_json()
    assert state["account_new"] is True
    shows = datetime.fromisoformat(state["visible_from"])
    assert shows == cook.created_at + timedelta(hours=24)


# --------------------------------------------------------------------------
# Moderation
# --------------------------------------------------------------------------


def _moderate(client, entry_id, action):
    return client.post(f"/api/feedback/moderation/{entry_id}/{action}")


@pytest.fixture
def moderator(app):
    return _user(MODERATOR_EMAIL, name="Adam Schoen")


def _held_entry(client, target, email, note="Used oat milk."):
    cook = _cook_with_copy(target, email)
    _sign_in(client, cook)
    entry_id = _post(client, target, note=note).get_json()["entry"]["id"]
    return cook, entry_id


def test_non_moderator_is_refused_every_action(client, world, moderator):
    _, target = world
    cook, entry_id = _held_entry(client, target, "cook@example.com")
    _sign_in(client, cook)
    assert client.get("/api/feedback/moderation").status_code == 403
    for action in ("approve", "reject", "hide-note", "hide-entry", "show-again"):
        assert _moderate(client, entry_id, action).status_code == 403


def test_missing_moderator_setting_refuses_everyone(client, world, moderator, monkeypatch):
    _, target = world
    _, entry_id = _held_entry(client, target, "cook@example.com")
    monkeypatch.delenv("FEEDBACK_MODERATOR_EMAILS")
    _sign_in(client, moderator)
    assert client.get("/api/feedback/moderation").status_code == 403
    assert _moderate(client, entry_id, "approve").status_code == 403


def test_auth_me_says_whether_the_account_moderates(client, moderator):
    with client.session_transaction() as sess:
        sess["user_id"] = moderator.id
        sess["credentials"] = {"token": "t"}
        sess["user_info"] = {"email": MODERATOR_EMAIL}
    assert client.get("/api/auth/me").get_json()["is_moderator"] is True
    other = _user("other@example.com")
    with client.session_transaction() as sess:
        sess["user_id"] = other.id
    assert client.get("/api/auth/me").get_json()["is_moderator"] is False


def test_approve_trusts_the_account_and_later_notes_publish(client, world, moderator):
    _, target = world
    cook, entry_id = _held_entry(client, target, "cook@example.com")
    _sign_in(client, moderator)
    assert _moderate(client, entry_id, "approve").status_code == 200
    assert db.session.get(User, cook.id).feedback_trusted is True

    second = _recipe(name="Oat Cookies", slug="oat-cookies", owner=world[0])
    _copy(second, cook)
    _sign_in(client, cook)
    assert _post(client, second, note="Also good").get_json()["entry"]["note_status"] == (
        "published"
    )


def test_reject_keeps_stars_and_account_stays_held(client, world, moderator):
    _, target = world
    cook, entry_id = _held_entry(client, target, "cook@example.com")
    _sign_in(client, moderator)
    response = _moderate(client, entry_id, "reject")
    assert response.get_json()["entry"]["note_status"] == "rejected"
    assert db.session.get(User, cook.id).feedback_trusted is False
    assert public_feedback_summary(target)["count"] == 1
    assert _moderate(client, entry_id, "approve").status_code == 409


def test_hide_note_returns_account_to_held_and_keeps_other_notes(client, world, moderator):
    author, target = world
    cook, first_id = _held_entry(client, target, "cook@example.com")
    _sign_in(client, moderator)
    _moderate(client, first_id, "approve")
    second = _recipe(name="Oat Cookies", slug="oat-cookies", owner=author)
    _copy(second, cook)
    _sign_in(client, cook)
    second_id = _post(client, second, note="Also good").get_json()["entry"]["id"]

    _sign_in(client, moderator)
    assert _moderate(client, first_id, "hide-note").status_code == 200
    assert db.session.get(User, cook.id).feedback_trusted is False
    assert db.session.get(RecipeFeedback, second_id).note_status == "published"
    # The hidden note's stars still count.
    assert public_feedback_summary(target)["count"] == 1
    assert public_feedback_summary(target)["notes"] == []

    third = _recipe(name="Rice Pudding", slug="rice-pudding", owner=author)
    _copy(third, cook)
    _sign_in(client, cook)
    third_id = _post(client, third, note="Held again").get_json()["entry"]["id"]
    assert db.session.get(RecipeFeedback, third_id).note_status == "held"
    _sign_in(client, moderator)
    _moderate(client, third_id, "approve")
    assert db.session.get(User, cook.id).feedback_trusted is True


def test_hide_entry_leaves_count_and_survives_delete_and_repost(client, world, moderator):
    _, target = world
    cook, entry_id = _held_entry(client, target, "cook@example.com")
    _sign_in(client, moderator)
    assert _moderate(client, entry_id, "hide-entry").status_code == 200
    assert public_feedback_summary(target)["count"] == 0
    assert db.session.get(User, cook.id).feedback_trusted is False

    _sign_in(client, cook)
    client.delete(f"/api/feedback/{target.id}")
    reposted = _post(client, target, stars=5).get_json()["entry"]
    assert reposted["entry_hidden"] is True  # O1
    assert public_feedback_summary(target)["count"] == 0

    _sign_in(client, moderator)
    assert _moderate(client, reposted["id"], "show-again").status_code == 200
    assert public_feedback_summary(target)["count"] == 1


def test_cook_deleted_entry_is_read_only_for_moderators(client, world, moderator):
    _, target = world
    cook, entry_id = _held_entry(client, target, "cook@example.com")
    client.delete(f"/api/feedback/{target.id}")
    _sign_in(client, moderator)
    for action in ("approve", "reject", "hide-entry", "show-again"):
        assert _moderate(client, entry_id, action).status_code == 409


def test_moderation_lists(client, world, moderator):
    author, target = world
    _, held_id = _held_entry(client, target, "held@example.com")
    trusted = _cook_with_copy(target, "trusted@example.com", name="Tess")
    trusted.feedback_trusted = True
    db.session.commit()
    _sign_in(client, trusted)
    published_id = _post(client, target, note="Fine").get_json()["entry"]["id"]
    gone, gone_id = _held_entry(client, target, "gone@example.com")
    client.delete(f"/api/feedback/{target.id}")

    _sign_in(client, moderator)
    lists = client.get("/api/feedback/moderation").get_json()
    assert [row["id"] for row in lists["held"]] == [held_id]
    assert [row["id"] for row in lists["published_without_review"]] == [published_id]
    assert [row["id"] for row in lists["hidden_and_deleted"]] == [gone_id]
    row = lists["held"][0]
    assert row["user"]["email"] == "held@example.com"
    assert row["user"]["account_new"] is False
    assert row["recipe"]["slug"] == target.slug


# --------------------------------------------------------------------------
# Mail
# --------------------------------------------------------------------------


def test_disabled_mail_logs_skip_and_write_succeeds(client, world, caplog):
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    with caplog.at_level(logging.INFO, logger="services.feedback_mail"):
        assert _post(client, target, note="Held").status_code == 201
    assert "feedback_moderation_email_skipped" in caplog.text


def test_failed_send_does_not_fail_the_write(client, world, monkeypatch, caplog):
    _, target = world
    monkeypatch.setenv("FEEDBACK_MODERATION_EMAIL_ENABLED", "true")
    monkeypatch.setenv("FEEDBACK_MODERATION_EMAIL_FROM", "feedback@tasteslikegood.org")
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    with (
        patch.object(feedback_mail, "_gmail_service", side_effect=RuntimeError("down")),
        caplog.at_level(logging.INFO, logger="services.feedback_mail"),
    ):
        assert _post(client, target, note="Held").status_code == 201
    assert "feedback_moderation_email_failed" in caplog.text


def test_enabled_mail_sends_through_gmail(monkeypatch):
    monkeypatch.setenv("FEEDBACK_MODERATION_EMAIL_ENABLED", "1")
    monkeypatch.setenv("FEEDBACK_MODERATION_EMAIL_FROM", "feedback@tasteslikegood.org")
    monkeypatch.setenv("FEEDBACK_MODERATOR_EMAILS", MODERATOR_EMAIL)
    with patch.object(feedback_mail, "_gmail_service") as service:
        assert feedback_mail.send_held_reminder(2) is True
    send = service.return_value.users.return_value.messages.return_value.send
    assert send.call_args.kwargs["userId"] == "me"
    assert "raw" in send.call_args.kwargs["body"]


def test_no_mail_for_a_note_that_publishes_or_has_no_note(client, world):
    _, target = world
    cook = _cook_with_copy(target, "cook@example.com")
    _sign_in(client, cook)
    with patch.object(feedback_mail, "notify_new_held_note") as notify:
        _post(client, target)
    notify.assert_not_called()


def test_reminder_needs_admin_token_and_counts_held(client, world):
    _, target = world
    _held_entry(client, target, "cook@example.com")
    assert client.post("/api/feedback/moderation/reminder").status_code == 403
    with patch.object(feedback_mail, "send_held_reminder", return_value=True) as remind:
        response = client.post(
            "/api/feedback/moderation/reminder",
            headers={"Authorization": "Bearer admin-token"},
        )
    assert response.get_json() == {"held": 1, "sent": True}
    remind.assert_called_once_with(1)


# --------------------------------------------------------------------------
# Public page
# --------------------------------------------------------------------------


def _json_ld(body):
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', body, re.S):
        data = json.loads(block)
        if data.get("@type") == "Recipe":
            return data
    raise AssertionError("no Recipe JSON-LD")


def _entry(target, user, stars=5, note=None, status=None, **kw):
    row = RecipeFeedback(
        recipe_id=target.id,
        user_id=user.id,
        stars=stars,
        note=note,
        note_status=status if note else None,
        created_at=kw.pop("created_at", datetime.utcnow()),
        **kw,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _page(client, target):
    response = client.get(f"/r/{target.slug}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_no_entries_renders_the_link_line_only(client, world):
    _, target = world
    body = _page(client, target)
    assert "Cooked this? Add your feedback" in body
    assert f"/?feedback={target.slug}" in body
    assert "Cooked by" not in body
    assert "From the author" not in body
    assert "public-recipe-meta-feedback" not in body
    assert "aggregateRating" not in _json_ld(body)


def test_no_entries_json_ld_is_unchanged(client, world):
    """Regression: a recipe with no entries renders the same JSON-LD as before."""
    from blueprints.public_bp import _canonical_url, _recipe_json_ld, _rendered_image

    _, target = world
    body = _page(client, target)
    with client.application.test_request_context(f"/r/{target.slug}"):
        expected = _recipe_json_ld(
            target,
            _canonical_url("public.show_public_recipe", slug=target.slug),
            _rendered_image(target)[0],
        )
    assert _json_ld(body) == json.loads(json.dumps(expected))


def test_author_only_with_held_note_shows_stars_and_line(client, world):
    author, target = world
    _entry(target, author, stars=4, note="Secret tweak", status="held")
    body = _page(client, target)
    assert "From the author" in body
    assert "The author cooked this." in body
    assert "Secret tweak" not in body
    assert "4 out of 5 stars" in body
    assert "Cooked by" not in body


def test_author_only_with_approved_note_shows_note(client, world):
    author, target = world
    _entry(target, author, stars=5, note="I make this weekly", status="approved")
    body = _page(client, target)
    assert "I make this weekly" in body
    assert "The author cooked this." not in body


def _feedback_section(body):
    start = body.index('id="feedback"')
    return body[start : body.index("</section>", start)]


def test_author_byline_uses_the_author_display_name(client, world, monkeypatch):
    """KAN-434: the site account's entry carries the brand name, never user.name."""
    author, target = world
    _entry(target, author, stars=5)
    monkeypatch.setenv("BRAND_AUTHOR_USER_IDS", str(author.id))
    section = _feedback_section(_page(client, target))
    assert "Tasteslikegood - classic" in section
    assert "Ada Author" not in section


def test_author_byline_is_the_account_name_for_other_authors(client, world, monkeypatch):
    author, target = world
    _entry(target, author, stars=5)
    monkeypatch.setenv("BRAND_AUTHOR_USER_IDS", "")
    section = _feedback_section(_page(client, target))
    assert "Ada Author" in section
    assert "Tasteslikegood - classic" not in section


def test_two_cooks_show_count_without_stars_or_rating(client, world):
    author, target = world
    _entry(target, author, stars=1)
    for index, email in enumerate(["a@example.com", "b@example.com"]):
        _entry(
            target, _user(email, name=f"Cook{index} Last"), stars=5, note="Good", status="approved"
        )
    body = _page(client, target)
    assert "Cooked by 2" in body
    assert "average" not in body
    assert "aggregateRating" not in _json_ld(body)
    assert "public-recipe-meta-feedback" in body
    assert "Cook0 · " in body
    assert "From the author" in body


def test_three_cooks_show_average_and_matching_aggregate_rating(client, world):
    author, target = world
    _entry(target, author, stars=1)  # never in the count or the average
    for email, stars in [("a@example.com", 5), ("b@example.com", 4), ("c@example.com", 4)]:
        _entry(target, _user(email), stars=stars)
    body = _page(client, target)
    assert "Cooked by 3" in body
    assert "4.3 ★ average from 3 cooks" in body
    rating = _json_ld(body)["aggregateRating"]
    assert rating["ratingValue"] == 4.3
    assert rating["ratingCount"] == 3
    assert rating["bestRating"] == 5


def test_hidden_entry_leaves_count_and_hidden_note_keeps_stars(client, world):
    _, target = world
    _entry(target, _user("a@example.com"), stars=5, entry_hidden=True)
    _entry(target, _user("b@example.com"), stars=3, note="Hidden text", status="hidden")
    body = _page(client, target)
    assert "Cooked by 1" in body
    assert "Hidden text" not in body


def test_held_and_rejected_notes_are_not_shown(client, world):
    _, target = world
    _entry(target, _user("a@example.com"), note="Held text", status="held")
    _entry(target, _user("b@example.com"), note="Rejected text", status="rejected")
    body = _page(client, target)
    assert "Cooked by 2" in body
    assert "Held text" not in body and "Rejected text" not in body


def test_note_text_is_escaped(client, world):
    _, target = world
    _entry(target, _user("a@example.com"), note="<script>alert(1)</script>", status="approved")
    body = _page(client, target)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_empty_name_shows_a_cook(client, world):
    _, target = world
    _entry(target, _user("a@example.com", name=""), note="Nice", status="approved")
    assert "A cook · " in _page(client, target)


def test_fifty_newest_notes_then_the_line(client, world):
    _, target = world
    start = datetime.utcnow() - timedelta(days=1)
    for index in range(51):
        _entry(
            target,
            _user(f"c{index}@example.com"),
            note=f"note-{index:02d}-text",
            status="approved",
            created_at=start + timedelta(minutes=index),
        )
    body = _page(client, target)
    assert "Showing the 50 newest notes." in body
    assert "note-00-text" not in body  # the oldest
    assert body.index("note-50-text") < body.index("note-49-text")  # newest first


def test_the_24_hours_apply_to_count_average_notes_and_rating_together(app, world):
    """E4: one age predicate. 23h59m absent from everything; 24h00m present."""
    _, target = world
    created = datetime(2026, 10, 1, 12, 0, 0)
    for email in ["a@example.com", "b@example.com"]:
        _entry(target, _user(email), stars=5)
    newcomer = User(email="new@example.com", name="Nia New", created_at=created)
    db.session.add(newcomer)
    db.session.commit()
    _entry(target, newcomer, stars=2, note="Approved early", status="approved")

    before = public_feedback_summary(target, now=created + timedelta(hours=23, minutes=59))
    assert before["count"] == 2
    assert before["average"] is None
    assert before["notes"] == []

    at = public_feedback_summary(target, now=created + timedelta(hours=24))
    assert at["count"] == 3
    assert at["average"] == 4.0
    assert [note["text"] for note in at["notes"]] == ["Approved early"]


def test_authors_wait_the_24_hours_too(app):
    created = datetime(2026, 10, 1, 12, 0, 0)
    author = User(email="author@example.com", name="Ada", created_at=created)
    db.session.add(author)
    db.session.commit()
    target = _recipe(owner=author)
    _entry(target, author, stars=5)
    assert public_feedback_summary(target, now=created + timedelta(hours=1))["author"] is None
    assert public_feedback_summary(target, now=created + timedelta(hours=24))["author"]


def test_summary_failure_renders_the_page_with_the_link_only(client, world):
    _, target = world
    _entry(target, _user("a@example.com"))
    with patch("blueprints.feedback_bp.db.session.query", side_effect=RuntimeError("db")):
        body = _page(client, target)
    assert "Cooked this? Add your feedback" in body
    assert "Cooked by" not in body


def test_markdown_view_renders_notes_as_literal_text(client, world):
    """R16 and the 2026-10-18 KAN-356 comment: no live Markdown from a note."""
    _, target = world
    note = (
        "# heading **bold** [link](http://x.example) ![img](http://x.example/p.png) "
        "Visit https://evil.example/path spam@evil.example"
    )
    _entry(target, _user("a@example.com"), note=note, status="published")
    response = client.get(f"/r/{target.slug}", headers={"Accept": "text/markdown"})
    assert response.status_code == 200
    markdown = response.get_data(as_text=True)
    assert "[link](http://x.example)" in markdown  # present, but only inside a code span
    line = next(row for row in markdown.splitlines() if "heading" in row)
    assert "`" in line
    code = line[line.index("`") : line.rindex("`") + 1]
    assert "https://evil.example/path" in code
    assert "spam@evil.example" in code
    assert "![img](http://x.example/p.png)" in code
    assert not re.search(r"^#+ heading", markdown, re.M)


# --------------------------------------------------------------------------
# Regression (design T6)
# --------------------------------------------------------------------------


def test_guest_merge_keeps_source_recipe_id(app, world):
    _, target = world
    cook = _user("cook@example.com")
    guest_copy = Recipe(
        id=str(uuid.uuid4()),
        guest_session_id="guest-abc",
        name=target.name,
        source_recipe_id=target.id,
        source_slug=target.slug,
        data={"name": target.name},
    )
    db.session.add(guest_copy)
    db.session.commit()
    _merge_guest_session_into_user(cook, "guest-abc")
    merged = Recipe.query.filter_by(user_id=cook.id).one()
    assert merged.source_recipe_id == target.id
