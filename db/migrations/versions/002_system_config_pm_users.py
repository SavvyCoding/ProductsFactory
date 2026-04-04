"""Add system_config, pm_users tables; extend products.status check constraint

Revision ID: 002
Revises: 001
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Extend products.status to include 'greenfield_pending' ────────────────
    op.drop_constraint("ck_products_status", "products", type_="check")
    op.create_check_constraint(
        "ck_products_status",
        "products",
        "status IN ('registered','discovering','discovered','ready','paused','error','greenfield_pending')",
    )

    # ── system_config (single-row global config, id always = 1) ──────────────
    op.create_table(
        "system_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("products_root_dir", sa.Text(), nullable=True),
        sa.Column("github_org", sa.Text(), nullable=True),
        sa.Column("github_pat", sa.Text(), nullable=True),
        sa.Column("github_ssh_key_name", sa.Text(), nullable=False, server_default="productfactory-deploy"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── pm_users ──────────────────────────────────────────────────────────────
    op.create_table(
        "pm_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("username", name="uq_pm_users_username"),
    )


def downgrade() -> None:
    op.drop_table("pm_users")
    op.drop_table("system_config")

    op.drop_constraint("ck_products_status", "products", type_="check")
    op.create_check_constraint(
        "ck_products_status",
        "products",
        "status IN ('registered','discovering','discovered','ready','paused','error')",
    )
