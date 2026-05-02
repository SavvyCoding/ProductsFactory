#!/usr/bin/env bash
# 2-hourly rolling ProductFactory database backup.
#
# Filename pattern: productfactory_2h_HH.dump  (HH = 00, 02, 04, ..., 22)
# Same file overwrites every 24 hours — 12 backups total, rolling.
# Complements scripts/backup_db.sh (daily, 7-day retention) for shorter RPO.
#
# Usage (run manually or via Windows Task Scheduler / cron every 2 hours):
#   bash scripts/backup_db_2h.sh
#
# Backup location: ./backups/db/  (relative to repo root)
# Reads DATABASE_URL from .env if present, otherwise from the environment.

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

DB_URL="${DATABASE_URL}"
DB_URL="${DB_URL//postgresql+asyncpg:\/\//postgresql://}"
DB_URL="${DB_URL//postgresql+psycopg2:\/\//postgresql://}"
DB_URL="${DB_URL//postgres:\/\//postgresql://}"

# Hour of day → slot id. Snap to the nearest even hour so the file name is
# stable even if the scheduler fires a few seconds early/late.
HOUR=$(date +%H)
SLOT=$(printf "%02d" $(( 10#$HOUR / 2 * 2 )))
DUMP_FILE="$BACKUP_DIR/productfactory_2h_${SLOT}.dump"

echo "Starting 2-hourly backup (slot ${SLOT}h): $DUMP_FILE"

# Atomic write via temp file rename — readers never see a half-written dump.
TMP_FILE="${DUMP_FILE}.partial"

if command -v pg_dump >/dev/null 2>&1; then
    pg_dump --format=custom --no-password "$DB_URL" -f "$TMP_FILE"
    mv "$TMP_FILE" "$DUMP_FILE"
else
    PG_CONTAINER="${PG_CONTAINER:-ProductsFactoryDB}"
    if ! docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
        echo "ERROR: pg_dump not on PATH and container '${PG_CONTAINER}' not running." >&2
        exit 1
    fi
    DUMP_NAME=$(basename "$TMP_FILE")
    DUMP_FILE_WIN=$(cygpath -w "$TMP_FILE" 2>/dev/null || echo "$TMP_FILE")

    MSYS_NO_PATHCONV=1 docker exec "$PG_CONTAINER" pg_dump --format=custom --no-password \
        -U "${POSTGRES_USER:-productfactory}" \
        -d "${POSTGRES_DB:-productfactory}" \
        -f "/tmp/${DUMP_NAME}"
    MSYS_NO_PATHCONV=1 docker cp "${PG_CONTAINER}:/tmp/${DUMP_NAME}" "$DUMP_FILE_WIN"
    MSYS_NO_PATHCONV=1 docker exec "$PG_CONTAINER" rm -f "/tmp/${DUMP_NAME}"
    mv "$TMP_FILE" "$DUMP_FILE"
fi

SIZE=$(du -sh "$DUMP_FILE" | cut -f1)
echo "Backup complete: $DUMP_FILE ($SIZE)"
