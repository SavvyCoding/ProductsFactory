"""add compound index on features(product_id, status, updated_at)

Speeds up next-for-persona and reset_stuck queries which filter by product_id + status
and order by updated_at. Without this index PostgreSQL does a full table scan on every
poller cycle.

Revision ID: 019
Revises: 018
Create Date: 2026-04-15
"""
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_features_product_status_updated",
        "features",
        ["product_id", "status", "updated_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_features_product_status_updated", table_name="features")
