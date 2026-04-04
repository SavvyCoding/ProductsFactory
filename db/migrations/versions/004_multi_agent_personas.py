"""Add multi-agent persona support: Designer, Coder, Reviewer.

New feature columns: skip_design, design_doc, design_doc_path, review_outcome, review_notes
New session column: persona
New feature statuses: Designing, Designed, Reviewing, Reviewed

Revision ID: 004
Revises: 003
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None

# Extended status list (must match models.py FEATURE_STATUSES)
NEW_STATUSES = (
    'Pending', 'Approved',
    'Designing', 'Designed',
    'Implementing', 'Implemented',
    'Reviewing', 'Reviewed',
    'Testing', 'Committed', 'Pushed',
    'Blocked', 'Rejected', 'Reverted',
)


def upgrade() -> None:
    # ── features: design pipeline columns ────────────────────────────────────
    op.add_column("features", sa.Column("skip_design",     sa.Boolean(),  nullable=False, server_default="false"))
    op.add_column("features", sa.Column("design_doc",      sa.Text(),     nullable=True))
    op.add_column("features", sa.Column("design_doc_path", sa.Text(),     nullable=True))
    op.add_column("features", sa.Column("review_outcome",  sa.String(32), nullable=True))
    op.add_column("features", sa.Column("review_notes",    sa.Text(),     nullable=True))

    # Drop the old check constraint and recreate with new statuses
    op.drop_constraint("ck_features_status", "features", type_="check")
    op.create_check_constraint(
        "ck_features_status",
        "features",
        f"status IN {NEW_STATUSES}",
    )

    # ── sessions: persona column ──────────────────────────────────────────────
    op.add_column("sessions", sa.Column("persona", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "persona")

    op.drop_constraint("ck_features_status", "features", type_="check")
    OLD_STATUSES = ('Pending', 'Approved', 'Implementing', 'Implemented',
                    'Testing', 'Committed', 'Pushed', 'Blocked', 'Rejected', 'Reverted')
    op.create_check_constraint(
        "ck_features_status",
        "features",
        f"status IN {OLD_STATUSES}",
    )

    op.drop_column("features", "review_notes")
    op.drop_column("features", "review_outcome")
    op.drop_column("features", "design_doc_path")
    op.drop_column("features", "design_doc")
    op.drop_column("features", "skip_design")
