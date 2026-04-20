"""Remove skip_design column from features — all features go through the designer pipeline

Revision ID: 028
Revises: 027
Create Date: 2026-04-20
"""
from alembic import op


revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("features", "skip_design")


def downgrade():
    import sqlalchemy as sa
    op.add_column("features", sa.Column("skip_design", sa.Boolean(), nullable=False, server_default="false"))
