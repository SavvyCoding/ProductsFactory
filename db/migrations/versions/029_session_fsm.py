"""Session FSM: explicit status + deadline + heartbeat for robust lifecycle

Adds columns to the sessions table so the orchestrator can manage sessions
authoritatively from the DB alone, without parsing docker output or file mtimes.

  status             — FSM: pending/starting/running/wrapping/ended/killed/orphaned
  heartbeat_at       — set by agents via PM API; readers detect stuck sessions
  expected_deadline  — started_at + SESSION_TIMEOUT_MIN; watchdog kills past this
  kill_reason        — diagnostic when status=killed/orphaned

Also creates the session_events audit log for lifecycle transitions.

Revision ID: 029
Revises: 028
Create Date: 2026-04-24
"""
from alembic import op
import sqlalchemy as sa


revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade():
    # 1. FSM columns on sessions
    op.add_column(
        "sessions",
        sa.Column("status", sa.Text(), nullable=False, server_default="ended"),
    )
    op.add_column(
        "sessions",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("expected_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("kill_reason", sa.Text(), nullable=True),
    )

    # 2. Backfill status for existing rows:
    #    - ended_at IS NULL → 'orphaned' (we can't trust these; let harvester reconcile)
    #    - ended_at NOT NULL + exit_code=0 → 'ended'
    #    - ended_at NOT NULL + exit_code!=0 → 'killed'
    op.execute("""
        UPDATE sessions SET status = CASE
            WHEN ended_at IS NULL THEN 'orphaned'
            WHEN exit_code = 0 THEN 'ended'
            ELSE 'killed'
        END
    """)

    # 3. Index the hot path: watchdog queries WHERE status IN ('pending','starting','running')
    op.create_index(
        "ix_sessions_status_deadline",
        "sessions",
        ["status", "expected_deadline"],
    )

    # 4. Audit log for lifecycle transitions
    op.create_table(
        "session_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),           # launched/heartbeat/killed/ended/reconciled
        sa.Column("detail", sa.Text(), nullable=True),           # optional JSON blob
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_session_events_session", "session_events", ["session_id", "created_at"])


def downgrade():
    op.drop_index("ix_session_events_session", table_name="session_events")
    op.drop_table("session_events")
    op.drop_index("ix_sessions_status_deadline", table_name="sessions")
    op.drop_column("sessions", "kill_reason")
    op.drop_column("sessions", "expected_deadline")
    op.drop_column("sessions", "heartbeat_at")
    op.drop_column("sessions", "status")
