"""sessions.model — the resolved LLM every session ran, for ALL personas

Revision ID: 051
Revises: 050
Create Date: 2026-07-06

`ladder_model` (migration 050) is written ONLY for coder sessions (the escalation
ladder tier). Designer / reviewer / architect / code_auditor / planner / analytics
sessions carried no per-session model — the model was resolved at launch from
`ollama_model_map[persona]` but never persisted, so the dashboard's Model Usage
panel bucketed all of them under `backend` ("ollama") and models like deepseek /
kimi / qwen3-coder:480b that do review/design/architecture work never appeared.

`sessions.model` is the general per-session model column for every persona: the
PRIMARY model of the resolved chain the agent actually ran (`MODELS[0]`), or the
coder ladder's exact tier model. The Model Usage panels group by
COALESCE(model, ladder_model, backend). `ladder_model` stays coder-specific for
the escalation telemetry (`/api/escalation-stats`).
"""
import sqlalchemy as sa
from alembic import op


revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("model", sa.Text()))


def downgrade():
    op.drop_column("sessions", "model")
