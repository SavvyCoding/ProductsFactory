"""coder escalation tiers: dedicated tier counter + coder_tiers ladder config

Revision ID: 049
Revises: 048
Create Date: 2026-07-02

Replaces the single global premium-escalation tier (migration 047) with a
configurable, coder-only **model ladder** — a first model plus up to three
stronger escalation models, each naming a (backend, model, #attempts). See
docs/design/coder_escalation_tiers.md.

Two design invariants baked into the schema here:

- `features.escalation_step` is a DEDICATED cumulative counter, distinct from
  `fix_attempts`. `fix_attempts` is an overloaded control signal the blocked
  re-processor writes (=0 on divergent-review/rapid-flap→Approved, =4 on the
  code-quality path); keying tiers off it would reset an escalated feature to
  the cheapest model on the very retry it earned by failing. The ladder owns
  `escalation_step`; the re-processor never touches it. The active tier is
  derived by walking the cumulative `max_attempts` of each tier (see
  orchestrator/coder_tiers.resolve_coder_tier), so per-tier attempt budgets need
  only this ONE counter.

- The ladder is the coder's SOLE routing path — the legacy migration-047 premium
  tier is retired (no master on/off gate). Behavior is preserved without config:
  when `coder_tiers` is empty the runtime builds a single default tier from the
  current `coder_model` (diagnostician fires at the existing threshold), so an
  unconfigured environment behaves exactly as today. Adding escalation tiers via
  the UI turns on climbing.

`coder_tiers` is a JSONB ordered list (1–4 entries) rather than flat columns so
the tier COUNT is variable (First Attempt + up to 3 escalations). Shape:
  [{"backend": "ollama"|"claude-api"|"openai", "model": str,
    "max_attempts": int>=1, "enabled": bool}, ...]
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade():
    # Dedicated cumulative ladder counter (0 = start of First Attempt). Only the
    # ladder advances it; the blocked re-processor must never write it. The
    # active tier is derived by walking cumulative per-tier max_attempts.
    op.add_column("features", sa.Column(
        "escalation_step", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint(
        "ck_features_escalation_step", "features", "escalation_step >= 0")

    # Coder model ladder (JSONB list). Nullable: when empty the runtime builds a
    # single default tier from coder_model, so behavior is preserved with no config.
    op.add_column("system_config", sa.Column(
        "coder_tiers", JSONB()))


def downgrade():
    op.drop_column("system_config", "coder_tiers")
    op.drop_constraint("ck_features_escalation_step", "features", type_="check")
    op.drop_column("features", "escalation_step")
