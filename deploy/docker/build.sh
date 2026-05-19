#!/usr/bin/env bash
# ProductFactory — build agent image + create isolated Docker network.
# Run from the repo root: bash deploy/docker/build.sh
#
# Options:
#   --no-cache   Force a full rebuild (ignores Docker layer cache)
#   --test       Run a smoke test after building
#   --tag TAG    Image tag (default: productfactory-agent)

set -euo pipefail

IMAGE_TAG="productfactory-agent"
ORCHESTRATOR_TAG="productfactory-orchestrator"
NO_CACHE=""
RUN_TEST=false
BUILD_ORCHESTRATOR=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-cache)      NO_CACHE="--no-cache" ;;
        --test)          RUN_TEST=true ;;
        --tag)           IMAGE_TAG="$2"; shift ;;
        --orchestrator)  BUILD_ORCHESTRATOR=true ;;
        --hermes)        BUILD_ORCHESTRATOR=true ;; # legacy alias
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

echo "=== ProductFactory Docker Build ==="
echo "Image: ${IMAGE_TAG}"
echo ""

# ── 1. Create isolated bridge network (idempotent) ────────────────────────────
if docker network inspect productfactory-net &>/dev/null; then
    echo "[1/3] Network 'productfactory-net' already exists — skipping"
else
    echo "[1/3] Creating isolated bridge network: productfactory-net"
    docker network create \
        --driver bridge \
        --opt com.docker.network.bridge.name=pf-net \
        productfactory-net
    echo "      Created."
fi

# ── 2. Build agent image ──────────────────────────────────────────────────────
echo "[2/3] Building image: ${IMAGE_TAG}"
echo "      (This takes 5-15 minutes on first build — pyenv downloads Python)"

docker build \
    $NO_CACHE \
    --file deploy/docker/Dockerfile \
    --tag "${IMAGE_TAG}" \
    --tag "${IMAGE_TAG}:$(date +%Y%m%d)" \
    --label "productfactory.built=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    .

echo "      Built: ${IMAGE_TAG}"

# ── 3. Optional smoke test ────────────────────────────────────────────────────
if [ "$RUN_TEST" = true ]; then
    echo "[3/3] Running smoke test..."
    bash deploy/docker/test_image.sh "${IMAGE_TAG}"
else
    echo "[3/3] Skipping smoke test (run with --test to verify)"
fi

# ── 4. Optional orchestrator image ────────────────────────────────────────────
if [ "$BUILD_ORCHESTRATOR" = true ]; then
    echo ""
    echo "[4/4] Building orchestrator image: ${ORCHESTRATOR_TAG}"
    docker build \
        $NO_CACHE \
        --file deploy/docker/Dockerfile.orchestrator \
        --tag "${ORCHESTRATOR_TAG}" \
        --tag "${ORCHESTRATOR_TAG}:$(date +%Y%m%d)" \
        --label "productfactory.built=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        .
    echo "      Built: ${ORCHESTRATOR_TAG}"
fi

echo ""
echo "=== Done ==="
echo "Agent image: ${IMAGE_TAG}"
[ "$BUILD_ORCHESTRATOR" = true ] && echo "Orchestrator image: ${ORCHESTRATOR_TAG}"
echo ""
echo "Next steps:"
echo "  1. Start infrastructure:   docker compose up -d  (from repo root)"
echo "  2. Orchestrator:           docker compose --profile orchestrator up -d"
# Legacy host-mode poller (orchestrator/poller.py via deploy/windows/start_poller.ps1)
# was retired 2026-05-18 — see those files' deprecation guards and INVARIANTS.md
# preface for the consolidation history. No longer mentioned here so new
# contributors aren't directed at the dead path.
