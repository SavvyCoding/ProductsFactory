#!/usr/bin/env bash
# pf-verify-env.sh — Mechanical preflight checks for the agent's environment.
#
# Runs from agent-entrypoint.sh BEFORE the agent process starts. Exit 42 =
# env-not-ready: the orchestrator (_finalize_session, detect_kill_recovery)
# special-cases this code so the affected features are NOT charged a
# fix_attempt. Operator gets an alert with the failing-check string.
#
# Every check is purely deterministic (binary exists, exec bit, version
# resolves). No LLM judgment, no free-text matching. False positives would
# bin healthy features and have to be triaged out by hand, so the bias here
# is hard towards "miss rather than false-fire" — only check things that
# are mechanically true/false.
#
# Adding a new check: append a stack-detection clause below. Keep it in the
# format `if <marker>; then <check> || fail "<msg>"; fi`. Markers should be
# in the workspace (package.json key, requirements.txt mention, etc.) so we
# only validate toolchains the product actually uses.

set +e

WORKSPACE="${WORKSPACE_DIR:-/workspace}"
fail() { echo "pf-verify-env: $1" >&2; exit 42; }

# ── Always-required tools ─────────────────────────────────────────────────────
command -v git     >/dev/null || fail "git missing"
command -v node    >/dev/null || fail "node missing"
command -v python3 >/dev/null || fail "python3 missing"

# ── Node + Playwright ─────────────────────────────────────────────────────────
# Playwright bundles chrome-headless-shell at install time. Two failure
# modes the agent can't repair from inside its session:
#   1. Binary missing entirely (image was built without playwright install)
#   2. Binary present but lacks exec bit (Windows-bind-mount install path)
# Both surface as test-time EACCES; the coder spent 5 fix_attempts on #394
# fighting executablePath workarounds before auto-block. Pre-flighting here
# routes the failure to the operator instead of the retry loop.
if [ -f "$WORKSPACE/package.json" ] && grep -q '"@playwright/test"\|"playwright"' "$WORKSPACE/package.json" 2>/dev/null; then
    PW_DIR="${PLAYWRIGHT_BROWSERS_PATH:-/home/agent/.cache/ms-playwright}"
    PW_BIN=$(find "$PW_DIR" -type f -name 'chrome-headless-shell' 2>/dev/null | head -1)
    if [ -z "$PW_BIN" ]; then
        # Fallback to the full chrome browser if headless-shell variant absent
        PW_BIN=$(find "$PW_DIR" -type f -name 'chrome' 2>/dev/null | head -1)
    fi
    if [ -z "$PW_BIN" ]; then
        fail "playwright in package.json but no chrome binary in $PW_DIR (rebuild agent image)"
    fi
    if [ ! -x "$PW_BIN" ]; then
        fail "$PW_BIN exists but lacks exec bit"
    fi
fi

# ── Python toolchain ──────────────────────────────────────────────────────────
if [ -f "$WORKSPACE/pyproject.toml" ] || [ -f "$WORKSPACE/requirements.txt" ] || [ -f "$WORKSPACE/setup.py" ]; then
    python3 -c "import pytest" 2>/dev/null || fail "Python project detected but pytest not importable"
fi

# ── Go toolchain ──────────────────────────────────────────────────────────────
if [ -f "$WORKSPACE/go.mod" ]; then
    command -v go >/dev/null || fail "go.mod present but go binary missing"
fi

echo "pf-verify-env: ok"
exit 0
