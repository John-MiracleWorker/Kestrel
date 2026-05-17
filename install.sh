#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_REPO="https://github.com/John-MiracleWorker/Kestrel.git"
DEFAULT_HOME="${HOME}/.kestrel-agent"
DEFAULT_EXTRAS="memvid,openai,server,mcp,dev"
DEFAULT_PORT="8765"

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
  KESTREL_START_SERVER  Set to 1/true to start the local server after install.
  KESTREL_PORT          Server port when KESTREL_START_SERVER=1. Defaults to 8765.
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
  init command: nest-agent init --backend memvid --memory-dir .nest/memory
  verify command: nest-agent memory verify --backend memvid --memory-dir .nest/memory
  smoke command: nest-agent chat --backend memory --memory-dir .nest/install-smoke-memory --provider mock --model mock --message "hello from one-shot install"
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

print_next_steps() {
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
  PYTHON_BIN="$(detect_python)"
  readonly KESTREL_HOME KESTREL_REPO KESTREL_REF KESTREL_EXTRAS KESTREL_PORT PYTHON_BIN

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
  readonly DRY_RUN_LABEL WEB_BUILD_LABEL SMOKE_LABEL

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
  print_next_steps

  if is_true "${KESTREL_START_SERVER:-}"; then
    log "Starting Kestrel server on http://127.0.0.1:${KESTREL_PORT}/"
    run .venv/bin/nest-agent server --backend memvid --memory-dir .nest/memory --provider mock --model mock --host 127.0.0.1 --port "$KESTREL_PORT"
  fi
}

main "$@"
