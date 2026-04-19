"""Phase 2 H8/M6: add ondelete=SET NULL on Feature.depends_on + missing FK indexes

- features.depends_on FK → ondelete SET NULL (so deleting a blocker doesn't leave
  a dangling reference on dependents)
- indexes on FK columns that had none: features.depends_on, features.sprint_id

Other FK indexes already exist from earlier migrations:
  022 → ix_labels_product, ix_sprints_product
  024 → ix_phases_product,  ix_sprints_phase_id

Revision ID: 027
Revises: 026
Create Date: 2026-04-18
"""
from alembic import op


revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade():
    # ── FK cascade fix on Feature.depends_on ─────────────────────────────────
    # Previously had no ondelete rule → deleting a feature left its dependents
    # with a dangling reference. Use SET NULL so the dependent feature survives
    # as "no longer blocked by anything" instead of silently carrying a bad FK.
    #
    # Postgres doesn't allow altering an FK's ondelete in place — drop + recreate.
    op.execute(
        "ALTER TABLE features DROP CONSTRAINT IF EXISTS features_depends_on_fkey"
    )
    op.create_foreign_key(
        "features_depends_on_fkey",
        "features", "features",
        ["depends_on"], ["id"],
        ondelete="SET NULL",
    )

    # ── Missing FK indexes ────────────────────────────────────────────────────
    # Postgres does NOT auto-index FK columns. Without these, JOINs and lookups
    # on depends_on / sprint_id trigger full table scans on `features`.
    op.create_index("ix_features_depends_on", "features", ["depends_on"])
    op.create_index("ix_features_sprint_id",  "features", ["sprint_id"])


def downgrade():
    op.drop_index("ix_features_sprint_id",  table_name="features")
    op.drop_index("ix_features_depends_on", table_name="features")

    op.drop_constraint("features_depends_on_fkey", "features", type_="foreignkey")
    op.create_foreign_key(
        "features_depends_on_fkey",
        "features", "features",
        ["depends_on"], ["id"],
    )
