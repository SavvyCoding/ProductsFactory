#!/usr/bin/env bash
# Daily ProductFactory database backup.
#
# Usage (run manually or via Task Scheduler / cron):
#   bash scripts/backup_db.sh
#
# Backup location: ./backups/db/  (relative to repo root)
# Retention: keeps the last 7 daily dumps, deletes older ones automatically.
#
# Reads DATABASE_URL from .env if present, otherwise from the environment.
# DATABASE_URL format: postgresql://user:pass@host:port/dbname

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/backups/db"
mkdir -p "$BACKUP_DIR"

# Load .env
if [[ -f "$REPO_ROOT/.env" ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +o allexport
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "ERROR: DATABASE_URL is not set. Set it in .env or the environment." >&2
    exit 1
fi

# Parse DATABASE_URL into pg_dump flags
# Expected format: postgresql://user:pass@host:port/dbname
#   or:            postgres://user:pass@host:port/dbname
DB_URL="${DATABASE_URL}"

# Strip driver prefix variants (asyncpg, psycopg2 etc.)
DB_URL="${DB_URL//postgresql+asyncpg:\/\//postgresql://}"
DB_URL="${DB_URL//postgresql+psycopg2:\/\//postgresql://}"
DB_URL="${DB_URL//postgres:\/\//postgresql://}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DUMP_FILE="$BACKUP_DIR/productfactory_${TIMESTAMP}.dump"

echo "Starting backup: $DUMP_FILE"

if command -v pg_dump >/dev/null 2>&1; then
    # Host has postgres-client installed — dump directly.
    pg_dump --format=custom --no-password "$DB_URL" -f "$DUMP_FILE"
else
    # Fallback: pg_dump inside the postgres container, then docker cp the
    # result to the host. Works out-of-the-box on Windows hosts where
    # nobody installs postgres-client but Docker Desktop is always there.
    PG_CONTAINER="${PG_CONTAINER:-ProductsFactoryDB}"
    if ! docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
        echo "ERROR: pg_dump not on PATH and container '${PG_CONTAINER}' not running." >&2
        exit 1
    fi
    DUMP_NAME=$(basename "$DUMP_FILE")
    docker exec "$PG_CONTAINER" pg_dump --format=custom --no-password \
        -U "${POSTGRES_USER:-productfactory}" \
        -d "${POSTGRES_DB:-productfactory}" \
        -f "/tmp/${DUMP_NAME}"
    docker cp "${PG_CONTAINER}:/tmp/${DUMP_NAME}" "$DUMP_FILE"
    docker exec "$PG_CONTAINER" rm -f "/tmp/${DUMP_NAME}"
fi

echo "Backup complete: $DUMP_FILE ($(du -sh "$DUMP_FILE" | cut -f1))"

# Retention: delete dumps older than 7 days
find "$BACKUP_DIR" -name "productfactory_*.dump" -mtime +7 -delete
echo "Old backups pruned (retention: 7 days)"
