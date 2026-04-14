"""Add feature_reviews table — full review history per feature.

Revision ID: 006
Revises: 005
Create Date: 2026-04-04
"""

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_reviews",
        sa.Column("id",             sa.Integer(),     nullable=False, primary_key=True),
        sa.Column("feature_id",     sa.Integer(),     nullable=False),
        sa.Column("review_outcome", sa.String(32),    nullable=False),
        sa.Column("review_notes",   sa.Text(),        nullable=True),
        sa.Column("session_uid",    sa.Text(),        nullable=True),
        sa.Column("created_at",     sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["feature_id"], ["features.id"], ondelete="CASCADE"),
        sa.CheckConstraint("review_outcome IN ('approved', 'changes_requested')", name="ck_feature_reviews_outcome"),
    )
    op.create_index("ix_feature_reviews_feature_id", "feature_reviews", ["feature_id"])


def downgrade() -> None:
    op.drop_index("ix_feature_reviews_feature_id", "feature_reviews")
    op.drop_table("feature_reviews")
