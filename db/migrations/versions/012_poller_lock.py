"""add poller distributed lock columns to system_config

Revision ID: 012
Revises: 011
"""
from alembic import op
import sqlalchemy as sa

revision = '012'
down_revision = '011'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('system_config', sa.Column('poller_pid',          sa.Integer(),              nullable=True))
    op.add_column('system_config', sa.Column('poller_host',         sa.Text(),                 nullable=True))
    op.add_column('system_config', sa.Column('poller_locked_at',    sa.DateTime(timezone=True), nullable=True))
    op.add_column('system_config', sa.Column('poller_heartbeat_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column('system_config', 'poller_heartbeat_at')
    op.drop_column('system_config', 'poller_locked_at')
    op.drop_column('system_config', 'poller_host')
    op.drop_column('system_config', 'poller_pid')
