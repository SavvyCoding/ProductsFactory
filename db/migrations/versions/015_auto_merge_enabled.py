"""add auto_merge_enabled to system_config

Revision ID: 015
Revises: 014
Create Date: 2026-04-14
"""
from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "system_config",
        sa.Column("auto_merge_enabled", sa.Boolean(), nullable=True),
    )


def downgrade():
    op.drop_column("system_config", "auto_merge_enabled")
