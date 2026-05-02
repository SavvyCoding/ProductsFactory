#!/bin/sh
# startup.sh — runs inside the pm-api container before uvicorn.
#
# Pre-#18 this script auto-cleared the alembic_version row whenever
# `inspector.has_table("products")` returned False — on the assumption that
# the volume had been wiped but a stale alembic_version somehow survived.
# That heuristic FALSE-POSITIVED on 2026-05-02 and DROPPED the entire schema:
# all 1,698 sessions, every product, every feature gone in one container
# restart. The "benefit" of the self-heal was theoretical (a wiped volume
# loses alembic_version anyway since they're in the same volume); the
# downside was catastrophic data loss.
#
# New policy (#18): NEVER auto-clear alembic_version. If we detect an
# inconsistent state — alembic_version row present but core tables missing —
# log loudly and exit non-zero. Operator decides whether to:
#   - drop+recreate the database (intentional fresh start), OR
#   - investigate why the schema vanished, OR
#   - manually `DELETE FROM alembic_version` after a real volume wipe.
#
# The fail-closed default trades convenience for safety: a misfire now
# means uvicorn doesn't start (loud, recoverable) instead of every table
# being silently DROPped + CREATEd empty (silent, destructive).

set -e

python - <<'PYEOF'
import os
import sys
import sqlalchemy as sa

url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
engine = sa.create_engine(url)

# Sample a handful of core tables — not just `products` — so a single
# spurious has_table miss doesn't drive the inconsistency check. If even
# one core table is present, the schema is alive and alembic should be
# allowed to do incremental upgrades.
CORE_TABLES = ("products", "features", "sprints", "sessions", "system_config")

with engine.connect() as conn:
    inspector = sa.inspect(engine)
    has_ver = inspector.has_table("alembic_version")
    present_core = [t for t in CORE_TABLES if inspector.has_table(t)]

    if not has_ver and not present_core:
        print("[startup] Fresh DB (no alembic_version, no core tables) — alembic will create everything")
    elif has_ver and present_core:
        print(f"[startup] DB looks healthy — alembic_version present, {len(present_core)}/{len(CORE_TABLES)} core tables present")
    elif not has_ver and present_core:
        print(f"[startup] WARNING: alembic_version missing but {len(present_core)} core tables present — possible legacy/pre-alembic schema. Letting alembic upgrade head try to stamp.")
    else:
        # has_ver=True AND present_core=[]: the dangerous case — pre-#18 this
        # silently destroyed the schema. Now we refuse to start until the
        # operator looks at it.
        print("[startup] FATAL: alembic_version is populated but ALL core tables are missing.", file=sys.stderr)
        print("[startup] This is the inconsistent state the old auto-clear used to wipe through.", file=sys.stderr)
        print(f"[startup] Checked tables: {CORE_TABLES}", file=sys.stderr)
        print("[startup] Refusing to start. Diagnose by inspecting the database directly:", file=sys.stderr)
        print("[startup]   docker exec ProductsFactoryDB psql -U productfactory -d productfactory -c '\\dt'", file=sys.stderr)
        print("[startup] If a fresh start is intended, manually run: DELETE FROM alembic_version;", file=sys.stderr)
        print("[startup] then restart this container.", file=sys.stderr)
        sys.exit(2)

engine.dispose()
PYEOF

alembic upgrade head
exec uvicorn website.main:app --host 0.0.0.0 --port 8080 --workers 2
