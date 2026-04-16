"""add feature_comments and feature_changelog tables

feature_comments — per-feature discussion thread (pm, agents, poller)
feature_changelog — field-level audit trail auto-populated on every PATCH

Revision ID: 021
Revises: 020
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feature_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feature_id", sa.Integer(), sa.ForeignKey("features.id", ondelete="CASCADE"), nullable=False),
        sa.Column("author", sa.Text(), nullable=False, server_default="pm"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_feature_comments_feature", "feature_comments", ["feature_id", "created_at"])

    op.create_table(
        "feature_changelog",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feature_id", sa.Integer(), sa.ForeignKey("features.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("old_value", sa.Text()),
        sa.Column("new_value", sa.Text()),
        sa.Column("changed_by", sa.Text(), nullable=False, server_default="poller"),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_feature_changelog_feature", "feature_changelog", ["feature_id", "changed_at"])


def downgrade():
    op.drop_index("ix_feature_changelog_feature", table_name="feature_changelog")
    op.drop_table("feature_changelog")
    op.drop_index("ix_feature_comments_feature", table_name="feature_comments")
    op.drop_table("feature_comments")
