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

# pg_dump runs from the Docker host; the DB is inside the compose network.
# If running on the Windows host where the DB is exposed on localhost:5432,
# this works directly. Inside Docker, use the service name instead.
pg_dump --format=custom --no-password "$DB_URL" -f "$DUMP_FILE"

echo "Backup complete: $DUMP_FILE ($(du -sh "$DUMP_FILE" | cut -f1))"

# Retention: delete dumps older than 7 days
find "$BACKUP_DIR" -name "productfactory_*.dump" -mtime +7 -delete
echo "Old backups pruned (retention: 7 days)"
