"""Add ui_template column to products

Records the UI/UX preset picked in the New Product wizard (modern_saas,
material_3, apple_hig, bootstrap, brutalist, minimalist, agent_choose).
The coder agent reads this to pick matching CSS/components when scaffolding.
NULL for non-web products and brownfield imports.

Revision ID: 033
Revises: 032
Create Date: 2026-04-27
"""
from alembic import op
import sqlalchemy as sa


revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("ui_template", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("products", "ui_template")
