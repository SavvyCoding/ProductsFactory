"""add sprint DoD status, retro path, completed_at

dod_status   — JSONB tracking per-gate sign-offs:
               {all_features_done, no_open_prs, qa_passed, security_clean, retro_done}
retro_doc_path — path to retrospective markdown written by the retro agent
completed_at   — timestamp when sprint was auto- or manually completed

Revision ID: 026
Revises: 025
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sprints", sa.Column(
        "dod_status",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=True,
        server_default=sa.text("'{}'::jsonb"),
    ))
    op.add_column("sprints", sa.Column("retro_doc_path", sa.Text(), nullable=True))
    op.add_column("sprints", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("sprints", "completed_at")
    op.drop_column("sprints", "retro_doc_path")
    op.drop_column("sprints", "dod_status")
