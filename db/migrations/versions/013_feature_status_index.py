"""add index on features.status for persona queries

Revision ID: 013
Revises: 012
"""
from alembic import op

revision = '013'
down_revision = '012'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_features_status", "features", ["status"])


def downgrade():
    op.drop_index("ix_features_status", table_name="features")
