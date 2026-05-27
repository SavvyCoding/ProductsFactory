"""Phases→features flat model: drop sprints, add feature.parent_id/merge_notes/phase_id.

The 1-PR session-PR model already made sprints redundant as a shipping unit.
This migration completes the cleanup: drop the entire `sprints` table, give
`features` direct parent-child relations + phase membership + per-feature
merge notes. Phases become pure UI groupings (no completion gates, no DoD).

Three incidents in the two weeks before this migration would not have
happened under the new model:
  - MyTracking 2026-05-22 (47 features auto-Blocked via sprint-PR/cap
    interactions during an Ollama quota outage)
  - MyDocusign 2026-05-21 (planner eligibility filter bug around the
    kind='blocked' holdpen sprint)
  - MyContract 2026-05-26 (13 features auto-Blocked at sizing-gate ×
    sprint-cap intersection)

Data wipe of all 5 products preceded this migration (no migration of
runtime data is required) — see scripts/wipe_all_products.sql.
system_config + pm_users were preserved.

Revision ID: 043_phases_features_flat
Revises: 042_github_app_auth
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa


revision      = "043"
down_revision = "042"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # ── features: drop sprint_id, add parent_id + merge_notes + phase_id ──
    op.drop_index("ix_features_sprint_id", table_name="features")
    op.drop_constraint("features_sprint_id_fkey", "features", type_="foreignkey")
    op.drop_column("features", "sprint_id")

    op.add_column("features", sa.Column("parent_id",   sa.Integer(), nullable=True))
    op.add_column("features", sa.Column("merge_notes", sa.Text(),    nullable=True))
    op.add_column("features", sa.Column("phase_id",    sa.Integer(), nullable=True))

    op.create_foreign_key(
        "features_parent_id_fkey", "features", "features",
        ["parent_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "features_phase_id_fkey", "features", "phases",
        ["phase_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_features_parent_id", "features", ["parent_id"])
    op.create_index("ix_features_phase_id",  "features", ["phase_id"])

    # ── phases: drop the `status` column (and its check constraint) ──
    # Phases are now pure UI groupings. No active/planned/completed lifecycle.
    op.drop_constraint("ck_phases_status", "phases", type_="check")
    op.drop_column("phases", "status")

    # ── sprints: drop the entire table ──
    # Cascade-drop catches sprints.phase_id FK + any sprint-referencing
    # constraint from related tables (e.g. a hypothetical sprint_signoffs).
    # `features.sprint_id` was already dropped above.
    op.execute("DROP TABLE IF EXISTS sprints CASCADE")


def downgrade() -> None:
    # Recreate sprints table with the legacy schema. Best-effort — the
    # rich state that lived in dod_status JSONB and the kind='blocked'
    # holdpen sprint cannot be reconstructed from features alone, so a
    # downgrade leaves an empty sprints table.
    op.create_table(
        "sprints",
        sa.Column("id",             sa.Integer(), nullable=False),
        sa.Column("product_id",     sa.Integer(), nullable=False),
        sa.Column("phase_id",       sa.Integer(), nullable=True),
        sa.Column("name",           sa.Text(),    nullable=False),
        sa.Column("goal",           sa.Text(),    nullable=True),
        sa.Column("status",         sa.Text(),    nullable=False,
                  server_default=sa.text("'planned'")),
        sa.Column("kind",           sa.Text(),    nullable=False,
                  server_default=sa.text("'normal'")),
        sa.Column("dod_status",     sa.dialects.postgresql.JSONB(),
                  nullable=True),
        sa.Column("release_notes",  sa.Text(), nullable=True),
        sa.Column("retro_doc_path", sa.Text(), nullable=True),
        sa.Column("completed_at",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",     sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["phase_id"], ["phases.id"], ondelete="SET NULL",
        ),
    )

    # phases.status returns
    op.add_column(
        "phases",
        sa.Column("status", sa.Text(), nullable=False,
                  server_default=sa.text("'planned'")),
    )
    op.create_check_constraint(
        "ck_phases_status", "phases",
        "status IN ('planned', 'active', 'completed')",
    )

    # features: drop new columns, restore sprint_id
    op.drop_index("ix_features_phase_id",  table_name="features")
    op.drop_index("ix_features_parent_id", table_name="features")
    op.drop_constraint("features_phase_id_fkey",  "features", type_="foreignkey")
    op.drop_constraint("features_parent_id_fkey", "features", type_="foreignkey")
    op.drop_column("features", "phase_id")
    op.drop_column("features", "merge_notes")
    op.drop_column("features", "parent_id")

    op.add_column("features", sa.Column("sprint_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "features_sprint_id_fkey", "features", "sprints",
        ["sprint_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_features_sprint_id", "features", ["sprint_id"])
