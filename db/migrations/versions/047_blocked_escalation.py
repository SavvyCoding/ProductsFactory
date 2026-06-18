"""blocked-feature premium-model escalation: 'Stuck' status + escalation flags + config

Revision ID: 047
Revises: 046
Create Date: 2026-06-18

Adds the schema for auto-escalating Blocked features to a stronger, user-
configured LLM (Claude API / OpenAI) for a bounded premium pass. See
docs/blocked_escalation_plan.md for the full design.

- features.status gains 'Stuck' — the terminal state for a feature that BOTH
  the base model AND the premium escalation pass failed. Terminal and invisible
  to the escalation reprocessor → a feature can never loop.
- features.escalation_active — set while a feature is in its one premium pass;
  drives model routing in docker_runner and the re-block → 'Stuck' branch.
- sessions.is_escalation — tags premium sessions so the daily-USD-cap query
  sums only their cost_usd.
- system_config gains the global escalation knobs (UI-configurable) + the
  Anthropic/OpenAI keys.

Downgrade re-narrows the status constraint; any existing 'Stuck' rows must be
retyped first (the constraint recreate will fail loudly otherwise — deliberate).
"""
import sqlalchemy as sa
from alembic import op


revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None

_STATUSES_WITHOUT_STUCK = (
    "'Pending', 'Approved', 'Designing', 'Designed', 'Implementing', "
    "'Implemented', 'Reviewing', 'Reviewed', 'Testing', 'Committed', "
    "'Pushed', 'Blocked', 'Rejected', 'Reverted', 'Deferred'"
)
_STATUSES_WITH_STUCK = _STATUSES_WITHOUT_STUCK + ", 'Stuck'"


def upgrade():
    # 1. Widen the feature-status constraint to include 'Stuck'.
    op.drop_constraint("ck_features_status", "features", type_="check")
    op.create_check_constraint(
        "ck_features_status", "features",
        f"status IN ({_STATUSES_WITH_STUCK})",
    )

    # 2. Per-feature escalation flag (drives model routing + re-block routing).
    op.add_column("features", sa.Column(
        "escalation_active", sa.Boolean(), nullable=False, server_default=sa.false()))

    # 3. Tag premium sessions for the daily cost-cap sum.
    op.add_column("sessions", sa.Column(
        "is_escalation", sa.Boolean(), nullable=False, server_default=sa.false()))

    # 4. Global escalation config (all UI-editable on the Settings card).
    op.add_column("system_config", sa.Column("blocked_escalation_enabled", sa.Boolean()))
    op.add_column("system_config", sa.Column("blocked_escalation_backend", sa.Text()))
    op.add_column("system_config", sa.Column("blocked_escalation_model", sa.Text()))
    op.add_column("system_config", sa.Column("blocked_escalation_max_attempts", sa.Integer()))
    op.add_column("system_config", sa.Column("blocked_escalation_daily_usd_cap", sa.Numeric(10, 2)))
    op.add_column("system_config", sa.Column("anthropic_api_key", sa.Text()))
    op.add_column("system_config", sa.Column("openai_api_key", sa.Text()))


def downgrade():
    op.drop_column("system_config", "openai_api_key")
    op.drop_column("system_config", "anthropic_api_key")
    op.drop_column("system_config", "blocked_escalation_daily_usd_cap")
    op.drop_column("system_config", "blocked_escalation_max_attempts")
    op.drop_column("system_config", "blocked_escalation_model")
    op.drop_column("system_config", "blocked_escalation_backend")
    op.drop_column("system_config", "blocked_escalation_enabled")
    op.drop_column("sessions", "is_escalation")
    op.drop_column("features", "escalation_active")
    op.drop_constraint("ck_features_status", "features", type_="check")
    op.create_check_constraint(
        "ck_features_status", "features",
        f"status IN ({_STATUSES_WITHOUT_STUCK})",
    )
