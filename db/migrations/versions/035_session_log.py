"""Add sessions.log column for agent transcript snapshot

Revision ID: 035
Revises: 034
Create Date: 2026-04-30

Captures the agent's stdout transcript per session so the History tab can
show what the agent actually did. Snapshot is taken at session close from
the PM API's in-memory per-product log buffer (filtered by session_uid).
NULL = no transcript captured (e.g. sessions that ended before this
landed, or sessions where the buffer rolled over before close).
"""
from alembic import op
import sqlalchemy as sa


revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("log", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("sessions", "log")
