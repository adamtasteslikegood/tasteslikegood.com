"""Reassign-or-unpublish guest-owned public recipes (eng-review 3A/D8).

Publishing is being gated to authenticated (OAuth) users so there is an
accountable owner behind every public /r/<slug> page. Rows published before
the gate may be guest-owned — most likely the founder's own pre-sign-in
session. Unpublishing those would 404 URLs Google has already indexed, so:

- With ``reassign_email`` set (env ``GUEST_PUBLIC_REASSIGN_EMAIL`` in the
  migration): guest-owned public rows are reassigned to that user and their
  pages stay live.
- Without it: they are unpublished (the accountability-safe default).
- An email that matches no user raises, aborting the migration — and with it
  the deploy — rather than guessing.

Used by the Alembic migration and unit-tested directly (same pattern as
scripts/backfill_slugs.py).
"""

import logging
from typing import Dict, Optional

from models.recipe import Recipe
from models.user import User

logger = logging.getLogger(__name__)


def run_gate(session, reassign_email: Optional[str] = None) -> Dict[str, int]:
    """Apply the gate to pre-existing rows. Returns a summary dict."""
    rows = (
        session.query(Recipe)
        .filter(Recipe.is_public.is_(True), Recipe.user_id.is_(None))
        .all()
    )
    summary = {"found": len(rows), "reassigned": 0, "unpublished": 0}
    if not rows:
        logger.info("gate_guest_public_recipes: no guest-owned public rows")
        return summary

    logger.info(
        "gate_guest_public_recipes: %d guest-owned public rows: %s",
        len(rows),
        ", ".join(r.slug or r.id for r in rows),
    )

    if reassign_email:
        user = session.query(User).filter_by(email=reassign_email).one_or_none()
        if user is None:
            raise RuntimeError(
                f"GUEST_PUBLIC_REASSIGN_EMAIL={reassign_email!r} matches no user; "
                "aborting so no public page silently changes ownership"
            )
        for row in rows:
            row.user_id = user.id
            row.guest_session_id = None
        summary["reassigned"] = len(rows)
        logger.info(
            "gate_guest_public_recipes: reassigned %d rows to user %s",
            len(rows),
            reassign_email,
        )
    else:
        for row in rows:
            row.is_public = False
        summary["unpublished"] = len(rows)
        logger.warning(
            "gate_guest_public_recipes: unpublished %d guest-owned rows "
            "(set GUEST_PUBLIC_REASSIGN_EMAIL to reassign instead)",
            len(rows),
        )

    session.commit()
    return summary
