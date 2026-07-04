"""coder ladder telemetry: features.max_ladder_tier + sessions.ladder_model/ladder_tier

Revision ID: 050
Revises: 049
Create Date: 2026-07-03

Durable escalation telemetry so ladder usage is cleanly countable, replacing
the log-grep + escalation_step-snapshot guesswork (escalation_step gets reset on
unblock; container recreates truncate logs; glm sessions aren't is_escalation-
tagged because glm is an Ollama model).

- features.max_ladder_tier — the HIGHEST ladder tier this feature ever ran a coder
  session on (0 = First Attempt only, 1 = reached glm, 2 = reached the next tier…).
  MONOTONIC: only ever raised, NEVER reset (unlike escalation_step). One GROUP BY
  on (max_ladder_tier, status) answers "how many stayed on First Attempt vs
  escalated, and how many of each shipped".
- sessions.ladder_tier / ladder_model — per coder session, the tier index + the
  actual model it ran (minimax-m3 / glm-5.2 / …), for granular per-session detail.
"""
import sqlalchemy as sa
from alembic import op


revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("features", sa.Column(
        "max_ladder_tier", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sessions", sa.Column("ladder_tier", sa.Integer()))
    op.add_column("sessions", sa.Column("ladder_model", sa.Text()))


def downgrade():
    op.drop_column("sessions", "ladder_model")
    op.drop_column("sessions", "ladder_tier")
    op.drop_column("features", "max_ladder_tier")
