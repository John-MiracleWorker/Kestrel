#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${KESTREL_LAUNCHD_LABEL:-ai.kestrel.daemon}"
PLIST="${KESTREL_LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
LOG_DIR="${KESTREL_LOG_DIR:-$HOME/.kestrel/logs}"
UID_VALUE="$(id -u)"

if [[ "${1:-}" == "--check" ]]; then
  [[ -f "$PLIST" ]] || { echo "Missing launchd plist: $PLIST" >&2; exit 1; }
  [[ "$(stat -f '%Lp' "$PLIST")" == "600" ]] || {
    echo "Launchd plist must be mode 600: $PLIST" >&2
    exit 1
  }
  plutil -lint "$PLIST" >/dev/null
  if plutil -p "$PLIST" | grep -q EnvironmentVariables; then
    echo "Launchd plist must not embed EnvironmentVariables" >&2
    exit 1
  fi
  launchctl print "gui/$UID_VALUE/$LABEL" >/dev/null
  printf 'Launchd service %s is loaded and credential-free\n' "$LABEL"
  exit 0
fi
if [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--check]" >&2
  exit 2
fi

mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
chmod 700 "$(dirname "$LOG_DIR")" "$LOG_DIR"

PROJECT_ROOT="$ROOT" LAUNCHD_LABEL="$LABEL" LAUNCHD_PLIST="$PLIST" LAUNCHD_LOG_DIR="$LOG_DIR" python3 - <<'PY'
import os
import plistlib
from pathlib import Path

root = Path(os.environ["PROJECT_ROOT"]).resolve()
label = os.environ["LAUNCHD_LABEL"]
plist_path = Path(os.environ["LAUNCHD_PLIST"]).expanduser()
log_dir = Path(os.environ["LAUNCHD_LOG_DIR"]).expanduser()
start_script = root / "scripts" / "start-telegram-stack.sh"
if not start_script.is_file():
    raise SystemExit(f"missing startup script: {start_script}")
payload = {
    "Label": label,
    "ProgramArguments": [str(start_script)],
    "WorkingDirectory": str(root),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Interactive",
    "StandardOutPath": str(log_dir / "launchd.stdout.log"),
    "StandardErrorPath": str(log_dir / "launchd.stderr.log"),
}
temporary = plist_path.with_suffix(plist_path.suffix + ".tmp")
with temporary.open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=True)
os.chmod(temporary, 0o600)
os.replace(temporary, plist_path)
os.chmod(plist_path, 0o600)
PY

plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$UID_VALUE/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_VALUE" "$PLIST"
launchctl enable "gui/$UID_VALUE/$LABEL"
launchctl kickstart -k "gui/$UID_VALUE/$LABEL"

printf 'Installed %s at %s (mode 600, credential-free)\n' "$LABEL" "$PLIST"
