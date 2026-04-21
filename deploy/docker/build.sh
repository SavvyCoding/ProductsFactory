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
HERMES_TAG="productfactory-hermes"
NO_CACHE=""
RUN_TEST=false
BUILD_HERMES=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-cache) NO_CACHE="--no-cache" ;;
        --test)     RUN_TEST=true ;;
        --tag)      IMAGE_TAG="$2"; shift ;;
        --hermes)   BUILD_HERMES=true ;;
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

# ── 4. Optional Hermes orchestrator image ─────────────────────────────────────
if [ "$BUILD_HERMES" = true ]; then
    echo ""
    echo "[4/4] Building Hermes orchestrator image: ${HERMES_TAG}"
    docker build \
        $NO_CACHE \
        --file deploy/docker/Dockerfile.hermes \
        --tag "${HERMES_TAG}" \
        --tag "${HERMES_TAG}:$(date +%Y%m%d)" \
        --label "productfactory.built=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        .
    echo "      Built: ${HERMES_TAG}"
fi

echo ""
echo "=== Done ==="
echo "Agent image: ${IMAGE_TAG}"
[ "$BUILD_HERMES" = true ] && echo "Hermes image: ${HERMES_TAG}"
echo ""
echo "Next steps:"
echo "  1. Start infrastructure:  docker compose up -d  (from repo root)"
echo "  2a. Hermes orchestrator:  docker compose --profile hermes up -d hermes"
echo "  2b. (or legacy) Poller:   bash deploy/install.sh  (or run start_poller.ps1)"
