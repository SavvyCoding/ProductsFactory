"""
First-time DB initialisation.
Run once on the Ubuntu VM after PostgreSQL is installed.

Usage:
    DATABASE_URL=postgresql://user:pass@localhost/productfactory python -m db.init_db

What it does:
  1. Verifies the DB connection
  2. Runs all Alembic migrations (alembic upgrade head)
  3. Prints the final table list to confirm success
"""

import os
import sys
import subprocess

import sqlalchemy as sa


def check_connection(url: str) -> bool:
    sync_url = (
        url
        .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        .replace("postgresql://", "postgresql+psycopg2://")
    )
    try:
        engine = sa.create_engine(sync_url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        engine.dispose()
        return True
    except Exception as e:
        print(f"[ERROR] DB connection failed: {e}", file=sys.stderr)
        return False


def run_migrations() -> bool:
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        capture_output=False,   # stream output directly so user can see progress
    )
    return result.returncode == 0


def list_tables(url: str):
    sync_url = (
        url
        .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        .replace("postgresql://", "postgresql+psycopg2://")
    )
    engine = sa.create_engine(sync_url)
    inspector = sa.inspect(engine)
    tables = inspector.get_table_names()
    engine.dispose()
    return tables


if __name__ == "__main__":
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("[ERROR] DATABASE_URL environment variable not set", file=sys.stderr)
        sys.exit(1)

    print(f"[1/3] Checking DB connection...")
    if not check_connection(db_url):
        sys.exit(1)
    print("      OK")

    print(f"[2/3] Running migrations (alembic upgrade head)...")
    if not run_migrations():
        print("[ERROR] Migration failed", file=sys.stderr)
        sys.exit(1)

    print(f"[3/3] Verifying tables...")
    tables = list_tables(db_url)
    expected = {"products", "features", "sessions", "alerts", "alembic_version"}
    missing = expected - set(tables)
    if missing:
        print(f"[ERROR] Missing tables: {missing}", file=sys.stderr)
        sys.exit(1)

    print(f"\n✓ ProductFactory DB ready. Tables: {sorted(tables)}")
