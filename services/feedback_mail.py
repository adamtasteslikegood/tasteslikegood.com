"""Moderation email for recipe feedback (KAN-356, decision R3).

Two notices, both notification only: one when a new note joins the review
queue, and a reminder of held notes that Cloud Scheduler triggers through
``POST /api/feedback/moderation/reminder``. Neither carries an approve or reject
link; both link to the app's moderation view.

Transport: the Gmail API, sending as an address on the tasteslikegood.org
Workspace, with a send-only grant. The Flask service account signs a
domain-wide-delegation token through the IAM Credentials API, so no key file
is needed on Cloud Run.

Settings:

- ``FEEDBACK_MODERATION_EMAIL_ENABLED``: ``1``/``true``/``yes``/``on`` to send.
  Anything else logs ``feedback_moderation_email_skipped`` and sends nothing.
- ``FEEDBACK_MODERATION_EMAIL_FROM``: the Workspace address mail is sent as.
- ``FEEDBACK_MODERATION_EMAIL_SERVICE_ACCOUNT``: optional; the service account
  that holds the delegation. Defaults to the runtime's own account.
- Recipients are the moderators, ``FEEDBACK_MODERATOR_EMAILS``.

Every function here returns a bool and never raises: a failed send must never
fail the entry write (eng review, failure modes).
"""

from __future__ import annotations

import base64
import logging
import os
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
TOKEN_URI = "https://oauth2.googleapis.com/token"
_TRUE = {"1", "true", "yes", "on"}


def email_enabled() -> bool:
    return os.environ.get("FEEDBACK_MODERATION_EMAIL_ENABLED", "").strip().lower() in _TRUE


def _moderation_url() -> str:
    base = os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/moderation"


def _recipients() -> list[str]:
    raw = os.environ.get("FEEDBACK_MODERATOR_EMAILS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _gmail_service(sender: str) -> Any:  # pragma: no cover - needs Google credentials
    """A Gmail client acting as ``sender`` via keyless domain-wide delegation."""
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    request = Request()
    source.refresh(request)
    account = os.environ.get("FEEDBACK_MODERATION_EMAIL_SERVICE_ACCOUNT") or getattr(
        source, "service_account_email", None
    )
    signer = iam.Signer(request, source, account)
    credentials = service_account.Credentials(
        signer, account, TOKEN_URI, scopes=[GMAIL_SEND_SCOPE], subject=sender
    )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _send(kind: str, subject: str, body: str) -> bool:
    if not email_enabled():
        logger.info("feedback_moderation_email_skipped kind=%s reason=disabled", kind)
        return False
    sender = os.environ.get("FEEDBACK_MODERATION_EMAIL_FROM", "").strip()
    recipients = _recipients()
    if not sender or not recipients:
        logger.warning("feedback_moderation_email_skipped kind=%s reason=not_configured", kind)
        return False
    message = EmailMessage()
    message["To"] = ", ".join(recipients)
    message["From"] = sender
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        _gmail_service(sender).users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception:  # noqa: BLE001 — logged, never fatal
        logger.exception("feedback_moderation_email_failed kind=%s", kind)
        return False
    logger.info("feedback_moderation_email_sent kind=%s", kind)
    return True


def notify_new_held_note(entry: Any) -> bool:
    """One email when a new note joins the queue. The note text is not included."""
    try:
        recipe_name = entry.recipe.name if entry.recipe else "a recipe"
    except Exception:  # noqa: BLE001
        recipe_name = "a recipe"
    return _send(
        "new_note",
        "A recipe note is waiting for review",
        (
            f"A new note on {recipe_name} is waiting for review.\n\n"
            f"Review it in the app: {_moderation_url()}\n"
        ),
    )


def send_held_reminder(held_count: int) -> bool:
    """The scheduled reminder of notes still waiting."""
    noun = "note is" if held_count == 1 else "notes are"
    return _send(
        "reminder",
        f"{held_count} recipe {noun} waiting for review",
        (
            f"{held_count} recipe {noun} waiting for review.\n\n"
            f"Review them in the app: {_moderation_url()}\n"
        ),
    )
