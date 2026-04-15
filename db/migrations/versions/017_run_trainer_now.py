"""add run_trainer_now to products

Revision ID: 017
Revises: 016
Create Date: 2026-04-15
"""
from alembic import op
import sqlalchemy as sa

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "products",
        sa.Column("run_trainer_now", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade():
    op.drop_column("products", "run_trainer_now")
