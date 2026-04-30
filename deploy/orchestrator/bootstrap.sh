#!/bin/bash
# ProductFactory orchestrator bootstrap — runs inside the container on startup.
# Acquires the poller lock, then runs the orchestration loop.

set -euo pipefail

LOGDIR=/app/logs
mkdir -p "$LOGDIR"

# Staging dir for temp Claude creds + GH token files. Bind-mounted from host.
STAGING="${ORCHESTRATOR_STAGING_CONTAINER:-/orchestrator-staging}"
mkdir -p "$STAGING" || true

# Acquire distributed poller lock. If another orchestrator holds it, exit cleanly.
echo "[bootstrap] Acquiring poller lock..."
LOCK_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$PM_API_URL/api/poller/lock" \
    -H "Content-Type: application/json" \
    -d "{\"pid\": $$, \"host\": \"orchestrator-$(hostname)\"}" || echo "000")

if [ "$LOCK_STATUS" = "409" ]; then
    echo "[bootstrap] Another orchestrator holds the lock (409). Exiting."
    exit 0
elif [ "$LOCK_STATUS" != "200" ] && [ "$LOCK_STATUS" != "201" ]; then
    echo "[bootstrap] Unexpected lock response: $LOCK_STATUS — continuing (PM API may still be starting)."
fi

# Configure git credential helper using the GitHub PAT so the orchestrator's
# git operations (post-coder push, scaffold push, pat-rotate sweeps) succeed
# without the PAT being baked into remote URLs. Pulls fresh from PM API in
# case the operator rotated it after .env was loaded.
PAT=$(curl -s "$PM_API_URL/api/system-config" 2>/dev/null \
        | python3 -c "import sys,json; print((json.load(sys.stdin) or {}).get('github_pat') or '')" 2>/dev/null \
        || echo "")
if [ -n "$PAT" ]; then
    git config --global credential.helper store
    cat > ~/.git-credentials <<EOF
https://x-access-token:${PAT}@github.com
EOF
    chmod 600 ~/.git-credentials
    git config --global user.email "orchestrator@productfactory.local" 2>/dev/null || true
    git config --global user.name  "ProductFactory Orchestrator" 2>/dev/null || true
    echo "[bootstrap] Git credential helper configured."
else
    echo "[bootstrap] No GitHub PAT available — git push operations may fail until set."
fi

echo "[bootstrap] Starting orchestration loop..."
exec python3 /app/orchestrate.py
