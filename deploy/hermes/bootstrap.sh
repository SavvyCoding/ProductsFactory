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
# Hermes's duration parser takes m/h/d only — use `1m` for 60-second cycles.
if [ ! -s "$HERMES_HOME/cron/jobs.json" ] || ! grep -q "orchestrate" "$HERMES_HOME/cron/jobs.json" 2>/dev/null; then
    echo "[bootstrap] Registering orchestration cron job..."
    if hermes cron create "every 1m" "Run the ProductFactory orchestration cycle" \
          --skill orchestrate --name "Orchestrate"; then
        echo "[bootstrap] Cron registered (skill=orchestrate, toolset=productfactory)."
    else
        echo "[bootstrap] hermes cron create failed — gateway may still be starting, retrying next cycle."
        # Not fatal — the gateway will start regardless; we'll register on next restart.
    fi
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

# Launch the web dashboard in the background (port 9119).
# --insecure is required to bind to 0.0.0.0 so the host can reach it.
# No API keys are actually stored in this deployment (local Ollama only), so
# the "DANGEROUS" warning from the flag name is benign in our case.
echo "[bootstrap] Starting Hermes dashboard on :9119..."
hermes dashboard --host 0.0.0.0 --port 9119 --no-open --insecure \
    > /home/hermes/.hermes/logs/dashboard.log 2>&1 &
DASHBOARD_PID=$!
echo "[bootstrap] Dashboard PID=$DASHBOARD_PID → http://localhost:9119"

# Forward SIGTERM/SIGINT to the dashboard so docker stop is clean.
trap "kill $DASHBOARD_PID 2>/dev/null || true" TERM INT

# Hand off to the gateway daemon. This blocks; Docker restart policy handles crashes.
exec hermes gateway
