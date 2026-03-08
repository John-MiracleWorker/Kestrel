#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROFILE_PATH="${KESTREL_PROFILE:-$ROOT_DIR/config/startup/native-hybrid.env}"
CMD="${1:-check}"

if [[ ! -f "$PROFILE_PATH" ]]; then
  echo "[ERROR] Profile not found: $PROFILE_PATH"
  echo "Copy config/startup/native-hybrid.env.example to config/startup/native-hybrid.env first."
  exit 1
fi

# shellcheck disable=SC1090
source "$PROFILE_PATH"

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[ERROR] Missing dependency: $1"
    return 1
  fi
}

check_versions() {
  local py_major node_major
  py_major="$(python3 -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo 0)"
  node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"

  if [[ "$py_major" -lt "${KESTREL_REQUIRED_PYTHON_MAJOR:-3}" ]]; then
    echo "[ERROR] Python major version is too old: $py_major"
    return 1
  fi

  if [[ "$node_major" -lt "${KESTREL_REQUIRED_NODE_MAJOR:-18}" ]]; then
    echo "[ERROR] Node major version is too old: $node_major"
    return 1
  fi

  return 0
}

check_policy() {
  if is_true "${KESTREL_ENABLE_HOST_WRITE:-false}" || is_true "${KESTREL_ENABLE_HOST_EXEC:-false}"; then
    if [[ -z "${KESTREL_NATIVE_POLICY_FILE:-}" ]]; then
      echo "[ERROR] KESTREL_NATIVE_POLICY_FILE must be set when host write/exec is enabled."
      return 1
    fi
    if [[ ! -f "$KESTREL_NATIVE_POLICY_FILE" ]]; then
      echo "[ERROR] Native policy file missing: $KESTREL_NATIVE_POLICY_FILE"
      return 1
    fi
  fi
  return 0
}

check_permissions() {
  if [[ ! -w "$ROOT_DIR" ]]; then
    echo "[ERROR] Repo root is not writable: $ROOT_DIR"
    return 1
  fi

  if is_true "${KESTREL_ENABLE_SCREEN_AGENT:-true}"; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
      echo "[INFO] macOS detected: ensure Screen Recording + Accessibility are granted to your terminal for screen-agent."
    else
      echo "[INFO] Non-macOS host: ensure desktop session allows screenshot/input automation for screen-agent."
    fi
  fi
  return 0
}

run_checks() {
  require_cmd python3
  require_cmd node
  require_cmd npm
  check_versions
  check_policy
  check_permissions

  if is_true "${KESTREL_ENABLE_DOCKER_SUBSYSTEMS:-true}"; then
    require_cmd docker
    echo "[INFO] Docker-enabled hybrid mode for: ${KESTREL_DOCKER_SUBSYSTEMS:-postgres,redis,hands}"
  else
    echo "[INFO] Pure native mode selected (Docker subsystems disabled)."
  fi

  echo "[OK] Startup checks passed for profile: $PROFILE_PATH"
}

start_native_services() {
  mkdir -p "$ROOT_DIR/.kestrel/logs"

  if is_true "${KESTREL_ENABLE_SCREEN_AGENT:-true}"; then
    (cd "$ROOT_DIR" && nohup bash -lc "$KESTREL_SCREEN_AGENT_CMD" > .kestrel/logs/screen-agent.log 2>&1 &)
    echo "[STARTED] screen-agent (log: .kestrel/logs/screen-agent.log)"
  fi

  if is_true "${KESTREL_ENABLE_DOCKER_SUBSYSTEMS:-true}"; then
    local services
    services="${KESTREL_DOCKER_SUBSYSTEMS:-postgres,redis,hands}"
    (cd "$ROOT_DIR" && docker compose up -d $(echo "$services" | tr ',' ' '))
    echo "[STARTED] docker subsystems: $services"
  fi

  (cd "$ROOT_DIR" && nohup bash -lc "$KESTREL_BRAIN_CMD" > .kestrel/logs/brain.log 2>&1 &)
  echo "[STARTED] brain (log: .kestrel/logs/brain.log)"

  (cd "$ROOT_DIR" && nohup bash -lc "$KESTREL_GATEWAY_CMD" > .kestrel/logs/gateway.log 2>&1 &)
  echo "[STARTED] gateway (log: .kestrel/logs/gateway.log)"

  (cd "$ROOT_DIR" && nohup bash -lc "$KESTREL_WEB_CMD" > .kestrel/logs/web.log 2>&1 &)
  echo "[STARTED] web (log: .kestrel/logs/web.log)"
}

case "$CMD" in
  check)
    run_checks
    ;;
  up)
    run_checks
    start_native_services
    ;;
  *)
    echo "Usage: $0 [check|up]"
    exit 1
    ;;
esac
