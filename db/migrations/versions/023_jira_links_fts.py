"""add feature_links table and full-text search GIN index on features

feature_links — directed relationship edges between features:
               blocks, is_blocked_by, relates_to, duplicates
GIN index      — enables fast PostgreSQL full-text search on name + description

Revision ID: 023
Revises: 022
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feature_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("features.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("features.id", ondelete="CASCADE"), nullable=False),
        sa.Column("link_type", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", "target_id", "link_type", name="uq_feature_links"),
        sa.CheckConstraint(
            "link_type IN ('blocks', 'is_blocked_by', 'relates_to', 'duplicates')",
            name="ck_feature_links_type",
        ),
    )
    op.create_index("ix_feature_links_source", "feature_links", ["source_id"])
    op.create_index("ix_feature_links_target", "feature_links", ["target_id"])

    # Full-text search GIN index on features.name + features.description
    op.execute("""
        CREATE INDEX ix_features_fts ON features
        USING GIN (to_tsvector('english',
            coalesce(name, '') || ' ' || coalesce(description, '')
        ))
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_features_fts")
    op.drop_index("ix_feature_links_target", table_name="feature_links")
    op.drop_index("ix_feature_links_source", table_name="feature_links")
    op.drop_table("feature_links")
