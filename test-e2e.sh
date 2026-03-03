#!/bin/bash
set -e

echo "=== Kestrel End-to-End Test Suite ==="

# 1. Test Web UI (Playwright)
echo "Running Web UI End-to-End Tests..."
npm run test:e2e --workspace=@kestrel/web

# 2. Test Desktop App (Cargo Check)
echo "Checking Tauri Desktop build..."
cd packages/desktop/src-tauri
cargo check

echo "=== All Tests Passed Successfully ==="
