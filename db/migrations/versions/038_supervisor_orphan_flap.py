"""Supervisor: kill_recovery + orphan_approved + rapid_flap config

Revision ID: 038
Revises: 037
Create Date: 2026-05-01

Adds three more detectors' enable flags and tunables to system_config:
- kill_recovery: bumps fix_attempts on every assigned feature still in agent
  state when a session dies non-zero. Catches the watchdog-kill loop where
  the same feature is re-assigned and re-killed without ever advancing
  fix_attempts toward the auto-Block route.
- orphan_approved: triggers /plan-sprints when N+ Approved features sit
  unsprinted for ≥orphan_approved_min_age_hours.
- rapid_flap: routes a feature to the Blocked sprint when its status
  transitions exceed rapid_flap_min_transitions in
  rapid_flap_window_hours. Uses the existing /sprints/blocked/route
  endpoint and feature_changelog table as the data source.
"""
from alembic import op
import sqlalchemy as sa


revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("system_config",
        sa.Column("supervisor_kill_recovery_enabled", sa.Boolean, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_orphan_approved_enabled", sa.Boolean, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_orphan_approved_min_age_hours", sa.Integer, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_orphan_approved_threshold", sa.Integer, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_rapid_flap_enabled", sa.Boolean, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_rapid_flap_window_hours", sa.Integer, nullable=True))
    op.add_column("system_config",
        sa.Column("supervisor_rapid_flap_min_transitions", sa.Integer, nullable=True))


def downgrade():
    op.drop_column("system_config", "supervisor_rapid_flap_min_transitions")
    op.drop_column("system_config", "supervisor_rapid_flap_window_hours")
    op.drop_column("system_config", "supervisor_rapid_flap_enabled")
    op.drop_column("system_config", "supervisor_orphan_approved_threshold")
    op.drop_column("system_config", "supervisor_orphan_approved_min_age_hours")
    op.drop_column("system_config", "supervisor_orphan_approved_enabled")
    op.drop_column("system_config", "supervisor_kill_recovery_enabled")
