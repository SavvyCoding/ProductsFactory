"""Sprint-level branch + PR + per-sprint feature cap

Revision ID: 034
Revises: 033
Create Date: 2026-04-28

Adds:
- sprints.branch_name / pr_number / pr_url
  Populated when product.config.sprint_pr_mode is true and a sprint becomes active.
  All three are nullable for backward compatibility with existing per-feature flow.
- system_config.max_features_per_sprint
  Default 5; enforced by the website on any feature -> sprint assignment.
"""
from alembic import op
import sqlalchemy as sa


revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sprints", sa.Column("branch_name", sa.String(255), nullable=True))
    op.add_column("sprints", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("sprints", sa.Column("pr_url", sa.String(500), nullable=True))
    op.add_column(
        "system_config",
        sa.Column("max_features_per_sprint", sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column("system_config", "max_features_per_sprint")
    op.drop_column("sprints", "pr_url")
    op.drop_column("sprints", "pr_number")
    op.drop_column("sprints", "branch_name")
