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
    # /workspace symlink-farm root. Designer Verify recipes and the test code
    # coders write following them routinely use absolute /workspace/... paths
    # (the agent container mounts the product at /workspace). The post-coder
    # pipeline runs in this orchestrator container where /workspace doesn't
    # exist by default, so every such test/recipe fails with "no such file".
    # We create /workspace as a dir owned by the orchestrator user so the
    # post-coder pipeline can populate it with subdir symlinks pointing at
    # the current product's working_dir (see _workspace_symlink in
    # orchestrator/pipelines/post_coder.py). Doing this here (as root, before
    # re-exec to the orchestrator user) is the only place / can be written.
    # Canonical 2026-05-31 DocumentSign cascade: features 1147/1148/1149.
    mkdir -p /workspace
    chown orchestrator:orchestrator /workspace
    chmod 0755 /workspace
    echo "[bootstrap] /workspace dir ready for symlink-farm population"

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

# Git auth is GitHub App installation tokens, minted per-session via
# orchestrator.integrations.git_ops::_git_with_token. That helper uses a
# one-shot `git -c credential.helper=<inline>` so the token is never
# persisted to disk. Bootstrap must NOT configure a global credential.helper
# (e.g. `store`): when both a global and a one-shot helper are configured,
# git invokes them in order and the FIRST one to return credentials wins.
# A persistent helper backed by a stale/revoked PAT would intercept every
# request before the one-shot's App token had a chance to run.
#
# Belt-and-braces: remove any global credential.helper inherited from prior
# starts (image layer cache, leftover ~/.gitconfig from a bind-mounted home).
git config --global --unset-all credential.helper 2>/dev/null || true
# Defensive: zero out any leftover ~/.git-credentials so even if some other
# code path adds a `store` helper, it has nothing to return.
: > ~/.git-credentials 2>/dev/null || true
chmod 600 ~/.git-credentials 2>/dev/null || true
echo "[bootstrap] Git auth: GitHub App per-session tokens (no global credential helper)."

echo "[bootstrap] Starting orchestration loop..."
exec python3 /app/orchestrate.py
