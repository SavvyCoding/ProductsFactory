"""Per-persona Claude model routing: heavy/light tier columns + explicit map

Adds two tier columns and a JSONB map so Sonnet/Haiku can be routed per
persona without redeploying code:

  claude_model_heavy   — coder/reviewer/designer/security/qa      (default Sonnet)
  claude_model_light   — planner/documenter/retrospective/...     (default Haiku)
  claude_model_map     — explicit persona→model overrides (JSONB)

Cascade: sys_cfg.claude_model_map[persona] > claude_model_heavy/light > claude_model > hardcoded default.

Revision ID: 030
Revises: 029
Create Date: 2026-04-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("system_config",
        sa.Column("claude_model_heavy", sa.Text(), nullable=True))
    op.add_column("system_config",
        sa.Column("claude_model_light", sa.Text(), nullable=True))
    op.add_column("system_config",
        sa.Column("claude_model_map", JSONB(), nullable=True))


def downgrade():
    op.drop_column("system_config", "claude_model_map")
    op.drop_column("system_config", "claude_model_light")
    op.drop_column("system_config", "claude_model_heavy")
