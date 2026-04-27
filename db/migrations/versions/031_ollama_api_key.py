"""Add ollama_api_key to system_config for Ollama Cloud auth

Revision ID: 031
Revises: 030
Create Date: 2026-04-26
"""
from alembic import op
import sqlalchemy as sa


revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("system_config",
        sa.Column("ollama_api_key", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("system_config", "ollama_api_key")
