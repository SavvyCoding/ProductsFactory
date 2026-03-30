"""Initial schema — products, features, sessions, alerts

Revision ID: 001
Revises:
Create Date: 2026-03-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── products ──────────────────────────────────────────────────────────────
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("working_dir", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("github_repo", sa.Text(), nullable=True),
        sa.Column("tech_stack", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("type", sa.Text(), nullable=False, server_default="greenfield"),
        sa.Column("status", sa.Text(), nullable=False, server_default="registered"),
        sa.Column("analysis_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("working_dir", name="uq_products_working_dir"),
        sa.CheckConstraint(
            "type IN ('greenfield', 'brownfield')",
            name="ck_products_type",
        ),
        sa.CheckConstraint(
            "status IN ('registered', 'discovering', 'discovered', 'ready', 'paused', 'error')",
            name="ck_products_status",
        ),
        sa.CheckConstraint(
            "analysis_status IN ('pending', 'running', 'done')",
            name="ck_products_analysis_status",
        ),
    )

    # ── features ─────────────────────────────────────────────────────────────
    op.create_table(
        "features",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="Pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("depends_on", sa.Integer(), sa.ForeignKey("features.id"), nullable=True),
        sa.Column("fix_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.Text(), nullable=False, server_default="pm"),
        sa.Column("branch_name", sa.Text(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('Pending','Approved','Implementing','Implemented',"
            "'Testing','Committed','Pushed','Blocked','Rejected','Reverted')",
            name="ck_features_status",
        ),
        sa.CheckConstraint("priority BETWEEN 1 AND 100", name="ck_features_priority"),
        sa.CheckConstraint("fix_attempts >= 0", name="ck_features_fix_attempts"),
        sa.CheckConstraint("source IN ('pm', 'ai')", name="ck_features_source"),
    )

    # ── sessions ─────────────────────────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_uid", sa.Text(), nullable=False),
        sa.Column("container_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("features_attempted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("features_pushed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("session_uid", name="uq_sessions_session_uid"),
    )

    # ── alerts ───────────────────────────────────────────────────────────────
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "level IN ('info', 'warning', 'error', 'critical')",
            name="ck_alerts_level",
        ),
        sa.CheckConstraint("retry_count >= 0", name="ck_alerts_retry_count"),
    )

    # ── indexes ───────────────────────────────────────────────────────────────
    op.create_index(
        "idx_features_product_status",
        "features",
        ["product_id", "status"],
    )
    op.create_index(
        "idx_products_last_run",
        "products",
        ["last_run_at"],
        postgresql_where=sa.text("status = 'ready'"),
    )
    op.create_index(
        "idx_alerts_undelivered",
        "alerts",
        ["created_at"],
        postgresql_where=sa.text("delivered = false"),
    )

    # ── updated_at triggers ───────────────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_products_updated
            BEFORE UPDATE ON products
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)
    op.execute("""
        CREATE TRIGGER trg_features_updated
            BEFORE UPDATE ON features
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_features_updated ON features;")
    op.execute("DROP TRIGGER IF EXISTS trg_products_updated ON products;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")

    op.drop_index("idx_alerts_undelivered", table_name="alerts")
    op.drop_index("idx_products_last_run", table_name="products")
    op.drop_index("idx_features_product_status", table_name="features")

    op.drop_table("alerts")
    op.drop_table("sessions")
    op.drop_table("features")
    op.drop_table("products")
