"""add claude profile columns to system_config

Revision ID: 010
Revises: 009
"""
from alembic import op
import sqlalchemy as sa

revision = '010'
down_revision = '009'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('system_config', sa.Column('claude_model',           sa.Text(), nullable=True))
    op.add_column('system_config', sa.Column('claude_credentials_dir', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('system_config', 'claude_credentials_dir')
    op.drop_column('system_config', 'claude_model')
