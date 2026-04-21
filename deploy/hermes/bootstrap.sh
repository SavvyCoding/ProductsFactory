#!/bin/bash
# Hermes gateway bootstrap — runs inside the container on first startup.
# Registers the 60s orchestration cron and then launches the gateway daemon.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/home/hermes/.hermes}"
mkdir -p "$HERMES_HOME/cron"

# Staging dir for temp Claude creds + GH token files. This is bind-mounted
# from the host at the same path, so `docker run -v /hermes-staging/xxx:...`
# resolves correctly both for Hermes (local write) and the Docker daemon
# (host read).
STAGING="${HERMES_STAGING_CONTAINER:-/hermes-staging}"
mkdir -p "$STAGING" || true

# Only register cron jobs if none exist yet (idempotent across container restarts).
if [ ! -s "$HERMES_HOME/cron/jobs.json" ] || ! grep -q "orchestrate" "$HERMES_HOME/cron/jobs.json" 2>/dev/null; then
    echo "[bootstrap] Registering orchestration cron job..."
    hermes cron create "every 60s" "Run the orchestrate skill to advance ProductFactory sprints" || {
        echo "[bootstrap] hermes cron create failed — check that the gateway can accept CLI commands."
        exit 1
    }
fi

# Attempt initial lock acquisition — if another Hermes already holds it, exit
# cleanly (docker-compose restart policy will back off and retry).
echo "[bootstrap] Acquiring poller lock..."
LOCK_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$PM_API_URL/api/poller/lock" \
    -H "Content-Type: application/json" \
    -d "{\"pid\": $$, \"host\": \"hermes-$(hostname)\"}" || echo "000")

if [ "$LOCK_STATUS" = "409" ]; then
    echo "[bootstrap] Another orchestrator holds the lock (409). Exiting."
    exit 0
elif [ "$LOCK_STATUS" != "200" ] && [ "$LOCK_STATUS" != "201" ]; then
    echo "[bootstrap] Unexpected lock response: $LOCK_STATUS — continuing anyway (PM API may still be starting)."
fi

# Hand off to the gateway daemon. This blocks; Docker restart policy handles crashes.
exec hermes gateway run
