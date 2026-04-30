#!/bin/bash
# ProductFactory agent container entrypoint.
#
# Purpose: ensure the GitHub PAT lives in `~/.git-credentials` (gitignored,
# never echoed by `git remote`) instead of being embedded in the
# `origin` remote URL. Without this, every `git remote -v` shows the PAT
# in plaintext and the LLM forwards it to Ollama Cloud as part of context.
#
# Runs once per `docker run` before the agent process starts.

set -e

# 1. Set up git credential helper if GH_TOKEN is available.
if [ -n "${GH_TOKEN:-}" ]; then
    git config --global credential.helper store
    # Format git's credential store expects:
    #   https://USER:PASSWORD@HOST
    # GitHub's `x-access-token:<PAT>` form is the documented equivalent.
    cred_file="${HOME}/.git-credentials"
    cat > "$cred_file" <<EOF
https://x-access-token:${GH_TOKEN}@github.com
EOF
    chmod 600 "$cred_file"
    # Identity needed for any commits the agent makes.
    git config --global user.email "agent@productfactory.local" 2>/dev/null || true
    git config --global user.name  "ProductFactory Agent" 2>/dev/null || true
fi

# 2. If the workspace already has an embedded-PAT remote URL (legacy products),
#    rewrite it to plain HTTPS. The credential helper will supply the PAT for
#    fetch/push without it ever appearing in `git remote -v` output.
if [ -d /workspace/.git ] && [ -n "${GH_TOKEN:-}" ]; then
    current_url=$(git -C /workspace remote get-url origin 2>/dev/null || true)
    if [ -n "$current_url" ]; then
        # Strip `x-access-token:<token>@` from the URL — keep host + path intact.
        cleaned=$(echo "$current_url" | sed -E 's#https://x-access-token:[^@]+@#https://#')
        if [ "$cleaned" != "$current_url" ]; then
            git -C /workspace remote set-url origin "$cleaned"
        fi
    fi
fi

exec "$@"
