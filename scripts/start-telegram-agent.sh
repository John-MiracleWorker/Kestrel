#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${KESTREL_TELEGRAM_ENV:-.env.telegram}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE. Copy .env.telegram.example to .env.telegram and fill secrets." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

export NEST_AGENT_CHANNEL_CONFIG="${NEST_AGENT_CHANNEL_CONFIG:-.nest/config/channels.json}"
export NEST_AGENT_ENABLE_CHANNEL_DELIVERY="${NEST_AGENT_ENABLE_CHANNEL_DELIVERY:-true}"
export NEST_AGENT_PROVIDER="${NEST_AGENT_PROVIDER:-ollama-cloud}"
export NEST_AGENT_MODEL="${NEST_AGENT_MODEL:-deepseek-v4-pro}"
export NEST_AGENT_API_KEY_ENV="${NEST_AGENT_API_KEY_ENV:-OLLAMA_API_KEY}"

# Default to the permanent architecture: one Kestrel server owns the shared
# runtime/Memvid store, and both Web UI + Telegram route through that process.
# Set KESTREL_TELEGRAM_RUNTIME=isolated for a separate Telegram-only runtime.
export KESTREL_TELEGRAM_RUNTIME="${KESTREL_TELEGRAM_RUNTIME:-shared}"
case "$KESTREL_TELEGRAM_RUNTIME" in
  shared)
    export NEST_AGENT_BACKEND="${NEST_AGENT_BACKEND:-memvid}"
    export NEST_AGENT_MEMORY_DIR="${NEST_AGENT_MEMORY_DIR:-.nest/memory}"
    export NEST_AGENT_LOG_DIR="${NEST_AGENT_LOG_DIR:-.nest/logs}"
    export NEST_AGENT_STATE_PATH="${NEST_AGENT_STATE_PATH:-.nest/state/agent.db}"
    export NEST_AGENT_SECRET_STORE_PATH="${NEST_AGENT_SECRET_STORE_PATH:-.nest/secrets/local_vault.json}"
    ;;
  isolated)
    export NEST_AGENT_BACKEND="${NEST_AGENT_BACKEND:-memvid}"
    export NEST_AGENT_MEMORY_DIR="${NEST_AGENT_MEMORY_DIR:-.nest/telegram/memory}"
    export NEST_AGENT_LOG_DIR="${NEST_AGENT_LOG_DIR:-.nest/telegram/logs}"
    export NEST_AGENT_STATE_PATH="${NEST_AGENT_STATE_PATH:-.nest/telegram/state/agent.db}"
    export NEST_AGENT_SECRET_STORE_PATH="${NEST_AGENT_SECRET_STORE_PATH:-.nest/telegram/secrets/local_vault.json}"
    ;;
  *)
    echo "Unsupported KESTREL_TELEGRAM_RUNTIME=$KESTREL_TELEGRAM_RUNTIME; expected shared or isolated." >&2
    exit 1
    ;;
esac
export NEST_AGENT_TRUSTED_HOSTS="${NEST_AGENT_TRUSTED_HOSTS:-127.0.0.1,localhost,::1,[::1],testserver,*.trycloudflare.com}"

export NEST_AGENT_WORKSPACE="${NEST_AGENT_WORKSPACE:-$PWD}"
export NEST_AGENT_TIMEOUT_SECONDS="${NEST_AGENT_TIMEOUT_SECONDS:-300}"
export NEST_AGENT_MAX_TOOL_ROUNDS="${NEST_AGENT_MAX_TOOL_ROUNDS:-8}"
export NEST_AGENT_CONTEXT_BUDGET_CHARS="${NEST_AGENT_CONTEXT_BUDGET_CHARS:-12000}"

if [[ "${KESTREL_PRINT_ENV:-false}" == "1" || "${KESTREL_PRINT_ENV:-false}" == "true" ]]; then
  for name in \
    KESTREL_TELEGRAM_RUNTIME \
    NEST_AGENT_CHANNEL_CONFIG \
    NEST_AGENT_ENABLE_CHANNEL_DELIVERY \
    NEST_AGENT_PROVIDER \
    NEST_AGENT_MODEL \
    NEST_AGENT_API_KEY_ENV \
    NEST_AGENT_BACKEND \
    NEST_AGENT_MEMORY_DIR \
    NEST_AGENT_LOG_DIR \
    NEST_AGENT_STATE_PATH \
    NEST_AGENT_SECRET_STORE_PATH \
    NEST_AGENT_TRUSTED_HOSTS \
    NEST_AGENT_WORKSPACE; do
    printf '%s=%s\n' "$name" "${!name}"
  done
  exit 0
fi

PORT="${KESTREL_PORT:-8765}"
if command -v lsof >/dev/null 2>&1; then
  existing_pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$existing_pids" ]]; then
    if [[ "${KESTREL_REPLACE_EXISTING:-false}" == "true" ]]; then
      kill $existing_pids
      sleep 1
    else
      echo "Port $PORT is already in use by PID(s): $existing_pids" >&2
      echo "Stop the existing Kestrel server or set KESTREL_REPLACE_EXISTING=true to replace it." >&2
      exit 1
    fi
  fi
fi

exec .venv/bin/nest-agent server --host 127.0.0.1 --port "$PORT"
