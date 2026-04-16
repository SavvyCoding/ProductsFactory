"""add optimistic-lock version column to features

Every write via PATCH /api/features/{id} increments version.
Callers that supply expected_version get a 409 on concurrent modification
instead of silently overwriting a newer state.

Revision ID: 020
Revises: 019
Create Date: 2026-04-15
"""
from alembic import op
import sqlalchemy as sa

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "features",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade():
    op.drop_column("features", "version")
