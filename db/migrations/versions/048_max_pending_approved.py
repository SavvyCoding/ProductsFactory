"""expose max_pending_approved as a UI-configurable system_config knob

Revision ID: 048
Revises: 047
Create Date: 2026-06-22

The planner persona generates a fresh backlog when a product's Approved queue
drains to zero, self-throttled by `max_pending_approved` (orchestrator/cycle/
persona.py step 5). That value was read from system_config but had NO column —
so it always fell back to the MAX_PENDING_APPROVED env / built-in default (10),
with no way to tune it per-environment from the admin UI.

Adds the column so the Settings → Poller card can set it (hot-reloadable, no
restart). Nullable: NULL → env var → built-in default, matching the other
poller knobs' resolution in website.main._cfg.
"""
import sqlalchemy as sa
from alembic import op


revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("system_config", sa.Column("max_pending_approved", sa.Integer()))


def downgrade():
    op.drop_column("system_config", "max_pending_approved")
