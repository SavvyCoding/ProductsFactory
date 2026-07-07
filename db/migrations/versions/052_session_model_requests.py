"""sessions.model_requests — per-model API request counts (fallback-aware)

Revision ID: 052
Revises: 051
Create Date: 2026-07-06

`sessions.model` (migration 051) records the model a session was configured to
PREFER (the primary of the fallback chain). But the agent silently falls back
to the next model when the primary is busy/errors — e.g. an architect session
whose deepseek-v4-pro primary is unavailable is served by glm-5.1 (its 2nd
choice). Those fallback requests were invisible, so the dashboard's per-model
view disagreed with the Ollama usage dashboard (which counts actual requests).

`model_requests` is a JSONB map {model: request_count} of the ACTUAL model that
served each request this session, accumulated by ollama_agent and PATCHed at
session end. The Model Usage panels sum it for a request-level, fallback-aware
"Requests" column that reconciles with Ollama. NULL for sessions predating this
migration and for non-Ollama backends that don't report it.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("model_requests", postgresql.JSONB()))


def downgrade():
    op.drop_column("sessions", "model_requests")
