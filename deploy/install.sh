#!/bin/bash
# ProductFactory — One-time setup script (Windows host, run in Git Bash or WSL)
#
# What this does:
#   1. Creates the Docker bridge network (productfactory-net)
#   2. Builds the agent image (productfactory-agent)
#   3. Builds and starts the infrastructure containers (PostgreSQL + PM website)
#   4. Installs the poller startup entry (Windows Startup folder)
#
# Prerequisites:
#   - Docker Desktop running
#   - .env file present (copy from .env.example and fill in)
#   - Python venv created: uv venv .venv && uv pip install -r requirements.txt

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "=== ProductFactory Setup ==="
echo ""

# ── 1. Check prerequisites ────────────────────────────────────────────────────
echo "[1/5] Checking prerequisites..."

if ! docker info &>/dev/null; then
    echo "  ERROR: Docker is not running. Start Docker Desktop first."
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "  ERROR: .env not found. Copy .env.example to .env and fill in values."
    exit 1
fi

if [ ! -f ".venv/Scripts/python.exe" ] && [ ! -f ".venv/bin/python" ]; then
    echo "  Creating Python venv..."
    uv venv .venv
    uv pip install -r requirements.txt
fi

echo "  OK"

# ── 2. Create Docker network (idempotent) ─────────────────────────────────────
echo "[2/5] Creating Docker network: productfactory-net"
if docker network inspect productfactory-net &>/dev/null; then
    echo "  Already exists — skipping"
else
    docker network create --driver bridge --opt com.docker.network.bridge.name=pf-net productfactory-net
    echo "  Created"
fi

# ── 3. Build agent image ──────────────────────────────────────────────────────
echo "[3/5] Building agent image (productfactory-agent)..."
echo "      First build takes 5-15 minutes (pyenv downloads Python runtimes)"
docker build \
    --file deploy/docker/Dockerfile \
    --tag productfactory-agent \
    --tag "productfactory-agent:$(date +%Y%m%d)" \
    .
echo "  Built: productfactory-agent"

# ── 4. Start infrastructure (PostgreSQL + PM website) ────────────────────────
echo "[4/5] Starting infrastructure containers..."
docker compose up -d --build
echo "  Waiting for PM website to be ready..."
for i in $(seq 1 30); do
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/ 2>/dev/null | grep -qE "200|401"; then
        echo "  PM website is up: http://localhost:8080"
        break
    fi
    sleep 2
done

# ── 5. Install poller startup entry ──────────────────────────────────────────
echo "[5/5] Installing poller startup entry..."
STARTUP_DIR="$APPDATA/Microsoft/Windows/Start Menu/Programs/Startup"
STARTUP_BAT="$STARTUP_DIR/ProductFactoryPoller.bat"

cat > "$STARTUP_BAT" << EOF
@echo off
powershell.exe -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "${REPO_ROOT//\//\\}\\deploy\\windows\\start_poller.ps1"
EOF

echo "  Installed: $STARTUP_BAT"
echo "  (Runs automatically on next Windows login)"

echo ""
echo "=== Setup complete ==="
echo ""
echo "PM Website:  http://localhost:8080  (admin / see .env PM_PASSWORD)"
echo "DB:          ProductsFactoryDB container (PostgreSQL 16)"
echo ""
echo "To start the poller now (no reboot needed):"
echo "  source .env && PYTHONIOENCODING=utf-8 .venv/Scripts/python -m orchestrator.poller"
echo ""
echo "Logs:"
echo "  docker compose logs -f          # web + db logs"
echo "  tail -f orchestrator/poller.log # poller log"
