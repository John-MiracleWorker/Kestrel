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

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is required}"

PORT="${KESTREL_PORT:-8765}"
export KESTREL_PORT="$PORT"
LOG_DIR="${KESTREL_STACK_LOG_DIR:-$HOME/.kestrel/logs}"
mkdir -p "$LOG_DIR"
SERVER_LOG="$LOG_DIR/server.log"
POLLER_LOG="$LOG_DIR/telegram-poller.log"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ -n "${POLLER_PID:-}" ]] && kill -0 "$POLLER_PID" 2>/dev/null; then
    kill "$POLLER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

export KESTREL_REPLACE_EXISTING="${KESTREL_REPLACE_EXISTING:-true}"
./scripts/start-telegram-agent.sh >>"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in {1..60}; do
  if curl -fsS "http://127.0.0.1:${PORT}/api/channels" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Kestrel server exited before readiness; see $SERVER_LOG" >&2
    exit 1
  fi
  sleep 1
done

: >"$POLLER_LOG"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
if [[ -z "$PYTHON_BIN" && -x .venv/bin/python ]]; then
  PYTHON_BIN=.venv/bin/python
fi
"$PYTHON_BIN" scripts/telegram-poller.py >>"$POLLER_LOG" 2>&1 &
POLLER_PID=$!

echo "Kestrel Telegram polling stack ready on 127.0.0.1:${PORT}"

while kill -0 "$SERVER_PID" 2>/dev/null && kill -0 "$POLLER_PID" 2>/dev/null; do
  sleep 5
done
exit 1
