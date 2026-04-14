"""Add 'Deferred' to feature status check constraint.

Revision ID: 007
Revises: 006
Create Date: 2026-04-04
"""

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None

_OLD_STATUSES = (
    'Pending', 'Approved',
    'Designing', 'Designed',
    'Implementing', 'Implemented',
    'Reviewing', 'Reviewed',
    'Testing', 'Committed', 'Pushed',
    'Blocked', 'Rejected', 'Reverted',
)
_NEW_STATUSES = _OLD_STATUSES + ('Deferred',)


def upgrade() -> None:
    op.drop_constraint("ck_features_status", "features", type_="check")
    op.create_check_constraint(
        "ck_features_status",
        "features",
        f"status IN {_NEW_STATUSES}",
    )


def downgrade() -> None:
    op.drop_constraint("ck_features_status", "features", type_="check")
    op.create_check_constraint(
        "ck_features_status",
        "features",
        f"status IN {_OLD_STATUSES}",
    )
