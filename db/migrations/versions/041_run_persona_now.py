"""add run_persona_now to products

Revision ID: 041
Revises: 040
Create Date: 2026-05-12

On-demand dispatch for the maintenance personas (documenter, analytics,
refactorer, devops, recommender). Stores the queued persona name; the
poller picks it up at the top of persona selection (alongside the
existing run_trainer_now flag) and clears it after picking.
"""
from alembic import op
import sqlalchemy as sa

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "products",
        sa.Column("run_persona_now", sa.String(length=50), nullable=True),
    )


def downgrade():
    op.drop_column("products", "run_persona_now")
