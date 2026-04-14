"""add recommender_pending_threshold to system_config

Revision ID: 014
Revises: 013
Create Date: 2026-04-14
"""
from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "system_config",
        sa.Column("recommender_pending_threshold", sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column("system_config", "recommender_pending_threshold")
