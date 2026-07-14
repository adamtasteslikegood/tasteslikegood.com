"""Gate guest-published recipes: reassign to an accountable owner or unpublish.

Publishing is now restricted to authenticated (OAuth) users (see
repositories/db_recipe_repository._gate_is_public). This data migration deals
with rows published before the gate existed: guest-owned public recipes.

Behavior (see scripts/gate_guest_public_recipes.py):
- If GUEST_PUBLIC_REASSIGN_EMAIL is set in the migration job's environment,
  guest-owned public rows are reassigned to that user — their /r/<slug> pages
  (already in Google's index) stay live under an accountable owner.
- If unset, they are unpublished.
- If the email matches no user, the migration raises and the deploy aborts
  (the old Flask revision keeps serving traffic).

Inventory the affected rows first:
    SELECT id, slug FROM recipe WHERE is_public AND user_id IS NULL;

Revision ID: e91b47a2c5d3
Revises: c60f6530f4ff
Create Date: 2026-07-07
"""

import logging
import os

from alembic import op
from sqlalchemy.orm import Session

# revision identifiers, used by Alembic.
revision = "e91b47a2c5d3"
down_revision = "c60f6530f4ff"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade():
    # Data-only migration: no schema change. Runs inside the app context that
    # `flask db upgrade` provides, so app modules are importable.
    from scripts.gate_guest_public_recipes import run_gate

    session = Session(bind=op.get_bind())
    summary = run_gate(session, reassign_email=os.environ.get("GUEST_PUBLIC_REASSIGN_EMAIL"))
    logger.info("gate_guest_published_recipes: %s", summary)


def downgrade():
    # Irreversible data migration: we cannot know which rows were guest-owned
    # before reassignment/unpublish. Intentionally a no-op.
    pass
