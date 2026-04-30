"""Per-product Blocked sprint + configurable max_fix_attempts

Revision ID: 036
Revises: 035
Create Date: 2026-04-30

Adds a per-product holding-pen sprint for features that have failed
`system_config.max_fix_attempts` times (default 5) so they don't keep
looping through the agent pipeline. PMs review + reroute from there.

- sprints.kind: "normal" (default — counts toward DoD, sprint cap, etc.)
                 or "blocked" (the holdpen — excluded from active-sprint
                 selection, DoD computation, sprint cap, sprint-PR
                 provisioning, and round-robin coder picks).
- system_config.max_fix_attempts: configurable threshold; existing code
  reads MAX_FIX_ATTEMPTS env var, this surfaces it in the DB so the
  PM can change it without a restart.
"""
from alembic import op
import sqlalchemy as sa


revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sprints",
        sa.Column(
            "kind",
            sa.String(16),
            nullable=False,
            server_default="normal",
        ),
    )
    op.create_check_constraint(
        "ck_sprints_kind",
        "sprints",
        "kind IN ('normal', 'blocked')",
    )
    op.add_column(
        "system_config",
        sa.Column("max_fix_attempts", sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column("system_config", "max_fix_attempts")
    op.drop_constraint("ck_sprints_kind", "sprints", type_="check")
    op.drop_column("sprints", "kind")
