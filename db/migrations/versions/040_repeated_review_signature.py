"""Add last_changes_signature + repeated_changes_count to features

Revision ID: 040
Revises: 039
Create Date: 2026-05-09

Powers `supervisor.detect_repeated_review_feedback`: when a reviewer
files `changes_requested` with effectively the same feedback as the
prior cycle, the supervisor auto-blocks the feature instead of waiting
for the fix_attempts=5 cap to fire (~3-4 hours of wasted compute on
Ollama Cloud). The signature is a hash of the reviewer's structured
comment bullets — so semantically equivalent rephrasings still match.

`last_changes_signature` is the most recent fingerprint; null means no
prior changes_requested cycle (first review or all approvals so far).
`repeated_changes_count` is consecutive cycles with the same signature;
resets to 0 when the signature changes (i.e. the coder addressed
something) or when the feature is approved.
"""
from alembic import op
import sqlalchemy as sa


revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "features",
        sa.Column("last_changes_signature", sa.Text(), nullable=True),
    )
    op.add_column(
        "features",
        sa.Column(
            "repeated_changes_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade():
    op.drop_column("features", "repeated_changes_count")
    op.drop_column("features", "last_changes_signature")
