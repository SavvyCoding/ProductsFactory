"""Add run_now/custom_prompt/scheduling to products; cost tracking to sessions;
   slack webhook + webhook secret to system_config.

Revision ID: 003
Revises: 002
Create Date: 2026-04-03
"""

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── products ──────────────────────────────────────────────────────────────
    op.add_column("products", sa.Column("run_now", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("products", sa.Column("custom_prompt", sa.Text(), nullable=True))
    op.add_column("products", sa.Column("quiet_hours_start", sa.Integer(), nullable=True))
    op.add_column("products", sa.Column("quiet_hours_end",   sa.Integer(), nullable=True))
    op.add_column("products", sa.Column("daily_session_cap", sa.Integer(), nullable=True))

    # ── sessions ──────────────────────────────────────────────────────────────
    op.add_column("sessions", sa.Column("tokens_input",  sa.Integer(), nullable=True))
    op.add_column("sessions", sa.Column("tokens_output", sa.Integer(), nullable=True))
    op.add_column("sessions", sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True))

    # ── system_config ─────────────────────────────────────────────────────────
    op.add_column("system_config", sa.Column("slack_webhook_url",    sa.Text(), nullable=True))
    op.add_column("system_config", sa.Column("github_webhook_secret", sa.Text(), nullable=True))
    op.add_column("system_config", sa.Column("max_sessions_per_day", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("system_config", "max_sessions_per_day")
    op.drop_column("system_config", "github_webhook_secret")
    op.drop_column("system_config", "slack_webhook_url")
    op.drop_column("sessions", "cost_usd")
    op.drop_column("sessions", "tokens_output")
    op.drop_column("sessions", "tokens_input")
    op.drop_column("products", "daily_session_cap")
    op.drop_column("products", "quiet_hours_end")
    op.drop_column("products", "quiet_hours_start")
    op.drop_column("products", "custom_prompt")
    op.drop_column("products", "run_now")
