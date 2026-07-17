#!/usr/bin/env bash
set -euo pipefail

PACKAGE_SPEC=""
VENV="${KESTREL_VENV:-.venv}"
SERVICE_LABEL="${KESTREL_SERVICE_LABEL:-ai.kestrel.daemon}"
HEALTH_URL="${KESTREL_HEALTH_URL:-http://127.0.0.1:8765/api/health/ready}"
APPLY=0

usage() {
  cat <<'EOF'
Usage: scripts/upgrade-kestrel.sh --package-spec SPEC [--venv PATH] [--service-label LABEL] [--apply]

Dry-run is the default. --apply stops the launchd service, snapshots SQLite,
installs SPEC, runs readiness checks, restarts the service, and rolls the Python
package back to the prior version automatically if validation fails.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --package-spec) PACKAGE_SPEC="$2"; shift 2 ;;
    --venv) VENV="$2"; shift 2 ;;
    --service-label) SERVICE_LABEL="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$PACKAGE_SPEC" ]] || { echo "--package-spec is required" >&2; exit 2; }
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
AGENT="$VENV/bin/nest-agent"
[[ -x "$PYTHON" && -x "$PIP" && -x "$AGENT" ]] || {
  echo "Kestrel virtualenv is incomplete: $VENV" >&2
  exit 1
}

CURRENT_VERSION="$($PYTHON -c 'import importlib.metadata as m; print(m.version("nested-memvid-agent"))')"
ROLLBACK_SPEC="nested-memvid-agent[server,memvid,mcp]==${CURRENT_VERSION}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STATE_PATH="${NEST_AGENT_STATE_PATH:-.nest/state/agent.db}"
BACKUP_ROOT="${KESTREL_UPGRADE_BACKUP_ROOT:-.nest/backups/upgrades}"
STATE_BACKUP="$BACKUP_ROOT/$TIMESTAMP/agent.db"
MEMORY_BACKUP_ROOT="$BACKUP_ROOT/$TIMESTAMP/memory"
MEMORY_BACKUP_ID=""
DOMAIN="gui/$UID"
PLIST="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"

printf 'Current version: %s\nTarget spec: %s\nState backup: %s\nService: %s/%s\n' \
  "$CURRENT_VERSION" "$PACKAGE_SPEC" "$STATE_BACKUP" "$DOMAIN" "$SERVICE_LABEL"
if [[ "$APPLY" -ne 1 ]]; then
  echo "Dry run only; rerun with --apply to execute."
  exit 0
fi

launchctl bootout "$DOMAIN/$SERVICE_LABEL" >/dev/null 2>&1 || true

mkdir -p "$(dirname "$STATE_BACKUP")"
chmod 700 "$BACKUP_ROOT" "$(dirname "$STATE_BACKUP")"
if [[ -f "$STATE_PATH" ]]; then
  "$PYTHON" - "$STATE_PATH" "$STATE_BACKUP" <<'PY'
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
    result = target.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise SystemExit(f"state backup integrity failure: {result}")
finally:
    target.close()
    source.close()
PY
  chmod 600 "$STATE_BACKUP"
fi

RUNTIME_SETTINGS="$(dirname "$(dirname "$STATE_PATH")")/config/runtime_settings.json"
if [[ -f "$RUNTIME_SETTINGS" ]]; then
  read -r ACTIVE_BACKEND ACTIVE_MEMORY_DIR < <(
    "$PYTHON" - "$RUNTIME_SETTINGS" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get("backend", "memory"), data.get("memory_dir", ".nest/memory"))
PY
  )
  if [[ "$ACTIVE_BACKEND" == "memvid" && -d "$ACTIVE_MEMORY_DIR" ]]; then
    MEMORY_BACKUP_JSON="$($AGENT memory backup --backend memvid --memory-dir "$ACTIVE_MEMORY_DIR" --state-path "$STATE_PATH" --backup-dir "$MEMORY_BACKUP_ROOT")"
    MEMORY_BACKUP_ID="$($PYTHON -c 'import json,sys; print(json.load(sys.stdin)["backup_id"])' <<<"$MEMORY_BACKUP_JSON")"
  fi
fi

rollback() {
  echo "Upgrade validation failed; rolling package back to $CURRENT_VERSION" >&2
  local restore_failed=0
  if [[ -n "$MEMORY_BACKUP_ID" ]]; then
    if ! "$AGENT" memory restore "$MEMORY_BACKUP_ID" --backend memvid --memory-dir "$ACTIVE_MEMORY_DIR" --state-path "$STATE_PATH" --backup-dir "$MEMORY_BACKUP_ROOT" --yes; then
      echo "Memvid rollback restore failed for backup $MEMORY_BACKUP_ID" >&2
      restore_failed=1
    fi
  fi
  "$PIP" install --upgrade "$ROLLBACK_SPEC"
  if [[ -f "$STATE_BACKUP" ]]; then
    "$PYTHON" - "$STATE_BACKUP" "$STATE_PATH" <<'PY'
import os, shutil, sys
source, target = sys.argv[1:]
temporary = target + ".rollback.tmp"
shutil.copy2(source, temporary)
os.chmod(temporary, 0o600)
os.replace(temporary, target)
PY
  fi
  if [[ -f "$PLIST" ]]; then
    launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    launchctl kickstart -k "$DOMAIN/$SERVICE_LABEL" >/dev/null 2>&1 || true
  fi
  if [[ "$restore_failed" -ne 0 ]]; then
    return 1
  fi
}
trap rollback ERR

server_is_ready() {
  "$PYTHON" - "$HEALTH_URL" <<'PY'
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

"$PIP" install --upgrade "$PACKAGE_SPEC"
"$PIP" check
"$AGENT" product setup --json --check >/dev/null
if [[ -f "$PLIST" ]]; then
  launchctl bootstrap "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
  launchctl kickstart -k "$DOMAIN/$SERVICE_LABEL"
fi

for _ in $(seq 1 30); do
  if server_is_ready >/dev/null 2>&1; then
    trap - ERR
    echo "Upgrade succeeded and readiness is healthy."
    exit 0
  fi
  sleep 1
done
false
