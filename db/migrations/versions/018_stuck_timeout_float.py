"""change stuck_feature_timeout_hours from Integer to Float

Revision ID: 018
Revises: 017
Create Date: 2026-04-15
"""
from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "system_config",
        "stuck_feature_timeout_hours",
        type_=sa.Float(),
        existing_type=sa.Integer(),
        existing_nullable=True,
    )


def downgrade():
    op.alter_column(
        "system_config",
        "stuck_feature_timeout_hours",
        type_=sa.Integer(),
        existing_type=sa.Float(),
        existing_nullable=True,
    )
