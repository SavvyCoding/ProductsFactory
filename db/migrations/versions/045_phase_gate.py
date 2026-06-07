"""phase gate columns: gate_state + report (human-in-loop per-phase checkpoint)

Revision ID: 045
Revises: 044
Create Date: 2026-06-07

Adds an opt-in human-in-the-loop checkpoint at phase boundaries. Gated per
product by `product.config.human_gate_phases` (default false → fully
autonomous, unchanged from migration 043's flat phases→features model).

Two new columns on `phases`:
  - `gate_state` — lifecycle latch: 'open' → 'awaiting_review' → 'approved'.
    Default 'open'. A per-cycle detector (`detect_completed_phases`) keeps
    this in sync with feature reality; only a human PATCHes it to 'approved'
    (one-way latch that unlocks the next phase for the dispatcher).
  - `report` — JSONB phase summary written when the phase settles
    (code-quality / challenges / blockers / forward-dependency warnings /
    recommendations). Rendered in the dashboard for the gate decision.

This is deliberately NOT a resurrection of the migration-043 sprints/DoD
machinery — no completion timing, no sign-off endpoints, no per-phase cap.
It is a lightweight state flag + a denormalized report blob.

Downgrade drops both columns. Any stored gate_state/report is lost — accepted
because the gate is opt-in and off by default.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "phases",
        sa.Column(
            "gate_state",
            sa.Text(),
            nullable=False,
            server_default="open",
        ),
    )
    op.add_column("phases", sa.Column("report", JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_phases_gate_state",
        "phases",
        "gate_state IN ('open', 'awaiting_review', 'approved')",
    )


def downgrade():
    op.drop_constraint("ck_phases_gate_state", "phases", type_="check")
    op.drop_column("phases", "report")
    op.drop_column("phases", "gate_state")
