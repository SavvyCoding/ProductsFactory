"""add GitHub App auth columns to system_config

Revision ID: 042
Revises: 041
Create Date: 2026-05-14

Migrates git authentication from per-product SSH deploy keys to a single
GitHub App with per-installation tokens. New columns hold the three values
needed to mint an installation token:

    github_app_id              — the App's numeric ID (from App settings page)
    github_app_private_key     — the PEM contents, used to sign the JWT
    github_app_installation_id — the install on the dedicated org

`github_org` already existed; it now holds the dedicated org slug
(e.g. 'ProductFactory-Agentic') used as the target for repo creation via
`POST /orgs/{org}/repos`.

`github_pat` is intentionally NOT dropped here. It stays as a deprecation
cushion for one release; once the App-token path is verified end-to-end
across every product, a follow-up migration removes the column.

Populate after running:
    UPDATE system_config
       SET github_app_id              = 3708112,
           github_app_installation_id = 132195390,
           github_org                 = 'ProductFactory-Agentic',
           github_app_private_key     = '<paste PEM contents>'
     WHERE id = 1;
"""
from alembic import op
import sqlalchemy as sa


revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "system_config",
        sa.Column("github_app_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "system_config",
        sa.Column("github_app_private_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "system_config",
        sa.Column("github_app_installation_id", sa.BigInteger(), nullable=True),
    )


def downgrade():
    op.drop_column("system_config", "github_app_installation_id")
    op.drop_column("system_config", "github_app_private_key")
    op.drop_column("system_config", "github_app_id")
