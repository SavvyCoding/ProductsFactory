#!/bin/bash
# ProductFactory — One-time setup script (Windows host, run in Git Bash or WSL)
#
# What this does:
#   1. Creates the Docker bridge network (productfactory-net)
#   2. Builds the agent image (productfactory-agent)
#   3. Builds and starts the infrastructure containers (PostgreSQL + PM website)
#
# (Step 4 was "Install the poller startup entry"; the legacy host-mode poller
# was retired 2026-05-18 — the orchestrator now runs inside the
# pf-orchestrator container started by `docker compose --profile orchestrator
# up -d`.)
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
echo "[1/4] Checking prerequisites..."

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
echo "[2/4] Creating Docker network: productfactory-net"
if docker network inspect productfactory-net &>/dev/null; then
    echo "  Already exists — skipping"
else
    docker network create --driver bridge --opt com.docker.network.bridge.name=pf-net productfactory-net
    echo "  Created"
fi

# ── 3. Build agent image ──────────────────────────────────────────────────────
echo "[3/4] Building agent image (productfactory-agent)..."
echo "      First build takes 5-15 minutes (pyenv downloads Python runtimes)"
docker build \
    --file deploy/docker/Dockerfile \
    --tag productfactory-agent \
    --tag "productfactory-agent:$(date +%Y%m%d)" \
    .
echo "  Built: productfactory-agent"

# ── 4. Start infrastructure (PostgreSQL + PM website) ────────────────────────
echo "[4/4] Starting infrastructure containers..."
docker compose up -d --build
echo "  Waiting for PM website to be ready..."
for i in $(seq 1 30); do
    if curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/ 2>/dev/null | grep -qE "200|401"; then
        echo "  PM website is up: http://localhost:8080"
        break
    fi
    sleep 2
done

echo ""
echo "=== Setup complete ==="
echo ""
echo "PM Website:  http://localhost:8080  (admin / see .env PM_PASSWORD)"
echo "DB:          ProductsFactoryDB container (PostgreSQL 16)"
echo ""
echo "To start the orchestrator:"
echo "  docker compose --profile orchestrator up -d"
echo ""
echo "Logs:"
echo "  docker compose logs -f                    # web + db logs"
echo "  docker logs -f pf-orchestrator            # orchestrator logs"
