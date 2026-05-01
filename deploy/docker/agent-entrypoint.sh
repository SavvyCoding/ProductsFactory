#!/bin/bash
# ProductFactory agent container entrypoint.
#
# Purpose: ensure the GitHub PAT lives in a credentials file (gitignored,
# never echoed by `git remote`) instead of being embedded in the
# `origin` remote URL. Without this, every `git remote -v` shows the PAT
# in plaintext and the LLM forwards it to Ollama Cloud as part of context.
#
# Runs once per `docker run` before the agent process starts.
#
# Two non-obvious constraints from docker_runner.py:
#   1. `GH_TOKEN` is NOT in env at this point. The orchestrator passes the
#      token via a file mount at /run/secrets/gh_token; the export happens
#      inside the agent_cmd's sh -c prelude, *after* this entrypoint's
#      exec "$@". So we read the file directly here.
#   2. The container runs `--read-only` with only specific tmpfs writable
#      mounts (/tmp, /run, /home/agent/.cache|.npm|.config|.local). Writing
#      ~/.git-credentials or ~/.gitconfig directly would EROFS. We park
#      both files under /tmp and point git at them via env vars.

set -e

# Resolve GH_TOKEN. Prefer env (legacy direct -e injection); otherwise read
# the mounted secret file.
if [ -z "${GH_TOKEN:-}" ] && [ -r /run/secrets/gh_token ]; then
    GH_TOKEN=$(cat /run/secrets/gh_token)
fi

if [ -n "${GH_TOKEN:-}" ]; then
    cred_file="/tmp/.git-credentials"
    cat > "$cred_file" <<EOF
https://x-access-token:${GH_TOKEN}@github.com
EOF
    chmod 600 "$cred_file"

    # Redirect git's global config to /tmp (rootfs is read-only). The agent
    # process inherits these env vars after `exec "$@"`.
    export GIT_CONFIG_GLOBAL=/tmp/.gitconfig
    : > "$GIT_CONFIG_GLOBAL"
    git config --global credential.helper "store --file=${cred_file}"
    git config --global user.email "agent@productfactory.local"
    git config --global user.name  "ProductFactory Agent"
    # Workspace bind-mount may be owned by a UID the container doesn't trust;
    # tell git to operate on it anyway. Same setting the orchestrator image
    # uses; harmless when ownership matches.
    git config --global --add safe.directory '*'
fi

# If the workspace already has an embedded-PAT remote URL (legacy products),
# rewrite it to plain HTTPS. The credential helper will supply the PAT for
# fetch/push without it ever appearing in `git remote -v` output.
# Skipped when GH_TOKEN is unavailable — leaving the embedded URL in place
# is better than breaking auth entirely.
if [ -d /workspace/.git ] && [ -n "${GH_TOKEN:-}" ]; then
    current_url=$(git -C /workspace remote get-url origin 2>/dev/null || true)
    if [ -n "$current_url" ]; then
        cleaned=$(echo "$current_url" | sed -E 's#https://x-access-token:[^@]+@#https://#')
        if [ "$cleaned" != "$current_url" ]; then
            git -C /workspace remote set-url origin "$cleaned"
        fi
    fi
fi

exec "$@"
