"""add operational settings to system_config

Revision ID: 009
Revises: 008
"""
from alembic import op
import sqlalchemy as sa

revision = '009'
down_revision = '008'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('system_config', sa.Column('poll_interval',               sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('session_timeout_minutes',     sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('stale_threshold_minutes',     sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('auth_check_timeout',          sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('max_open_prs',                sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('pr_gate_sleep',               sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('stuck_feature_timeout_hours', sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('max_features_per_run',        sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('brownfield_file_threshold',   sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('agent_backend',               sa.Text(),    nullable=True))
    op.add_column('system_config', sa.Column('ollama_host',                 sa.Text(),    nullable=True))
    op.add_column('system_config', sa.Column('designer_model',              sa.Text(),    nullable=True))
    op.add_column('system_config', sa.Column('coder_model',                 sa.Text(),    nullable=True))
    op.add_column('system_config', sa.Column('ollama_timeout',              sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('bash_timeout',                sa.Integer(), nullable=True))
    op.add_column('system_config', sa.Column('max_turns',                   sa.Integer(), nullable=True))


def downgrade():
    for col in [
        'poll_interval', 'session_timeout_minutes', 'stale_threshold_minutes',
        'auth_check_timeout', 'max_open_prs', 'pr_gate_sleep',
        'stuck_feature_timeout_hours', 'max_features_per_run', 'brownfield_file_threshold',
        'agent_backend', 'ollama_host', 'designer_model', 'coder_model',
        'ollama_timeout', 'bash_timeout', 'max_turns',
    ]:
        op.drop_column('system_config', col)
