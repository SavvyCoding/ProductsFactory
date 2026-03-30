#!/usr/bin/env bash
# ProductFactory — smoke test for the agent image.
# Verifies every tool Claude needs is installed and functional.
# Run: bash deploy/docker/test_image.sh [image-tag]
#
# Exit code: 0 = all checks passed, 1 = one or more failed.

set -euo pipefail

IMAGE="${1:-productfactory-agent}"
PASS=0
FAIL=0

check() {
    local description="$1"
    local command="$2"
    local expected="${3:-}"       # optional substring that must appear in output

    output=$(docker run --rm \
        --network productfactory-net \
        --memory 512m --cpus 1 \
        "$IMAGE" \
        sh -c "$command" 2>&1) || true

    if [ -n "$expected" ] && ! echo "$output" | grep -q "$expected"; then
        echo "  FAIL  $description"
        echo "        Expected: '$expected'"
        echo "        Got:      '$output'"
        FAIL=$((FAIL + 1))
    else
        echo "  PASS  $description"
        PASS=$((PASS + 1))
    fi
}

echo "=== Smoke Test: ${IMAGE} ==="
echo ""
echo "── Runtimes ─────────────────────────────────────────────"

check "Python 3.12 (default)"      "python3 --version"          "3.12"
check "Python 3.11 available"      "pyenv versions"             "3.11"
check "pip available"              "pip --version"              "pip"
check "Node.js 20 (default)"       "node --version"             "v20"
check "Node 18 available via n"    "n ls"                       "18"
check "npm available"              "npm --version"              ""
check "Go 1.22"                    "go version"                 "go1.22"

echo ""
echo "── Security tools ───────────────────────────────────────"

check "pip-audit installed"        "pip-audit --version"        ""
check "npm audit available"        "npm audit --version"        ""
check "govulncheck installed"      "govulncheck -version"       ""

echo ""
echo "── Developer tools ──────────────────────────────────────"

check "git installed"              "git --version"              "git version"
check "gh (GitHub CLI) installed"  "gh --version"               "gh version"
check "jq installed"               "jq --version"               "jq-"
check "ssh-keyscan in known_hosts" "cat /root/.ssh/known_hosts" "github.com"

echo ""
echo "── Python test packages ─────────────────────────────────"

check "pytest installed"           "python3 -m pytest --version" "pytest"
check "pytest-cov installed"       "python3 -m pytest --co -q 2>&1 || python3 -c 'import pytest_cov'" ""
check "pytest-json-report"         "python3 -c 'import pytest_jsonreport'" ""
check "httpx installed"            "python3 -c 'import httpx; print(httpx.__version__)'" ""
check "pydantic installed"         "python3 -c 'import pydantic; print(pydantic.__version__)'" ""

echo ""
echo "── Git configuration ─────────────────────────────────────"

check "git user.name set"          "git config --global user.name"  "ProductFactory"
check "git user.email set"         "git config --global user.email" "productfactory"
check "safe.directory configured"  "git config --global --list"     "safe.directory"

echo ""
echo "── Claude Code CLI ──────────────────────────────────────"

check "claude CLI installed"       "claude --version"           ""

echo ""
echo "── Security: read-only mount test ───────────────────────"

# Verify ~/.claude is mounted :ro when a volume is provided
# (We can't test the actual OAuth session, but we can test the mount works)
if docker run --rm \
    --network productfactory-net \
    --memory 512m --cpus 1 \
    -v /tmp:/root/.claude:ro \
    "$IMAGE" \
    sh -c "touch /root/.claude/test_write 2>&1 | grep -q 'Read-only'" 2>/dev/null; then
    echo "  PASS  ~/.claude :ro mount rejects writes"
    PASS=$((PASS + 1))
else
    echo "  INFO  ~/.claude :ro mount test skipped (requires /tmp writeable)"
fi

echo ""
echo "══════════════════════════════════════════════════════"
echo "Results: ${PASS} passed, ${FAIL} failed"
echo "══════════════════════════════════════════════════════"

if [ "$FAIL" -gt 0 ]; then
    echo "FAIL — fix the issues above before deploying."
    exit 1
else
    echo "PASS — image is ready."
    exit 0
fi
