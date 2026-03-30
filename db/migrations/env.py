# Alembic environment
# Run migrations:      alembic upgrade head
# Create new:          alembic revision --autogenerate -m "description"
# Rollback one step:   alembic downgrade -1

import os
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# Import models so Alembic's autogenerate can see the full schema.
# All four models must be imported even if unused here.
from website.models import Base  # noqa: F401 — registers Product, Feature, Session, Alert

config = context.config
fileConfig(config.config_file_name)

# Alembic uses a SYNC engine (psycopg2). The website runtime uses asyncpg.
# Both read DATABASE_URL from the environment; driver prefix is swapped here.
_raw_url = os.environ["DATABASE_URL"]
_sync_url = (
    _raw_url
    .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    .replace("postgresql://", "postgresql+psycopg2://")
)
config.set_main_option("sqlalchemy.url", _sync_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run without a live DB connection (generates SQL to stdout)."""
    context.configure(
        url=_sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,       # detect column type changes
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
