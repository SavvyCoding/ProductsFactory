"""add ssh_keys_dir to system_config

Revision ID: 016
Revises: 015
Create Date: 2026-04-15
"""
from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "system_config",
        sa.Column("ssh_keys_dir", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("system_config", "ssh_keys_dir")
