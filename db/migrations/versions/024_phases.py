"""add phases table and sprints.phase_id

phases — top-level planning containers that group sprints under a
         named phase (e.g. "Phase 1: Foundation").
sprints.phase_id — optional FK to phases so sprints belong to a phase.

Revision ID: 024
Revises: 023
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "phases",
        sa.Column("id",         sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name",       sa.Text(), nullable=False),
        sa.Column("goal",       sa.Text(), nullable=True),
        sa.Column("order",      sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status",     sa.Text(), nullable=False, server_default="planned"),
        sa.CheckConstraint("status IN ('planned', 'active', 'completed')", name="ck_phases_status"),
    )
    op.create_index("ix_phases_product", "phases", ["product_id"])

    op.add_column("sprints", sa.Column(
        "phase_id", sa.Integer(),
        sa.ForeignKey("phases.id", ondelete="SET NULL"),
        nullable=True,
    ))
    op.create_index("ix_sprints_phase_id", "sprints", ["phase_id"])


def downgrade():
    op.drop_index("ix_sprints_phase_id", table_name="sprints")
    op.drop_column("sprints", "phase_id")
    op.drop_index("ix_phases_product", table_name="phases")
    op.drop_table("phases")
