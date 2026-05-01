"""Supervisor audit log + per-detector flags

Revision ID: 037
Revises: 036
Create Date: 2026-05-01

Phase 1 of the supervisor: rule-based detectors that catch stuck-state
patterns the deterministic orchestrator misses (gate-gaming coder
sessions, dirty PRs, etc.). Every detector firing — whether it
actually mutates state or just runs in dry-run — writes one audit row
here so PMs can see what the system has been doing on their behalf.

Schema:
- supervisor_actions: audit log, one row per detector firing
- system_config: per-detector enable flags + tunable thresholds
"""
from alembic import op
import sqlalchemy as sa


revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "supervisor_actions",
        sa.Column("id",          sa.Integer, primary_key=True),
        sa.Column("detector",    sa.String(40), nullable=False),
        sa.Column("product_id",  sa.Integer,
                  sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id",   sa.String(100), nullable=False),
        sa.Column("action",      sa.String(40), nullable=False),
        sa.Column("reason",      sa.Text, nullable=False),
        sa.Column("dry_run",     sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at",  sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_supervisor_actions_created",
                    "supervisor_actions", ["created_at"])
    op.create_index("ix_supervisor_actions_product_created",
                    "supervisor_actions", ["product_id", "created_at"])

    # Per-detector flags + dry-run kill switch
    op.add_column("system_config",
        sa.Column("supervisor_dry_run_only", sa.Boolean, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_false_success_enabled", sa.Boolean, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_dirty_pr_enabled", sa.Boolean, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_dirty_pr_min_age_min", sa.Integer, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_dirty_pr_idle_min", sa.Integer, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_auto_plan_enabled", sa.Boolean, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_auto_plan_min_unsprinted", sa.Integer, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_merge_stall_enabled", sa.Boolean, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_merge_stall_min_min", sa.Integer, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_overlap_pr_enabled", sa.Boolean, nullable=True))


def downgrade():
    op.drop_column("system_config", "supervisor_overlap_pr_enabled")
    op.drop_column("system_config", "supervisor_merge_stall_min_min")
    op.drop_column("system_config", "supervisor_merge_stall_enabled")
    op.drop_column("system_config", "supervisor_auto_plan_min_unsprinted")
    op.drop_column("system_config", "supervisor_auto_plan_enabled")
    op.drop_column("system_config", "supervisor_dirty_pr_idle_min")
    op.drop_column("system_config", "supervisor_dirty_pr_min_age_min")
    op.drop_column("system_config", "supervisor_dirty_pr_enabled")
    op.drop_column("system_config", "supervisor_false_success_enabled")
    op.drop_column("system_config", "supervisor_dry_run_only")
    op.drop_index("ix_supervisor_actions_product_created", table_name="supervisor_actions")
    op.drop_index("ix_supervisor_actions_created", table_name="supervisor_actions")
    op.drop_table("supervisor_actions")
