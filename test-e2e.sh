#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Kestrel End-to-End Test Suite ==="

# 1. Test Web UI (Playwright)
echo "Running Web UI End-to-End Tests..."
npm run test:e2e --workspace=@kestrel/web

# 2. Test Desktop App (Rust unit tests + type check)
echo "Checking Tauri Desktop build..."
TAURI_DIR="$SCRIPT_DIR/packages/desktop/src-tauri"
if [ ! -d "$TAURI_DIR" ]; then
    echo "ERROR: Tauri directory not found at $TAURI_DIR" >&2
    exit 1
fi
pushd "$TAURI_DIR" > /dev/null
cargo test
popd > /dev/null

echo "=== All Tests Passed Successfully ==="
