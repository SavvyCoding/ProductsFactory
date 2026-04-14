"""add backend column to sessions

Revision ID: 011
Revises: 010
"""
from alembic import op
import sqlalchemy as sa

revision = '011'
down_revision = '010'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sessions', sa.Column('backend', sa.String(16), nullable=True))


def downgrade():
    op.drop_column('sessions', 'backend')
