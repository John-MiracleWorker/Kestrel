#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_REPO="https://github.com/John-MiracleWorker/Kestrel.git"
DEFAULT_HOME="${HOME}/.kestrel-agent"
DEFAULT_EXTRAS="memvid,openai,server,mcp,dev"
DEFAULT_PORT="8765"
DEFAULT_START_SERVER="1"
DEFAULT_OPEN_BROWSER="1"
DEFAULT_SERVER_SESSION="kestrel-agent"

usage() {
  cat <<'EOF'
Kestrel one-shot installer

Install from GitHub:
  curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/main/install.sh | bash

Environment options:
  KESTREL_HOME          Install directory. Defaults to $HOME/.kestrel-agent.
  KESTREL_REPO          Git repository URL or local path. Defaults to https://github.com/John-MiracleWorker/Kestrel.git.
  KESTREL_REF           Git ref to install. Defaults to main.
  KESTREL_PYTHON        Python 3.11+ interpreter path/name to use.
  KESTREL_EXTRAS        Python extras to install. Defaults to memvid,openai,server,mcp,dev.
  KESTREL_SKIP_WEB      Set to 1/true to skip npm ci and web build.
  KESTREL_SKIP_SMOKE    Set to 1/true to skip doctor/chat smoke checks.
  KESTREL_START_SERVER  Set to 0/false to skip launching the local server and web UI. Defaults to 1.
  KESTREL_OPEN_BROWSER  Set to 0/false to skip opening the web UI in your browser. Defaults to 1.
  KESTREL_SERVER_SESSION Detached screen/tmux session name. Defaults to kestrel-agent.
  KESTREL_SERVER_LOG    Server log path. Defaults to $KESTREL_HOME/.nest/server.log.
  KESTREL_SERVER_PID    Server PID file path. Defaults to $KESTREL_HOME/.nest/server.pid.
  KESTREL_PORT          Server port. Defaults to 8765.
  KESTREL_DRY_RUN       Set to 1/true to print commands without mutating the system.

Safe default runtime:
  backend=memvid, provider=mock, model=mock, high-risk tool flags disabled.
EOF
}

log() {
  printf '[kestrel-install] %s\n' "$*"
}

die() {
  printf '[kestrel-install] ERROR: %s\n' "$*" >&2
  exit 1
}

is_true() {
  case "${1:-}" in
    1 | true | TRUE | yes | YES | y | Y | on | ON) return 0 ;;
    *) return 1 ;;
  esac
}

is_false() {
  case "${1:-}" in
    0 | false | FALSE | no | NO | n | N | off | OFF) return 0 ;;
    *) return 1 ;;
  esac
}

quote_cmd() {
  local quoted=()
  local arg
  for arg in "$@"; do
    printf -v arg '%q' "$arg"
    quoted+=("$arg")
  done
  printf '%s' "${quoted[*]}"
}

run() {
  log "+ $(quote_cmd "$@")"
  if is_true "${KESTREL_DRY_RUN:-}"; then
    return 0
  fi
  "$@"
}

is_nonempty_dir() {
  local dir="$1"
  [[ -d "$dir" ]] || return 1
  [[ -n "$(find "$dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]
}

python_is_311_or_newer() {
  local candidate="$1"
  "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

detect_python() {
  local candidates=()
  if [[ -n "${KESTREL_PYTHON:-}" ]]; then
    candidates+=("${KESTREL_PYTHON}")
  fi
  candidates+=(
    "/opt/homebrew/bin/python3.11"
    "python3.11"
    "/usr/bin/python3.11"
    "/usr/local/bin/python3.11"
    "python3.12"
    "python3.13"
    "python3"
  )

  local candidate
  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 && python_is_311_or_newer "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  die "Python 3.11+ is required. Set KESTREL_PYTHON to a Python 3.11+ interpreter."
}

print_runtime_defaults() {
  cat <<'EOF'
[kestrel-install] Safe runtime defaults:
  NEST_AGENT_BACKEND=memvid
  NEST_AGENT_PROVIDER=mock
  NEST_AGENT_MODEL=mock
  NEST_AGENT_ALLOW_SHELL=false
  NEST_AGENT_ALLOW_FILE_WRITE=false
  NEST_AGENT_ALLOW_POLICY_WRITES=false
  NEST_AGENT_ALLOW_CODEX_CLI=false
  NEST_AGENT_ALLOW_PLUGIN_INSTALL=false
  NEST_AGENT_ALLOW_GIT_COMMIT=false
  NEST_AGENT_ALLOW_GIT_PUSH=false
  NEST_AGENT_ALLOW_REMOTE_MUTATION=false
EOF
}

print_install_plan() {
  cat <<EOF
[kestrel-install] Install plan:
  repo: ${KESTREL_REPO}
  ref: ${KESTREL_REF}
  home: ${KESTREL_HOME}
  python: ${PYTHON_BIN}
  extras: .[${KESTREL_EXTRAS}]
  memory: .nest/memory
  web build: ${WEB_BUILD_LABEL}
  smoke checks: ${SMOKE_LABEL}
  dry run: ${DRY_RUN_LABEL}
  server auto-start: ${SERVER_LABEL}
  browser open: ${BROWSER_LABEL}
  server session: ${KESTREL_SERVER_SESSION}
  server log: ${KESTREL_SERVER_LOG}
  server pid: ${KESTREL_SERVER_PID}
  health check: ${HEALTH_CHECK_LABEL}
  web UI: ${WEB_UI_LABEL}
  init command: nest-agent init --backend memvid --memory-dir .nest/memory
  verify command: nest-agent memory verify --backend memvid --memory-dir .nest/memory
  smoke command: nest-agent chat --backend memory --memory-dir .nest/install-smoke-memory --provider mock --model mock --message "hello from one-shot install"
  launch command: ${SERVER_COMMAND_LABEL}
EOF
}

ensure_git_target() {
  if [[ -e "$KESTREL_HOME" && ! -d "$KESTREL_HOME" ]]; then
    die "Install target exists and is not a directory: ${KESTREL_HOME}"
  fi
  if is_nonempty_dir "$KESTREL_HOME" && [[ ! -d "${KESTREL_HOME}/.git" ]]; then
    die "Refusing to install into non-git nonempty directory: ${KESTREL_HOME}"
  fi

  if [[ -d "${KESTREL_HOME}/.git" ]]; then
    log "Updating existing Kestrel checkout at ${KESTREL_HOME}"
    if ! is_true "${KESTREL_DRY_RUN:-}"; then
      if git -C "$KESTREL_HOME" remote get-url origin >/dev/null 2>&1; then
        run git -C "$KESTREL_HOME" remote set-url origin "$KESTREL_REPO"
      else
        run git -C "$KESTREL_HOME" remote add origin "$KESTREL_REPO"
      fi
    else
      run git -C "$KESTREL_HOME" remote set-url origin "$KESTREL_REPO"
    fi
  else
    run mkdir -p "$(dirname "$KESTREL_HOME")"
    run git clone "$KESTREL_REPO" "$KESTREL_HOME"
  fi

  run git -C "$KESTREL_HOME" fetch origin "$KESTREL_REF"
  run git -C "$KESTREL_HOME" checkout -f FETCH_HEAD
}

install_python_deps() {
  run "$PYTHON_BIN" -m venv .venv
  run .venv/bin/python -m pip install --upgrade pip
  run .venv/bin/python -m pip install -e ".[${KESTREL_EXTRAS}]"
}

install_web_deps() {
  if is_true "${KESTREL_SKIP_WEB:-}"; then
    log "Skipping web install/build because KESTREL_SKIP_WEB is set."
    return 0
  fi
  command -v npm >/dev/null 2>&1 || die "npm is required for the web workbench. Set KESTREL_SKIP_WEB=1 to skip."
  run npm ci --prefix web
  run npm run build --prefix web
}

initialize_memory() {
  run .venv/bin/nest-agent init --backend memvid --memory-dir .nest/memory
  run .venv/bin/nest-agent memory verify --backend memvid --memory-dir .nest/memory
}

run_smoke_checks() {
  if is_true "${KESTREL_SKIP_SMOKE:-}"; then
    log "Skipping smoke checks because KESTREL_SKIP_SMOKE is set."
    return 0
  fi
  run .venv/bin/nest-agent doctor --backend memvid --memory-dir .nest/memory --provider mock --model mock --timeout-seconds 300
  run .venv/bin/nest-agent chat --backend memory --memory-dir .nest/install-smoke-memory --provider mock --model mock --message "hello from one-shot install"
}

server_url() {
  printf 'http://127.0.0.1:%s/' "$KESTREL_PORT"
}

health_url() {
  printf 'http://127.0.0.1:%s/api/health' "$KESTREL_PORT"
}

server_is_healthy() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsS --max-time 2 "$(health_url)" >/dev/null 2>&1
}

wait_for_server() {
  if is_true "${KESTREL_DRY_RUN:-}"; then
    return 0
  fi

  local attempt
  for attempt in $(seq 1 60); do
    if server_is_healthy; then
      log "Kestrel server is healthy at $(server_url)"
      return 0
    fi
    sleep 1
  done

  die "Kestrel server did not become healthy at $(health_url). Check ${KESTREL_SERVER_LOG}."
}

stop_port_listener() {
  command -v lsof >/dev/null 2>&1 || return 0

  local pids
  pids="$(lsof -tiTCP:"$KESTREL_PORT" -sTCP:LISTEN -n -P 2>/dev/null || true)"
  local pid
  for pid in $pids; do
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1; then
      log "Stopping existing Kestrel listener ${pid} on port ${KESTREL_PORT}"
      kill "$pid" >/dev/null 2>&1 || true
      sleep 1
      if kill -0 "$pid" >/dev/null 2>&1; then
        kill -9 "$pid" >/dev/null 2>&1 || true
      fi
    fi
  done
}

stop_existing_detached_server() {
  if is_true "${KESTREL_DRY_RUN:-}"; then
    return 0
  fi

  if [[ -f "$KESTREL_SERVER_PID" ]]; then
    local old_pid
    old_pid="$(tr -d '[:space:]' <"$KESTREL_SERVER_PID")"
    if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" >/dev/null 2>&1; then
      log "Stopping previous Kestrel server process ${old_pid}"
      kill "$old_pid" >/dev/null 2>&1 || true
      sleep 1
      if kill -0 "$old_pid" >/dev/null 2>&1; then
        kill -9 "$old_pid" >/dev/null 2>&1 || true
      fi
    fi
  fi

  screen -S "$KESTREL_SERVER_SESSION" -X quit >/dev/null 2>&1 || true
  tmux kill-session -t "$KESTREL_SERVER_SESSION" >/dev/null 2>&1 || true
  if server_is_healthy; then
    stop_port_listener
  fi
}

start_server_detached() {
  if ! is_true "$KESTREL_START_SERVER"; then
    log "Skipping server launch because KESTREL_START_SERVER is disabled."
    return 0
  fi

  run mkdir -p "$(dirname "$KESTREL_SERVER_LOG")" "$(dirname "$KESTREL_SERVER_PID")"
  stop_existing_detached_server
  if ! is_true "${KESTREL_DRY_RUN:-}" && server_is_healthy; then
    log "Kestrel server is already healthy at $(server_url)"
    return 0
  fi

  local server_cmd=(
    .venv/bin/nest-agent
    server
    --backend
    memvid
    --memory-dir
    .nest/memory
    --provider
    mock
    --model
    mock
    --host
    127.0.0.1
    --port
    "$KESTREL_PORT"
  )

  log "Starting Kestrel server at $(server_url)"
  log "Server log: ${KESTREL_SERVER_LOG}"

  if command -v screen >/dev/null 2>&1; then
    run screen -dmS "$KESTREL_SERVER_SESSION" bash -lc 'cd "$1" || exit 1; log_file="$2"; pid_file="$3"; shift 3; "$@" >>"$log_file" 2>&1 & child=$!; printf "%s\n" "$child" >"$pid_file"; wait "$child"' bash "$KESTREL_HOME" "$KESTREL_SERVER_LOG" "$KESTREL_SERVER_PID" "${server_cmd[@]}"
  elif command -v tmux >/dev/null 2>&1; then
    run tmux new-session -d -s "$KESTREL_SERVER_SESSION" "cd $(printf '%q' "$KESTREL_HOME") && $(quote_cmd "${server_cmd[@]}") >>$(printf '%q' "$KESTREL_SERVER_LOG") 2>&1 & child=\$!; printf '%s\\n' \"\$child\" >$(printf '%q' "$KESTREL_SERVER_PID"); wait \"\$child\""
  else
    log "screen/tmux not found; falling back to nohup background launch."
    log "+ nohup $(quote_cmd "${server_cmd[@]}") >>$(printf '%q' "$KESTREL_SERVER_LOG") 2>&1 &"
    if ! is_true "${KESTREL_DRY_RUN:-}"; then
      nohup "${server_cmd[@]}" >>"$KESTREL_SERVER_LOG" 2>&1 &
      printf '%s\n' "$!" > "$KESTREL_SERVER_PID"
    fi
  fi

  wait_for_server
}

open_web_ui() {
  if ! is_true "$KESTREL_START_SERVER" || ! is_true "$KESTREL_OPEN_BROWSER"; then
    return 0
  fi

  local url
  url="$(server_url)"
  log "Opening Kestrel web UI at ${url}"
  if command -v open >/dev/null 2>&1; then
    run open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    run xdg-open "$url"
  else
    log "No browser opener found. Open ${url} in your browser."
  fi
}

print_next_steps() {
  if is_true "$KESTREL_START_SERVER"; then
    cat <<EOF

Kestrel install complete.

The local web workbench is running at:
  $(server_url)

Server log:
  ${KESTREL_SERVER_LOG}

Stop the detached server:
  kill "\$(cat "${KESTREL_SERVER_PID}")"
  screen -S "${KESTREL_SERVER_SESSION}" -X quit 2>/dev/null || true

Try CLI chat:
  cd "${KESTREL_HOME}"
  .venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider mock --model mock
EOF
    return 0
  fi

  cat <<EOF

Kestrel install complete.

Try CLI chat:
  cd "${KESTREL_HOME}"
  .venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider mock --model mock

Start the local web workbench:
  cd "${KESTREL_HOME}"
  .venv/bin/nest-agent server --backend memvid --memory-dir .nest/memory --provider mock --model mock --host 127.0.0.1 --port ${KESTREL_PORT}

Open:
  http://127.0.0.1:${KESTREL_PORT}/
EOF
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    return 0
  fi

  KESTREL_HOME="${KESTREL_HOME:-$DEFAULT_HOME}"
  KESTREL_REPO="${KESTREL_REPO:-$DEFAULT_REPO}"
  KESTREL_REF="${KESTREL_REF:-main}"
  KESTREL_EXTRAS="${KESTREL_EXTRAS:-$DEFAULT_EXTRAS}"
  KESTREL_PORT="${KESTREL_PORT:-$DEFAULT_PORT}"
  KESTREL_START_SERVER="${KESTREL_START_SERVER:-$DEFAULT_START_SERVER}"
  if is_true "$KESTREL_START_SERVER"; then
    KESTREL_OPEN_BROWSER="${KESTREL_OPEN_BROWSER:-$DEFAULT_OPEN_BROWSER}"
  else
    KESTREL_OPEN_BROWSER="${KESTREL_OPEN_BROWSER:-0}"
  fi
  if is_false "$KESTREL_START_SERVER"; then
    KESTREL_START_SERVER="0"
  fi
  if is_false "$KESTREL_OPEN_BROWSER"; then
    KESTREL_OPEN_BROWSER="0"
  fi
  KESTREL_SERVER_SESSION="${KESTREL_SERVER_SESSION:-$DEFAULT_SERVER_SESSION}"
  KESTREL_SERVER_LOG="${KESTREL_SERVER_LOG:-${KESTREL_HOME}/.nest/server.log}"
  KESTREL_SERVER_PID="${KESTREL_SERVER_PID:-${KESTREL_HOME}/.nest/server.pid}"
  PYTHON_BIN="$(detect_python)"
  readonly KESTREL_HOME KESTREL_REPO KESTREL_REF KESTREL_EXTRAS KESTREL_PORT KESTREL_START_SERVER KESTREL_OPEN_BROWSER KESTREL_SERVER_SESSION KESTREL_SERVER_LOG KESTREL_SERVER_PID PYTHON_BIN

  if is_true "${KESTREL_DRY_RUN:-}"; then
    DRY_RUN_LABEL="yes"
    log "DRY RUN: commands will be printed but not executed."
  else
    DRY_RUN_LABEL="no"
  fi
  if is_true "${KESTREL_SKIP_WEB:-}"; then
    WEB_BUILD_LABEL="skipped"
  else
    WEB_BUILD_LABEL="npm ci --prefix web && npm run build --prefix web"
  fi
  if is_true "${KESTREL_SKIP_SMOKE:-}"; then
    SMOKE_LABEL="skipped"
  else
    SMOKE_LABEL="doctor + mock chat"
  fi
  if is_true "$KESTREL_START_SERVER"; then
    SERVER_LABEL="enabled"
    HEALTH_CHECK_LABEL="$(health_url)"
    WEB_UI_LABEL="$(server_url)"
    SERVER_COMMAND_LABEL="nest-agent server --backend memvid --memory-dir .nest/memory --provider mock --model mock --host 127.0.0.1 --port ${KESTREL_PORT}"
    if is_true "$KESTREL_OPEN_BROWSER"; then
      BROWSER_LABEL="enabled"
    else
      BROWSER_LABEL="disabled"
    fi
  else
    SERVER_LABEL="disabled"
    BROWSER_LABEL="disabled"
    HEALTH_CHECK_LABEL="skipped"
    WEB_UI_LABEL="skipped"
    SERVER_COMMAND_LABEL="skipped"
  fi
  readonly DRY_RUN_LABEL WEB_BUILD_LABEL SMOKE_LABEL SERVER_LABEL BROWSER_LABEL HEALTH_CHECK_LABEL WEB_UI_LABEL SERVER_COMMAND_LABEL

  print_runtime_defaults
  print_install_plan
  ensure_git_target

  if ! is_true "${KESTREL_DRY_RUN:-}"; then
    cd "$KESTREL_HOME"
  fi

  install_python_deps
  install_web_deps
  initialize_memory
  run_smoke_checks
  start_server_detached
  open_web_ui
  print_next_steps
}

main "$@"
