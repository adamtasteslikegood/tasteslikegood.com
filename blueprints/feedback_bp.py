"""Recipe feedback API (KAN-356, design ``docs/designs/recipe-feedback.md``).

Cook and author routes, keyed to the PUBLIC recipe's id (the "target"):

    GET    /api/feedback/<target_id>   the signed-in account's entry and what it may do
    POST   /api/feedback/<target_id>   post an entry: {"stars": 1..5, "note": "..."}
    DELETE /api/feedback/<target_id>   delete the account's active entry (kept for moderation)

Moderator routes (an account whose email is on ``FEEDBACK_MODERATOR_EMAILS``;
with the setting missing, every moderation request is refused, decision R2):

    GET  /api/feedback/moderation                      three lists (R13)
    POST /api/feedback/moderation/<entry_id>/<action>  approve, reject, hide-note,
                                                       hide-entry or show-again
    POST /api/feedback/moderation/reminder             held-note reminder; needs
                                                       ADMIN_API_TOKEN (Cloud Scheduler)

``public_feedback_summary`` feeds the server-rendered ``/r/<slug>`` page and its
JSON-LD from one query, so the visible numbers and ``aggregateRating`` always
agree (eng review 2, E4).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any

from flask import Blueprint, jsonify, request, session
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import Recipe, User
from models.recipe_feedback import (
    NOTE_APPROVED,
    NOTE_HELD,
    NOTE_HIDDEN,
    NOTE_MAX_LENGTH,
    NOTE_PUBLISHED,
    NOTE_REJECTED,
    PUBLIC_NOTE_STATUSES,
    STARS_MAX,
    STARS_MIN,
    RecipeFeedback,
)
from services import feedback_mail
from utils.admin_auth import require_admin
from utils.log_sanitizer import sanitize_log_value

logger = logging.getLogger(__name__)

feedback_bp = Blueprint("feedback", __name__, url_prefix="/api/feedback")

ACCOUNT_AGE_BEFORE_PUBLIC = timedelta(hours=24)
AGGREGATE_RATING_MIN_COOKS = 3
PUBLIC_NOTES_CAP = 50
MODERATION_LIST_CAP = 200


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def moderator_emails() -> frozenset[str]:
    """The moderator list from deploy configuration; empty when unset (R2)."""
    raw = os.environ.get("FEEDBACK_MODERATOR_EMAILS", "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def is_moderator(user: User | None) -> bool:
    """True only for a signed-in account whose email is on the list. Fails closed."""
    if user is None or not user.email:
        return False
    return user.email.strip().lower() in moderator_emails()


@feedback_bp.record_once
def _warn_when_no_moderator(_state: Any) -> None:
    if not moderator_emails():
        logger.warning(
            "FEEDBACK_MODERATOR_EMAILS is not set: every feedback moderation action is refused"
        )


def first_name(user: User | None) -> str:
    """First word of the Google name, read at display; "A cook" when empty (R6, A3)."""
    name = (user.name if user else None) or ""
    parts = name.split()
    return parts[0] if parts else "A cook"


def visible_from(user: User) -> datetime:
    """When this account's entries start to show publicly (R8)."""
    return (user.created_at or datetime.utcnow()) + ACCOUNT_AGE_BEFORE_PUBLIC


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _current_user() -> User | None:
    """The signed-in account's row, or None for guests and stale sessions (A5)."""
    user_id = session.get("user_id")
    if not isinstance(user_id, int):
        return None
    return db.session.get(User, user_id)


def _active_entry(recipe_id: str, user_id: int) -> RecipeFeedback | None:
    entry: RecipeFeedback | None = RecipeFeedback.query.filter(
        RecipeFeedback.recipe_id == recipe_id,
        RecipeFeedback.user_id == user_id,
        RecipeFeedback.deleted_at.is_(None),
    ).first()
    return entry


def _has_saved_copy(target: Recipe, user: User) -> bool:
    """A row the account owns whose ``source_recipe_id`` is the target. No slug fallback (A4)."""
    return (
        db.session.query(Recipe.id)
        .filter(Recipe.user_id == user.id, Recipe.source_recipe_id == target.id)
        .first()
        is not None
    )


def _is_author(target: Recipe, user: User) -> bool:
    """The feedback author is the target recipe's ``user_id`` (A2)."""
    return target.user_id is not None and target.user_id == user.id


def _owner_entry_dict(entry: RecipeFeedback) -> dict[str, Any]:
    return {
        "id": entry.id,
        "recipe_id": entry.recipe_id,
        "stars": entry.stars,
        "note": entry.note,
        "note_status": entry.note_status,
        "entry_hidden": bool(entry.entry_hidden),
        "created_at": _iso(entry.created_at),
        "deleted_at": _iso(entry.deleted_at),
    }


def _error(message: str, status: int, code: str) -> tuple[Any, int]:
    return jsonify({"error": message, "code": code}), status


def _parse_entry_payload() -> tuple[int, str | None, Any]:
    """``(stars, note, None)`` from the JSON body, or ``(0, None, error_response)`` (Q2)."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return 0, None, _error("Expected a JSON object.", 400, "invalid_body")
    stars = body.get("stars")
    if isinstance(stars, bool) or not isinstance(stars, int) or not STARS_MIN <= stars <= STARS_MAX:
        return 0, None, _error("Choose a rating from 1 to 5 stars.", 400, "invalid_stars")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        return 0, None, _error("The note must be text.", 400, "invalid_note")
    note = (note or "").strip() or None
    if note is not None and len(note) > NOTE_MAX_LENGTH:
        return (
            0,
            None,
            _error(f"The note can be at most {NOTE_MAX_LENGTH} characters.", 400, "note_too_long"),
        )
    return stars, note, None


# --------------------------------------------------------------------------
# Cook and author routes
# --------------------------------------------------------------------------


@feedback_bp.route("/<target_id>", methods=["GET"])
def get_my_feedback(target_id: str):
    user = _current_user()
    if user is None:
        return _error("Sign in to leave feedback.", 401, "sign_in_required")
    target = db.session.get(Recipe, target_id)
    if target is None:
        return _error("Recipe not found.", 404, "not_found")

    author = _is_author(target, user)
    has_copy = author or _has_saved_copy(target, user)
    entry = _active_entry(target.id, user.id)
    if not has_copy and entry is None:
        # Not the account's recipe and not one it saved: say nothing about it.
        return _error("Recipe not found.", 404, "not_found")

    now = datetime.utcnow()
    shows_from = visible_from(user)
    return jsonify(
        {
            "target_id": target.id,
            "target_public": bool(target.is_public),
            "target_slug": target.slug if target.is_public else None,
            "role": "author" if author else "cook",
            "first_name": first_name(user),
            "account_new": shows_from > now,
            "visible_from": _iso(shows_from),
            "account_trusted": bool(user.feedback_trusted),
            "can_post": bool(target.is_public) and entry is None,
            "entry": _owner_entry_dict(entry) if entry else None,
        }
    )


@feedback_bp.route("/<target_id>", methods=["POST"])
def post_feedback(target_id: str):
    user = _current_user()
    if user is None:
        return _error("Sign in to leave feedback.", 401, "sign_in_required")

    stars, note, invalid = _parse_entry_payload()
    if invalid is not None:
        return invalid

    target = db.session.get(Recipe, target_id)
    if target is None:
        return _error("Recipe not found.", 404, "not_found")
    if not (_is_author(target, user) or _has_saved_copy(target, user)):
        return _error("Save this recipe to your cookbook to leave feedback.", 403, "not_eligible")
    if not target.is_public:
        return _error(
            "This recipe is no longer public, so feedback is closed.", 409, "recipe_not_public"
        )

    existing = _active_entry(target.id, user.id)
    if existing is not None:
        return _replay_or_conflict(existing, stars, note)

    previous = (
        RecipeFeedback.query.filter(
            RecipeFeedback.recipe_id == target.id, RecipeFeedback.user_id == user.id
        )
        .order_by(RecipeFeedback.created_at.desc(), RecipeFeedback.id.desc())
        .first()
    )
    now = datetime.utcnow()
    carried_hide = bool(previous and previous.entry_hidden)  # O1: a hide survives delete + repost
    if note is None:
        note_status = None
    elif user.feedback_trusted:
        note_status = NOTE_PUBLISHED
    else:
        note_status = NOTE_HELD
    entry = RecipeFeedback(
        recipe_id=target.id,
        user_id=user.id,
        stars=stars,
        note=note,
        note_status=note_status,
        entry_hidden=carried_hide,
        hidden_at=now if carried_hide else None,
        created_at=now,
    )
    db.session.add(entry)
    try:
        db.session.commit()
    except IntegrityError:
        # A double tap raced past the check above; the partial unique index won.
        db.session.rollback()
        existing = _active_entry(target.id, user.id)
        if existing is None:
            raise
        return _replay_or_conflict(existing, stars, note)

    if note_status == NOTE_HELD:
        feedback_mail.notify_new_held_note(entry)  # never raises; never fails the write
    return jsonify({"entry": _owner_entry_dict(entry)}), 201


def _replay_or_conflict(existing: RecipeFeedback, stars: int, note: str | None):
    if existing.stars == stars and (existing.note or None) == note:
        return jsonify({"entry": _owner_entry_dict(existing), "replayed": True}), 200
    return (
        jsonify(
            {
                "error": "You already posted feedback on this recipe. Delete it to post a new one.",
                "code": "entry_exists",
                "entry": _owner_entry_dict(existing),
            }
        ),
        409,
    )


@feedback_bp.route("/<target_id>", methods=["DELETE"])
def delete_my_feedback(target_id: str):
    """Ownership only: an entry outlives its saved copy (A4)."""
    user = _current_user()
    if user is None:
        return _error("Sign in to manage your feedback.", 401, "sign_in_required")
    entry = _active_entry(target_id, user.id)
    if entry is None:
        return _error("No feedback to delete.", 404, "not_found")
    entry.deleted_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"deleted": True, "entry": _owner_entry_dict(entry)}), 200


# --------------------------------------------------------------------------
# Moderation
# --------------------------------------------------------------------------


def _require_moderator() -> tuple[User | None, Any]:
    user = _current_user()
    if user is None:
        return None, _error("Sign in first.", 401, "sign_in_required")
    if not is_moderator(user):
        return None, _error("Moderators only.", 403, "not_moderator")
    return user, None


def _moderation_row(entry: RecipeFeedback, now: datetime) -> dict[str, Any]:
    cook: User = entry.user  # type: ignore[assignment]
    recipe: Recipe = entry.recipe  # type: ignore[assignment]
    return {
        **_owner_entry_dict(entry),
        "note_reviewed_at": _iso(entry.note_reviewed_at),
        "hidden_at": _iso(entry.hidden_at),
        "recipe": {
            "id": recipe.id,
            "name": recipe.name,
            "slug": recipe.slug,
            "is_public": bool(recipe.is_public),
        },
        "user": {
            "id": cook.id,
            "name": cook.name,
            "email": cook.email,
            "created_at": _iso(cook.created_at),
            "account_new": visible_from(cook) > now,
            "feedback_trusted": bool(cook.feedback_trusted),
            "is_author": recipe.user_id == cook.id,
        },
    }


@feedback_bp.route("/moderation", methods=["GET"])
def moderation_lists():
    _, refused = _require_moderator()
    if refused:
        return refused
    now = datetime.utcnow()
    base = RecipeFeedback.query
    held = (
        base.filter(
            RecipeFeedback.note_status == NOTE_HELD,
            RecipeFeedback.deleted_at.is_(None),
            RecipeFeedback.entry_hidden.is_(False),
        )
        .order_by(RecipeFeedback.created_at.asc())
        .limit(MODERATION_LIST_CAP)
        .all()
    )
    unreviewed = (
        base.filter(
            RecipeFeedback.note_status == NOTE_PUBLISHED,
            RecipeFeedback.deleted_at.is_(None),
            RecipeFeedback.entry_hidden.is_(False),
        )
        .order_by(RecipeFeedback.created_at.desc())
        .limit(MODERATION_LIST_CAP)
        .all()
    )
    hidden_or_deleted = (
        base.filter(
            db.or_(
                RecipeFeedback.entry_hidden.is_(True),
                RecipeFeedback.note_status == NOTE_HIDDEN,
                RecipeFeedback.deleted_at.isnot(None),
            )
        )
        .order_by(RecipeFeedback.created_at.desc())
        .limit(MODERATION_LIST_CAP)
        .all()
    )
    return jsonify(
        {
            "held": [_moderation_row(e, now) for e in held],
            "published_without_review": [_moderation_row(e, now) for e in unreviewed],
            "hidden_and_deleted": [_moderation_row(e, now) for e in hidden_or_deleted],
        }
    )


_ACTIONS = ("approve", "reject", "hide-note", "hide-entry", "show-again")


@feedback_bp.route("/moderation/<int:entry_id>/<action>", methods=["POST"])
def moderate(entry_id: int, action: str):
    moderator, refused = _require_moderator()
    if refused or moderator is None:
        return refused
    if action not in _ACTIONS:
        return _error("Unknown action.", 404, "unknown_action")
    entry = db.session.get(RecipeFeedback, entry_id)
    if entry is None:
        return _error("Feedback not found.", 404, "not_found")
    if entry.deleted_at is not None:
        # A cook-deleted entry is a read-only record; nobody restores it (R13).
        return _error("The cook deleted this feedback.", 409, "entry_deleted")

    now = datetime.utcnow()
    cook: User = entry.user  # type: ignore[assignment]
    if action == "approve":
        if entry.note_status != NOTE_HELD:
            return _error("Only a held note can be approved.", 409, "not_held")
        entry.note_status = NOTE_APPROVED
        entry.note_reviewed_at = now
        cook.feedback_trusted = True  # one approval restores the account (R3-6)
    elif action == "reject":
        if entry.note_status != NOTE_HELD:
            return _error("Only a held note can be rejected.", 409, "not_held")
        entry.note_status = NOTE_REJECTED
        entry.note_reviewed_at = now
    elif action == "hide-note":
        if entry.note_status not in (NOTE_APPROVED, NOTE_PUBLISHED):
            return _error("Only a public note can be hidden.", 409, "note_not_public")
        entry.note_status = NOTE_HIDDEN
        entry.note_reviewed_at = now
        cook.feedback_trusted = False  # back to held (A1)
    elif action == "hide-entry":
        if entry.entry_hidden:
            return _error("This feedback is already hidden.", 409, "already_hidden")
        entry.entry_hidden = True
        entry.hidden_at = now
        cook.feedback_trusted = False  # same effect on the account as hiding a note (R3-6)
    else:  # show-again: only what a moderator hid
        if entry.entry_hidden:
            entry.entry_hidden = False
            entry.hidden_at = None
        elif entry.note_status == NOTE_HIDDEN:
            entry.note_status = NOTE_APPROVED
            entry.note_reviewed_at = now
        else:
            return _error("Nothing here was hidden.", 409, "not_hidden")

    db.session.commit()
    logger.info(
        "feedback_moderated action=%s entry=%s moderator=%s",
        sanitize_log_value(action),
        entry.id,
        moderator.id,
    )
    return jsonify({"entry": _moderation_row(entry, now)})


@feedback_bp.route("/moderation/reminder", methods=["POST"])
def moderation_reminder():
    """Cloud Scheduler calls this with ADMIN_API_TOKEN (decision R3)."""
    refused = require_admin()
    if refused:
        return refused
    held = RecipeFeedback.query.filter(
        RecipeFeedback.note_status == NOTE_HELD,
        RecipeFeedback.deleted_at.is_(None),
        RecipeFeedback.entry_hidden.is_(False),
    ).count()
    sent = feedback_mail.send_held_reminder(held) if held else False
    return jsonify({"held": held, "sent": sent})


# --------------------------------------------------------------------------
# Public summary for /r/<slug>
# --------------------------------------------------------------------------


def public_feedback_summary(recipe: Recipe, now: datetime | None = None) -> dict[str, Any] | None:
    """What the public page shows, or None when the query fails (render no block).

    One query and one age predicate feed the count, the average, the notes and
    ``aggregateRating`` together, so they cannot disagree at the 24-hour mark (E4).
    """
    now = now or datetime.utcnow()
    try:
        rows = (
            db.session.query(RecipeFeedback, User)
            .join(User, User.id == RecipeFeedback.user_id)
            .filter(
                RecipeFeedback.recipe_id == recipe.id,
                RecipeFeedback.deleted_at.is_(None),
                RecipeFeedback.entry_hidden.is_(False),
                User.created_at <= now - ACCOUNT_AGE_BEFORE_PUBLIC,
            )
            .order_by(RecipeFeedback.created_at.desc(), RecipeFeedback.id.desc())
            .all()
        )
    except Exception:  # noqa: BLE001 — the recipe page must not go down with this
        logger.exception("feedback_summary_failed recipe=%s", recipe.id)
        db.session.rollback()
        return None

    author = None
    cooks: list[tuple[RecipeFeedback, User]] = []
    for entry, user in rows:
        if recipe.user_id is not None and user.id == recipe.user_id:
            author = entry
        else:
            cooks.append((entry, user))

    count = len(cooks)
    average = (
        round(sum(entry.stars for entry, _ in cooks) / count, 1)
        if count >= AGGREGATE_RATING_MIN_COOKS
        else None
    )
    public_notes = [
        {
            "text": entry.note,
            "name": first_name(user),
            "month": entry.created_at.strftime("%B %Y"),
        }
        for entry, user in cooks
        if entry.note and entry.note_status in PUBLIC_NOTE_STATUSES
    ]
    return {
        "count": count,
        "average": average,
        "author": (
            {
                "stars": author.stars,
                "note": (
                    author.note
                    if author.note and author.note_status in PUBLIC_NOTE_STATUSES
                    else None
                ),
            }
            if author is not None
            else None
        ),
        "notes": public_notes[:PUBLIC_NOTES_CAP],
        "notes_truncated": len(public_notes) > PUBLIC_NOTES_CAP,
    }


def aggregate_rating_json_ld(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """``aggregateRating`` only at 3 or more non-author entries (premise 4)."""
    if not summary or summary.get("average") is None:
        return None
    return {
        "@type": "AggregateRating",
        "ratingValue": summary["average"],
        "ratingCount": summary["count"],
        "bestRating": STARS_MAX,
        "worstRating": STARS_MIN,
    }
