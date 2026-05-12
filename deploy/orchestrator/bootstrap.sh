#!/bin/bash
# ProductFactory orchestrator bootstrap — runs inside the container on startup.
# Acquires the poller lock, then runs the orchestration loop.

set -euo pipefail

# Docker Desktop on Windows surfaces bind-mounted files as root:root with
# 777 perms regardless of host metadata. OpenSSH then refuses to use
# .ssh/config and any private key ("Bad owner or permissions on .ssh/...").
# Fix the in-container view at startup (host filesystem unaffected), then
# drop to the orchestrator user before running the orchestration loop. The
# docker-compose service runs us as user:"0" so we can do this; if someone
# changes that, the chown/chmod silently fail and we proceed as-is.
if [ "$(id -u)" = "0" ]; then
    SSH_DIR=/home/orchestrator/.ssh
    if [ -d "$SSH_DIR" ]; then
        chown -R orchestrator:orchestrator "$SSH_DIR" 2>/dev/null || true
        chmod 700 "$SSH_DIR" 2>/dev/null || true
        for f in "$SSH_DIR"/config "$SSH_DIR"/known_hosts; do
            [ -f "$f" ] && chmod 600 "$f" 2>/dev/null || true
        done
        for k in "$SSH_DIR"/id_ed25519_*; do
            [ -f "$k" ] || continue
            case "$k" in
                *.pub) chmod 644 "$k" 2>/dev/null || true ;;
                *)     chmod 600 "$k" 2>/dev/null || true ;;
            esac
        done
        echo "[bootstrap] SSH dir perms normalised"
    fi
    # `-G root` preserves the supplementary root group that docker-compose adds
    # via `group_add: ["0"]` for /var/run/docker.sock access. runuser otherwise
    # reads groups from /etc/group only, dropping the runtime-added group; the
    # orchestrator would then be unable to talk to the docker daemon and every
    # agent-container launch would die instantly with exit 1.
    exec runuser -u orchestrator -G root -G dockerhost -- "$0" "$@"
fi

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
