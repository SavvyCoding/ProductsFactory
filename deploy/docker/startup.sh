#!/bin/sh
# startup.sh — runs inside the pm-api container before uvicorn.
# Self-heals stale alembic_version: if the version row exists but the
# actual data tables are gone (e.g. after a volume wipe), we clear the
# stale row so alembic upgrade head re-creates everything from scratch.

set -e

python - <<'PYEOF'
import os
import sqlalchemy as sa

url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
engine = sa.create_engine(url)
with engine.connect() as conn:
    inspector = sa.inspect(engine)
    has_tables = inspector.has_table("products")
    has_ver    = inspector.has_table("alembic_version")
    if has_ver and not has_tables:
        conn.execute(sa.text("DELETE FROM alembic_version"))
        conn.commit()
        print("[startup] Cleared stale alembic_version — will re-run all migrations")
    elif not has_ver:
        print("[startup] Fresh DB — alembic will create all tables")
    else:
        print("[startup] DB looks healthy — running incremental migrations if any")
engine.dispose()
PYEOF

alembic upgrade head
exec uvicorn website.main:app --host 0.0.0.0 --port 8080 --workers 2
