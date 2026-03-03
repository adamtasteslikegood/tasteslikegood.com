"""Add Cookbook model

Revision ID: d4f8c2e19a73
Revises: b8896f552679
Create Date: 2026-03-03 15:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4f8c2e19a73'
down_revision = 'b8896f552679'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'cookbook',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=True),
        sa.Column('cover_image', sa.String(length=500), nullable=True),
        sa.Column('recipe_ids', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('cookbook')
