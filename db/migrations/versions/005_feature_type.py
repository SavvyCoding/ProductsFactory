"""Add feature_type column to features (feature | bug | chore).

Revision ID: 005
Revises: 004
Create Date: 2026-04-04
"""

from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "features",
        sa.Column(
            "feature_type",
            sa.Text(),
            nullable=False,
            server_default="feature",
        ),
    )
    op.create_check_constraint(
        "ck_features_type",
        "features",
        "feature_type IN ('feature', 'bug', 'chore')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_features_type", "features", type_="check")
    op.drop_column("features", "feature_type")
