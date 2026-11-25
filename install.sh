#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_BIN_DIR="$HOME/.local/bin"

echo "==> Syncing Python dependencies with uv..."
cd "$PROJECT_DIR"
uv sync

echo "==> Linking CLI command to $LOCAL_BIN_DIR/amarok-ytmusic..."
mkdir -p "$LOCAL_BIN_DIR"
ln -sf "$PROJECT_DIR/.venv/bin/amarok-ytmusic" "$LOCAL_BIN_DIR/amarok-ytmusic"

"$PROJECT_DIR/.venv/bin/amarok-ytmusic" setup --exec-cmd "$PROJECT_DIR/.venv/bin/amarok-ytmusic" "$@"
