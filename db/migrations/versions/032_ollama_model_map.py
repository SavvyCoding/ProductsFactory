"""Per-persona Ollama model routing — mirrors the Claude model map

Revision ID: 032
Revises: 031
Create Date: 2026-04-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("system_config",
        sa.Column("ollama_model_map", JSONB(), nullable=True))


def downgrade():
    op.drop_column("system_config", "ollama_model_map")
