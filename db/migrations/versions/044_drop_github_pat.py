"""drop system_config.github_pat (App-token migration complete)

Revision ID: 044
Revises: 043
Create Date: 2026-05-26

Migration 042 added the GitHub App auth columns and kept `github_pat` as a
deprecation cushion. As of this revision the cushion is removed:

  - No code path consumes the value. The admin save handler at
    `/admin/settings` stopped accepting it as a Form field on 2026-05-14;
    the greenfield-create validator's `_has_pat` branch (which would have
    allowed creation against a stored PAT) is removed in the same PR as
    this migration. CLAUDE.md → "Auth & Security" already declares
    GitHub App is the only git-auth path.
  - The defensive PAT-shape regex in `orchestrator/infra/redaction.py`
    is kept (it scrubs accidental log lines regardless of DB schema).

Downgrade restores the column as nullable Text. Any value stored under
the old column is lost on upgrade — that's accepted because the live DB
had `github_pat IS NULL` at migration time and no production caller
reads the column.
"""
from alembic import op
import sqlalchemy as sa


revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("system_config", "github_pat")


def downgrade():
    op.add_column(
        "system_config",
        sa.Column("github_pat", sa.Text(), nullable=True),
    )
