"""add sprints.release_notes column

Stores AI-generated release notes after a sprint is completed.

Revision ID: 025
Revises: 024
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sprints", sa.Column("release_notes", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("sprints", "release_notes")
