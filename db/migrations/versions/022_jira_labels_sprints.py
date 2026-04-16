"""add labels, feature_labels, sprints tables; add sprint_id/story_points/due_date to features

labels        — user-defined tags per product (e.g. security, mvp, tech-debt)
feature_labels — many-to-many join table
sprints        — time-boxed batches per product
features       — adds sprint_id (FK), story_points (int), due_date (date)

Revision ID: 022
Revises: 021
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "labels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), nullable=False, server_default="'#6366f1'"),
        sa.UniqueConstraint("product_id", "name", name="uq_labels_product_name"),
    )
    op.create_index("ix_labels_product", "labels", ["product_id"])

    op.create_table(
        "feature_labels",
        sa.Column("feature_id", sa.Integer(), sa.ForeignKey("features.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label_id", sa.Integer(), sa.ForeignKey("labels.id", ondelete="CASCADE"), nullable=False),
        sa.PrimaryKeyConstraint("feature_id", "label_id"),
    )

    op.create_table(
        "sprints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text()),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.Column("status", sa.Text(), nullable=False, server_default="'active'"),
        sa.CheckConstraint("status IN ('planned', 'active', 'completed')", name="ck_sprints_status"),
    )
    op.create_index("ix_sprints_product", "sprints", ["product_id"])

    op.add_column("features", sa.Column("sprint_id", sa.Integer(), sa.ForeignKey("sprints.id", ondelete="SET NULL"), nullable=True))
    op.add_column("features", sa.Column("story_points", sa.Integer(), nullable=True))
    op.add_column("features", sa.Column("due_date", sa.Date(), nullable=True))


def downgrade():
    op.drop_column("features", "due_date")
    op.drop_column("features", "story_points")
    op.drop_column("features", "sprint_id")
    op.drop_index("ix_sprints_product", table_name="sprints")
    op.drop_table("sprints")
    op.drop_table("feature_labels")
    op.drop_index("ix_labels_product", table_name="labels")
    op.drop_table("labels")
