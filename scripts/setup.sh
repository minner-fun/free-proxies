#!/usr/bin/env bash
# One-shot setup on a new machine: install dependencies + download the mihomo core binary.
# Usage: bash scripts/setup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MIHOMO_VERSION="v1.19.28"

echo "==> Installing Python dependencies (uv sync)"
uv sync

if [ -x "bin/mihomo" ]; then
    echo "==> bin/mihomo already exists, skipping download: $(bin/mihomo -v | head -1)"
else
    echo "==> Downloading mihomo ${MIHOMO_VERSION}"
    mkdir -p bin
    ARCH="$(uname -m)"
    case "$ARCH" in
        x86_64)  GOARCH="amd64" ;;
        aarch64|arm64) GOARCH="arm64" ;;
        *) echo "Unknown architecture $ARCH, please download manually from https://github.com/MetaCubeX/mihomo/releases" && exit 1 ;;
    esac
    URL="https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-${GOARCH}-${MIHOMO_VERSION}.gz"
    curl -fSL --retry 3 -o bin/mihomo.gz "$URL"
    gunzip -f bin/mihomo.gz
    chmod +x bin/mihomo
    echo "==> Done: $(bin/mihomo -v | head -1)"
fi

echo ""
echo "Setup complete. Next step:"
echo "  uv run python -m free_proxies all    # fetch subscriptions + validate, generate data/good.yaml"
