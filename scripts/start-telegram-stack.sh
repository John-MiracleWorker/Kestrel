#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

# launchd starts with a minimal PATH. Include the user-local locations used by
# optional providers such as codex-cli, while keeping Python imports isolated.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
unset PYTHONPATH

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
export NEST_AGENT_REQUIRE_API_AUTH="${NEST_AGENT_REQUIRE_API_AUTH:-true}"
export NEST_AGENT_API_AUTH_TOKEN_ENV="${NEST_AGENT_API_AUTH_TOKEN_ENV:-NEST_AGENT_API_TOKEN}"

PORT="${KESTREL_PORT:-8765}"
export KESTREL_PORT="$PORT"
LOG_DIR="${KESTREL_STACK_LOG_DIR:-$HOME/.kestrel/logs}"
mkdir -p "$LOG_DIR"
chmod 700 "$LOG_DIR"
SERVER_LOG="$LOG_DIR/server.log"
POLLER_LOG="$LOG_DIR/telegram-poller.log"
LOG_MAX_BYTES="${KESTREL_LOG_MAX_BYTES:-10485760}"
export KESTREL_SERVER_ACCESS_LOG="${KESTREL_SERVER_ACCESS_LOG:-false}"
if ! [[ "$LOG_MAX_BYTES" =~ ^[0-9]+$ ]] || (( LOG_MAX_BYTES == 0 )); then
  echo "KESTREL_LOG_MAX_BYTES must be a positive integer." >&2
  exit 1
fi

rotate_log() {
  local path="$1"
  local bytes=0
  if [[ -f "$path" ]]; then
    bytes="$(wc -c <"$path")"
  fi
  if [[ "$bytes" =~ ^[[:space:]]*[0-9]+[[:space:]]*$ ]] && (( bytes >= LOG_MAX_BYTES )); then
    rm -f "$path.1"
    mv "$path" "$path.1"
    chmod 600 "$path.1"
  fi
  touch "$path"
  chmod 600 "$path"
}

rotate_log "$SERVER_LOG"
rotate_log "$POLLER_LOG"

if [[ -n "${KESTREL_TELEGRAM_PYTHON:-${PYTHON_BIN:-}}" ]]; then
  PYTHON_BIN="${KESTREL_TELEGRAM_PYTHON:-$PYTHON_BIN}"
elif [[ -x .venv/bin/python ]]; then
  PYTHON_BIN=".venv/bin/python"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3 is required for the Telegram stack." >&2
  exit 1
fi

server_is_ready() {
  "$PYTHON_BIN" - "http://127.0.0.1:${PORT}/api/channels" <<'PY'
import os
import sys
import urllib.request

token_env = os.environ.get("NEST_AGENT_API_AUTH_TOKEN_ENV", "NEST_AGENT_API_TOKEN").strip()
token = os.environ.get(token_env, "").strip()
headers = {"Authorization": f"Bearer {token}"} if token else {}
request = urllib.request.Request(sys.argv[1], headers=headers)
with urllib.request.urlopen(request, timeout=3) as response:
    response.read()
PY
}

# Invoked indirectly by the EXIT/INT/TERM trap below.
# shellcheck disable=SC2329
cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
  fi
  if [[ -n "${POLLER_PID:-}" ]] && kill -0 "$POLLER_PID" 2>/dev/null; then
    kill "$POLLER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

export KESTREL_REPLACE_EXISTING="${KESTREL_REPLACE_EXISTING:-false}"
./scripts/start-telegram-agent.sh >>"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

for _ in {1..60}; do
  if server_is_ready >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Kestrel server exited before readiness; see $SERVER_LOG" >&2
    exit 1
  fi
  sleep 1
done

"$PYTHON_BIN" scripts/telegram-poller.py >>"$POLLER_LOG" 2>&1 &
POLLER_PID=$!

echo "Kestrel Telegram polling stack ready on 127.0.0.1:${PORT}"

while kill -0 "$SERVER_PID" 2>/dev/null && kill -0 "$POLLER_PID" 2>/dev/null; do
  sleep 5
done
exit 1
