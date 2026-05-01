"""Drop max_open_prs and pr_gate_sleep from system_config (Phase 6.5)

Revision ID: 039
Revises: 038
Create Date: 2026-05-01

The legacy MAX_OPEN_PRS coder gate is gone. In sprint-PR mode there's
exactly one open PR per product (the sprint PR); pausing on its
existence would block every coder run forever. The replacement is an
invariant alert (`orchestrator.sprint_pr.check_open_pr_invariant`) that
warns when count > 1 but never gates work.

Drops both columns. The downgrade restores them as nullable so older
code that might still try to write them won't crash, but they no longer
have any behavioral effect.
"""
from alembic import op
import sqlalchemy as sa


revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("system_config", "max_open_prs")
    op.drop_column("system_config", "pr_gate_sleep")


def downgrade():
    op.add_column("system_config", sa.Column("max_open_prs", sa.Integer, nullable=True))
    op.add_column("system_config", sa.Column("pr_gate_sleep", sa.Integer, nullable=True))
