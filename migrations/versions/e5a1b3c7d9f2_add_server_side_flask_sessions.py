"""Add server-side flask_sessions table for Flask-Session

Stores session data in PostgreSQL instead of cookies.
Benefits: survives container restarts, no 4KB cookie limit,
OAuth tokens kept server-side.

Revision ID: e5a1b3c7d9f2
Revises: 71f3f5f0c2b1
Create Date: 2026-03-20 02:15:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e5a1b3c7d9f2"
down_revision = "71f3f5f0c2b1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "flask_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.String(length=255), unique=True, nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        sa.Column("expiry", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_flask_sessions_session_id", "flask_sessions", ["session_id"], unique=True)
    op.create_index("ix_flask_sessions_expiry", "flask_sessions", ["expiry"])


def downgrade():
    op.drop_index("ix_flask_sessions_expiry", table_name="flask_sessions")
    op.drop_index("ix_flask_sessions_session_id", table_name="flask_sessions")
    op.drop_table("flask_sessions")
