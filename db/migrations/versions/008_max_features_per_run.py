"""add max_features_per_run to products

Revision ID: 008
Revises: 007
"""
from alembic import op
import sqlalchemy as sa

revision = '008'
down_revision = '007'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('products', sa.Column('max_features_per_run', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('products', 'max_features_per_run')
