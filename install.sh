#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_REPO="https://github.com/John-MiracleWorker/Kestrel.git"
DEFAULT_HOME="${HOME}/.kestrel-agent"
DEFAULT_EXTRAS="memvid,openai,anthropic,gemini,server,mcp,keyring"
DEFAULT_REQUIREMENTS_URL=""
DEFAULT_WHEEL_URL=""
DEFAULT_CHECKSUMS_URL=""
DEFAULT_RELEASE_SHA=""
DEFAULT_RELEASE_VERSION=""
DEFAULT_PORT="8765"
DEFAULT_START_SERVER="0"
DEFAULT_OPEN_BROWSER="0"
DEFAULT_SERVER_SESSION="kestrel-agent"

RELEASE_TRANSACTION_ACTIVE=0
RELEASE_VENV_BACKED_UP=0
RELEASE_VENV_NEW_STARTED=0
RELEASE_EXISTING_CHECKOUT=0
RELEASE_ORIGINAL_HEAD=""
RELEASE_ORIGINAL_BRANCH=""
RELEASE_VENV_PATH=""
RELEASE_VENV_PREVIOUS_PATH=""
RELEASE_WEB_DIST_BACKED_UP=0
RELEASE_WEB_DIST_NEW_STARTED=0
RELEASE_WEB_DIST_PATH=""
RELEASE_WEB_DIST_PREVIOUS_PATH=""
RELEASE_MEMORY_BACKED_UP=0
RELEASE_MEMORY_NEW_STARTED=0
RELEASE_MEMORY_PATH=""
RELEASE_MEMORY_PREVIOUS_PATH=""
RELEASE_STATE_BACKED_UP=0
RELEASE_STATE_NEW_STARTED=0
RELEASE_STATE_PATH=""
RELEASE_STATE_PREVIOUS_PATH=""
INSTALL_WEB_STAGED_DIR=""
INSTALL_MEMORY_STAGED_DIR=""
INSTALL_STATE_STAGED_ROOT=""
INSTALL_STATE_STAGED_PATH=""
INSTALL_CANARY_ROOT=""
INSTALL_CANARY_MEMORY_DIR=""
INSTALL_CANARY_STATE_PATH=""
MAINTENANCE_LOCK_ACQUIRED=0
MAINTENANCE_LOCK_PID=""
MAINTENANCE_LOCK_CONTROL_ROOT=""
MAINTENANCE_LOCK_RELEASED_FOR_SERVER=0
STARTED_SERVER_PID=""
STARTED_SUPERVISOR_PID=""
STARTED_SERVER_PGID=""
SERVER_LAUNCH_ATTEMPTED=0

usage() {
  cat <<'EOF'
Kestrel one-shot installer

Install from GitHub:
  curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/main/install.sh | bash

Environment options:
  KESTREL_HOME          Install directory. Defaults to $HOME/.kestrel-agent.
  KESTREL_REPO          Development/source-mode repository URL or local path. Staged release installers reject overrides.
                        Defaults to https://github.com/John-MiracleWorker/Kestrel.git.
  KESTREL_REF           Development/source-mode Git ref. Staged release installers reject overrides. Defaults to main.
  KESTREL_PYTHON        Python 3.11, 3.12, or 3.13 interpreter path/name to use.
  KESTREL_EXTRAS        Python extras to install. Defaults to memvid,openai,anthropic,gemini,server,mcp,keyring.
  NEST_AGENT_STATE_PATH Runtime state database. Relative paths are anchored at KESTREL_HOME.
                        Defaults to .nest/state/agent.db. Safe absolute paths are supported.
  KESTREL_REQUIREMENTS_URL  Internal release-asset URL; caller overrides are rejected.
  KESTREL_WHEEL_URL     Internal release-asset URL; caller overrides are rejected.
  KESTREL_CHECKSUMS_URL Internal release-asset URL; caller overrides are rejected.
  KESTREL_SKIP_WEB      Set to 1/true to skip npm ci and web build.
  KESTREL_SKIP_SMOKE    Set to 1/true to skip doctor/chat smoke checks.
  KESTREL_START_SERVER  Set to 1/true to launch the local server and web UI. Defaults to 0.
  KESTREL_OPEN_BROWSER  Set to 1/true to open the web UI in your browser (when server launch is enabled). Defaults to 0.
  KESTREL_SERVER_SESSION Detached screen/tmux session name. Defaults to kestrel-agent.
  KESTREL_SERVER_LOG    Server log path. Defaults to $KESTREL_HOME/.nest/server.log.
  KESTREL_SERVER_PID    Server PID file path. Defaults to $KESTREL_HOME/.nest/server.pid.
  KESTREL_SERVER_SUPERVISOR_PID Private supervisor PID file. Defaults to $KESTREL_HOME/.nest/server.supervisor.pid.
  KESTREL_SERVER_PROCESS_GROUP Private server process-group file. Defaults to $KESTREL_HOME/.nest/server.pgid.
  KESTREL_PORT          Server port. Defaults to 8765.
  KESTREL_DRY_RUN       Set to 1/true to print commands without mutating the system.

Safe default runtime:
  backend=memvid, provider=mock, model=mock, high-risk tool flags disabled.

Supported installer platforms:
  macOS (Intel or Apple silicon) and Linux x86_64, including x86_64 Linux inside WSL.
  Native Windows is unsupported. Linux ARM64 users should use the release container image.
EOF
}

log() {
  printf '[kestrel-install] %s\n' "$*"
}

die() {
  printf '[kestrel-install] ERROR: %s\n' "$*" >&2
  exit 1
}

release_install_exit_trap() {
  local status="$1"
  trap - EXIT INT TERM
  if [[ "$RELEASE_TRANSACTION_ACTIVE" -ne 1 ]]; then
    cleanup_install_canary
    cleanup_staged_web_assets
    release_maintenance_lock
    exit "$status"
  fi

  if [[ "$SERVER_LAUNCH_ATTEMPTED" -eq 1 ]]; then
    set +e
    if ! cleanup_failed_server_launch; then
      log "ERROR: the candidate server could not be proven stopped. Automatic filesystem and state rollback is unsafe; recovery material has been preserved for manual recovery."
      cleanup_install_canary
      cleanup_staged_state
      cleanup_staged_web_assets
      release_maintenance_lock
      exit "$status"
    fi
    set -e
  fi

  # Candidate startup requires releasing the same runtime and Memvid locks that
  # protect the transactional state and memory swap.  Once the candidate is
  # proven absent, reacquire both exact locks before restoring anything.  A
  # contender may legitimately have acquired ownership in the handoff window;
  # in that case fail closed and preserve every recovery artifact for an
  # operator instead of swapping files underneath a live runtime.
  if [[ "$MAINTENANCE_LOCK_RELEASED_FOR_SERVER" -eq 1 && "$MAINTENANCE_LOCK_ACQUIRED" -ne 1 ]]; then
    set +e
    acquire_maintenance_lock rollback
    local lock_status=$?
    set -e
    if [[ "$lock_status" -ne 0 ]]; then
      log "ERROR: exclusive runtime and Memvid maintenance ownership could not be reacquired for rollback. Automatic filesystem and state restore is unsafe; recovery material has been preserved for manual recovery."
      cleanup_install_canary
      cleanup_staged_state
      cleanup_staged_web_assets
      release_maintenance_lock
      exit "$status"
    fi
    MAINTENANCE_LOCK_RELEASED_FOR_SERVER=0
  fi

  log "Kestrel install failed before acceptance; restoring the previous checkout and Python environment, plus runtime state."
  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "DRY RUN: rollback would restore the previous checkout and .venv."
    exit "$status"
  fi

  set +e
  restore_previous_state
  if [[ "$RELEASE_VENV_BACKED_UP" -eq 1 ]] &&
    [[ -e "$RELEASE_VENV_PREVIOUS_PATH" || -L "$RELEASE_VENV_PREVIOUS_PATH" ]]; then
    rm -rf -- "$RELEASE_VENV_PATH"
    if ! mv -- "$RELEASE_VENV_PREVIOUS_PATH" "$RELEASE_VENV_PATH"; then
      log "WARNING: automatic .venv restore failed; recovery copy remains at ${RELEASE_VENV_PREVIOUS_PATH}."
    else
      log "Restored the previous Python environment."
    fi
  elif [[ "$RELEASE_VENV_NEW_STARTED" -eq 1 ]]; then
    rm -rf -- "$RELEASE_VENV_PATH"
  elif [[ "$RELEASE_VENV_BACKED_UP" -eq 1 && ! -e "$RELEASE_VENV_PATH" && ! -L "$RELEASE_VENV_PATH" ]]; then
    log "WARNING: prior .venv backup was not found; inspect ${KESTREL_HOME} before retrying."
  fi
  if [[ "$RELEASE_WEB_DIST_BACKED_UP" -eq 1 ]] &&
    [[ -e "$RELEASE_WEB_DIST_PREVIOUS_PATH" || -L "$RELEASE_WEB_DIST_PREVIOUS_PATH" ]]; then
    rm -rf -- "$RELEASE_WEB_DIST_PATH"
    if ! mv -- "$RELEASE_WEB_DIST_PREVIOUS_PATH" "$RELEASE_WEB_DIST_PATH"; then
      log "WARNING: automatic web/dist restore failed; recovery copy remains at ${RELEASE_WEB_DIST_PREVIOUS_PATH}."
    else
      log "Restored the previous web workbench assets."
    fi
  elif [[ "$RELEASE_WEB_DIST_NEW_STARTED" -eq 1 ]]; then
    rm -rf -- "$RELEASE_WEB_DIST_PATH"
  fi
  if [[ "$RELEASE_MEMORY_BACKED_UP" -eq 1 ]] &&
    [[ -e "$RELEASE_MEMORY_PREVIOUS_PATH" || -L "$RELEASE_MEMORY_PREVIOUS_PATH" ]]; then
    rm -rf -- "$RELEASE_MEMORY_PATH"
    if ! mv -- "$RELEASE_MEMORY_PREVIOUS_PATH" "$RELEASE_MEMORY_PATH"; then
      log "WARNING: automatic memory restore failed; recovery copy remains at ${RELEASE_MEMORY_PREVIOUS_PATH}."
    else
      log "Restored the previous memory directory."
    fi
  elif [[ "$RELEASE_MEMORY_NEW_STARTED" -eq 1 ]]; then
    rm -rf -- "$RELEASE_MEMORY_PATH"
  fi
  if [[ "$RELEASE_EXISTING_CHECKOUT" -eq 1 && -n "$RELEASE_ORIGINAL_HEAD" ]]; then
    local restore_ref="$RELEASE_ORIGINAL_HEAD"
    local checkout_args=(--detach --no-overwrite-ignore "$RELEASE_ORIGINAL_HEAD")
    if [[ -n "$RELEASE_ORIGINAL_BRANCH" ]]; then
      restore_ref="$RELEASE_ORIGINAL_BRANCH"
      checkout_args=(--no-overwrite-ignore "$RELEASE_ORIGINAL_BRANCH")
    fi
    if ! git -C "$KESTREL_HOME" checkout "${checkout_args[@]}" >/dev/null 2>&1; then
      log "WARNING: automatic checkout restore failed; restore commit ${RELEASE_ORIGINAL_HEAD} manually."
    else
      log "Restored checkout ${restore_ref}."
    fi
  fi
  cleanup_install_canary
  cleanup_staged_state
  cleanup_staged_web_assets
  release_maintenance_lock
  exit "$status"
}

start_release_install_transaction() {
  RELEASE_TRANSACTION_ACTIVE=1
  RELEASE_VENV_PATH="${KESTREL_HOME}/.venv"
  RELEASE_VENV_PREVIOUS_PATH="${KESTREL_HOME}/.venv.release-previous"
  RELEASE_WEB_DIST_PATH="${KESTREL_HOME}/web/dist"
  RELEASE_WEB_DIST_PREVIOUS_PATH="${KESTREL_HOME}/web/.dist.release-previous"
  RELEASE_MEMORY_PATH="${KESTREL_HOME}/.nest/memory"
  RELEASE_MEMORY_PREVIOUS_PATH="${KESTREL_HOME}/.nest/.memory.release-previous"
  RELEASE_STATE_PATH="$KESTREL_STATE_PATH"
  RELEASE_STATE_PREVIOUS_PATH="${KESTREL_STATE_PATH}.release-previous"
  trap 'release_install_exit_trap "$?"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

prepare_release_venv_replacement() {
  if [[ -e "$RELEASE_VENV_PREVIOUS_PATH" || -L "$RELEASE_VENV_PREVIOUS_PATH" ]]; then
    die "A prior release recovery environment already exists: ${RELEASE_VENV_PREVIOUS_PATH}. Restore or remove it before retrying."
  fi
  if [[ -e "$RELEASE_VENV_PATH" || -L "$RELEASE_VENV_PATH" ]]; then
    RELEASE_VENV_BACKED_UP=1
    if ! run mv -- "$RELEASE_VENV_PATH" "$RELEASE_VENV_PREVIOUS_PATH"; then
      RELEASE_VENV_BACKED_UP=0
      die "Unable to preserve the existing Python environment for rollback."
    fi
  fi
  RELEASE_VENV_NEW_STARTED=1
}

finalize_release_install_transaction() {
  if [[ "$RELEASE_TRANSACTION_ACTIVE" -ne 1 ]]; then
    return 0
  fi
  RELEASE_TRANSACTION_ACTIVE=0
  if [[ "$RELEASE_STATE_BACKED_UP" -eq 1 ]]; then
    if ! remove_previous_state_recovery; then
      log "WARNING: unable to remove prior state recovery material: ${RELEASE_STATE_PREVIOUS_PATH}"
    fi
  fi
  if [[ "$RELEASE_VENV_BACKED_UP" -eq 1 ]]; then
    if ! run rm -rf -- "$RELEASE_VENV_PREVIOUS_PATH"; then
      log "WARNING: unable to remove the prior .venv backup: ${RELEASE_VENV_PREVIOUS_PATH}"
    fi
  fi
  if [[ "$RELEASE_WEB_DIST_BACKED_UP" -eq 1 ]]; then
    if ! run rm -rf -- "$RELEASE_WEB_DIST_PREVIOUS_PATH"; then
      log "WARNING: unable to remove prior web asset backup: ${RELEASE_WEB_DIST_PREVIOUS_PATH}"
    fi
  fi
  if [[ "$RELEASE_MEMORY_BACKED_UP" -eq 1 ]]; then
    if ! run rm -rf -- "$RELEASE_MEMORY_PREVIOUS_PATH"; then
      log "WARNING: unable to remove prior memory backup: ${RELEASE_MEMORY_PREVIOUS_PATH}"
    fi
  fi
  RELEASE_STATE_BACKED_UP=0
  RELEASE_STATE_NEW_STARTED=0
}

finish_post_commit_maintenance() {
  cleanup_install_canary
  cleanup_staged_state
  cleanup_staged_web_assets
  release_maintenance_lock
  trap - EXIT INT TERM
}

require_supported_platform() {
  local platform architecture
  platform="$(uname -s 2>/dev/null || true)"
  case "$platform" in
    Darwin) return 0 ;;
    Linux)
      architecture="$(uname -m 2>/dev/null || true)"
      case "$architecture" in
        x86_64 | amd64) return 0 ;;
        aarch64 | arm64)
          die "The one-shot installer does not support Linux ARM64. Use the published linux/arm64 container image instead."
          ;;
        *)
          die "The one-shot installer supports Linux x86_64 only (detected ${architecture:-unknown}). Use a published container image for other Linux architectures."
          ;;
      esac
      ;;
    *)
      die "install.sh supports macOS and Linux x86_64 (including Linux inside WSL); native Windows is unsupported. Open an x86_64 WSL distro or use the published container image."
      ;;
  esac
}

validate_release_artifact_config() {
  local configured=0
  [[ -n "$KESTREL_REQUIREMENTS_URL" ]] && configured=$((configured + 1))
  [[ -n "$KESTREL_WHEEL_URL" ]] && configured=$((configured + 1))
  [[ -n "$KESTREL_CHECKSUMS_URL" ]] && configured=$((configured + 1))

  if [[ "$configured" -ne 0 && "$configured" -ne 3 ]]; then
    die "KESTREL_REQUIREMENTS_URL, KESTREL_WHEEL_URL, and KESTREL_CHECKSUMS_URL must be set together."
  fi
  if [[ "$configured" -eq 0 ]]; then
    return 0
  fi
  [[ "$DEFAULT_RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] ||
    die "Release-artifact mode requires an installer-embedded immutable Git commit SHA. Download install.sh from the matching GitHub release."
  [[ "$DEFAULT_RELEASE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([a-zA-Z0-9.+-]*)?$ ]] ||
    die "Release-artifact mode requires an installer-embedded release version. Download install.sh from the matching GitHub release."
  [[ "$KESTREL_REPO_OVERRIDE_SET" -eq 0 ]] ||
    die "KESTREL_REPO cannot override the source repository in a staged release installer."
  [[ "$KESTREL_REF_OVERRIDE_SET" -eq 0 ]] ||
    die "KESTREL_REF cannot override the immutable source revision in a staged release installer."
  [[ "$KESTREL_REQUIREMENTS_URL_OVERRIDE_SET" -eq 0 ]] ||
    die "KESTREL_REQUIREMENTS_URL cannot override the verified payload in a staged release installer."
  [[ "$KESTREL_WHEEL_URL_OVERRIDE_SET" -eq 0 ]] ||
    die "KESTREL_WHEEL_URL cannot override the verified payload in a staged release installer."
  [[ "$KESTREL_CHECKSUMS_URL_OVERRIDE_SET" -eq 0 ]] ||
    die "KESTREL_CHECKSUMS_URL cannot override the verified payload in a staged release installer."
  [[ "$KESTREL_EXTRAS" == "$DEFAULT_EXTRAS" ]] ||
    die "The verified release artifacts support the default extras exactly: ${DEFAULT_EXTRAS}."

  local url
  for url in "$KESTREL_REQUIREMENTS_URL" "$KESTREL_WHEEL_URL" "$KESTREL_CHECKSUMS_URL"; do
    [[ "$url" == https://* ]] || die "Release artifact URLs must use HTTPS: ${url}"
  done
  command -v curl >/dev/null 2>&1 || die "curl is required to download verified release artifacts."

  local wheel_name="${KESTREL_WHEEL_URL##*/}"
  [[ "$wheel_name" == nested_memvid_agent-*.whl ]] ||
    die "Unexpected Kestrel wheel filename: ${wheel_name}"
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

absolute_path() {
  "$PYTHON_BIN" -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$1"
}

resolve_runtime_state_path() {
  "$PYTHON_BIN" - "$KESTREL_HOME" "${NEST_AGENT_STATE_PATH-.nest/state/agent.db}" <<'PY'
import os
import sys
from pathlib import Path

home = Path(sys.argv[1])
configured = Path(sys.argv[2])
candidate = configured if configured.is_absolute() else home / configured
print(os.path.abspath(candidate))
PY
}

require_safe_install_paths() {
  "$PYTHON_BIN" - \
    "$KESTREL_HOME" \
    "$KESTREL_STATE_PATH" \
    "$KESTREL_SERVER_PID" \
    "$KESTREL_SERVER_SUPERVISOR_PID" \
    "$KESTREL_SERVER_PROCESS_GROUP" \
    "$KESTREL_SERVER_LOG" <<'PY'
import os
import stat
import sys
from pathlib import Path

expected_uid = os.geteuid() if hasattr(os, "geteuid") else None
home = Path(sys.argv[1])
state_path = Path(sys.argv[2])
control_paths = [Path(value) for value in sys.argv[3:]]


def existing_chain(path: Path, label: str) -> os.stat_result | None:
    if not path.is_absolute():
        raise SystemExit(f"{label} must be an absolute path: {path}")
    current = Path(path.anchor)
    target_metadata = None
    missing = False
    for component in path.parts[1:]:
        current /= component
        if missing:
            continue
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            missing = True
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise SystemExit(f"refusing symbolic-link ancestor for {label}: {current}")
        if current != path and not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"refusing non-directory ancestor for {label}: {current}")
        target_metadata = metadata
    return None if missing else target_metadata


def owner_directory(path: Path, label: str) -> None:
    metadata = existing_chain(path, label)
    if metadata is None:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f"refusing non-directory {label}: {path}")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise SystemExit(f"refusing {label} owned by uid {metadata.st_uid}: {path}")


owner_directory(home, "Kestrel install directory")
for relative, label in (
    (Path(".nest"), "Kestrel runtime directory"),
    (Path(".nest/state"), "Kestrel state directory"),
    (Path(".nest/memory"), "Kestrel memory directory"),
    (Path(".nest/config"), "Kestrel config directory"),
):
    owner_directory(home / relative, label)

if not state_path.is_absolute():
    raise SystemExit(f"Kestrel state path must be absolute after resolution: {state_path}")
if state_path == home:
    raise SystemExit(f"refusing Kestrel install directory as the state file: {state_path}")
for protected in (
    home / ".git",
    home / ".venv",
    home / "web" / "dist",
    home / ".nest" / "memory",
):
    if state_path == protected or protected in state_path.parents:
        raise SystemExit(
            f"refusing Kestrel state path inside a transactionally replaced directory: {state_path}"
        )

owner_directory(state_path.parent, f"state-file parent for {state_path}")
state_metadata = existing_chain(state_path, f"state file {state_path}")
if state_metadata is not None:
    if not stat.S_ISREG(state_metadata.st_mode) or stat.S_ISLNK(state_metadata.st_mode):
        raise SystemExit(f"refusing non-regular Kestrel state file: {state_path}")
    if state_metadata.st_nlink != 1:
        raise SystemExit(f"refusing hard-linked Kestrel state file: {state_path}")
    if expected_uid is not None and state_metadata.st_uid != expected_uid:
        raise SystemExit(f"refusing Kestrel state file owned by uid {state_metadata.st_uid}: {state_path}")

for suffix in ("-wal", "-shm", "-journal"):
    sidecar = Path(f"{state_path}{suffix}")
    metadata = existing_chain(sidecar, f"SQLite sidecar {sidecar}")
    if metadata is None:
        continue
    if state_metadata is None:
        raise SystemExit(f"refusing orphaned SQLite sidecar without a state database: {sidecar}")
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SystemExit(f"refusing non-regular SQLite sidecar: {sidecar}")
    if metadata.st_nlink != 1:
        raise SystemExit(f"refusing hard-linked SQLite sidecar: {sidecar}")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise SystemExit(f"refusing SQLite sidecar owned by uid {metadata.st_uid}: {sidecar}")

recovery = Path(f"{state_path}.release-previous")
recovery_metadata = existing_chain(recovery, f"state recovery path {recovery}")
if recovery_metadata is not None:
    raise SystemExit(
        f"prior state recovery material already exists: {recovery}. Restore or remove it before retrying."
    )

for path in control_paths:
    owner_directory(path.parent, f"control-file parent for {path}")
    metadata = existing_chain(path, f"control file {path}")
    if metadata is None:
        continue
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"refusing non-regular control file: {path}")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise SystemExit(f"refusing control file owned by uid {metadata.st_uid}: {path}")
PY
}

acquire_maintenance_lock() {
  local failure_mode="${1:-fatal}"
  [[ "$MAINTENANCE_LOCK_ACQUIRED" -eq 0 ]] || return 0
  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "+ acquire exclusive runtime and Memvid memory maintenance locks under ${KESTREL_HOME}/.nest"
    return 0
  fi

  local state_parent state_name runtime_lock_path
  state_parent="$(dirname "$KESTREL_STATE_PATH")"
  state_name="$(basename "$KESTREL_STATE_PATH")"
  runtime_lock_path="${state_parent}/.${state_name}.kestrel-runtime-owner.lock"
  local memory_lock_path="${KESTREL_HOME}/.nest/.memory.kestrel-memory.lock"
  MAINTENANCE_LOCK_CONTROL_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/kestrel-install-lock.XXXXXX")" ||
    die "Unable to create private maintenance-lock control directory."
  chmod 700 "$MAINTENANCE_LOCK_CONTROL_ROOT"
  local control_fifo="${MAINTENANCE_LOCK_CONTROL_ROOT}/control.fifo"
  local ready_file="${MAINTENANCE_LOCK_CONTROL_ROOT}/ready"
  mkfifo -m 600 "$control_fifo"
  exec 9<>"$control_fifo"

  "$PYTHON_BIN" - \
    "$runtime_lock_path" \
    "$memory_lock_path" \
    "$control_fifo" \
    "$ready_file" 9>&- <<'PY' &
import fcntl
import os
import stat
import sys
from pathlib import Path

runtime_lock_path = Path(sys.argv[1])
memory_lock_path = Path(sys.argv[2])
control_fifo = Path(sys.argv[3])
ready_file = Path(sys.argv[4])


def validate_directory_chain(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"unsafe {label} directory: {current}")


def open_private_lock(path: Path, label: str) -> int:
    validate_directory_chain(path.parent, label)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = os.lstat(path.parent)
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise SystemExit(f"unsafe {label} lock directory")
    if hasattr(os, "geteuid") and parent.st_uid != os.geteuid():
        raise SystemExit(f"{label} lock directory has the wrong owner")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    before = os.fstat(descriptor)
    after = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        os.close(descriptor)
        raise SystemExit(f"unsafe {label} lock file")
    if not os.path.samestat(before, after):
        os.close(descriptor)
        raise SystemExit(f"{label} lock changed during validation")
    if hasattr(os, "geteuid") and before.st_uid != os.geteuid():
        os.close(descriptor)
        raise SystemExit(f"{label} lock has the wrong owner")
    os.fchmod(descriptor, 0o600)
    return descriptor


runtime_descriptor = open_private_lock(runtime_lock_path, "runtime ownership")
memory_descriptor = None
try:
    fcntl.flock(runtime_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    memory_descriptor = open_private_lock(memory_lock_path, "Memvid memory")
    fcntl.flock(memory_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ready_descriptor = os.open(
        ready_file,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(ready_descriptor)
    with control_fifo.open("rb", buffering=0) as control:
        while control.read(4096):
            pass
finally:
    if memory_descriptor is not None:
        os.close(memory_descriptor)
    os.close(runtime_descriptor)
PY
  MAINTENANCE_LOCK_PID="$!"

  local _
  for _ in {1..100}; do
    if [[ -f "$ready_file" ]]; then
      MAINTENANCE_LOCK_ACQUIRED=1
      log "Acquired exclusive Kestrel runtime maintenance ownership."
      return 0
    fi
    if ! kill -0 "$MAINTENANCE_LOCK_PID" >/dev/null 2>&1; then
      exec 9>&-
      wait "$MAINTENANCE_LOCK_PID" >/dev/null 2>&1 || true
      MAINTENANCE_LOCK_PID=""
      rm -rf -- "$MAINTENANCE_LOCK_CONTROL_ROOT"
      MAINTENANCE_LOCK_CONTROL_ROOT=""
      if [[ "$failure_mode" == "rollback" ]]; then
        printf '%s\n' \
          "[kestrel-install] ERROR: unable to reacquire exclusive Kestrel maintenance ownership for rollback because another runtime or direct Memvid command owns it." >&2
        return 1
      fi
      die "Unable to acquire exclusive Kestrel maintenance ownership. Stop every Kestrel runtime and direct Memvid command using ${KESTREL_HOME}/.nest, then re-run."
    fi
    sleep 0.05
  done
  exec 9>&-
  if [[ -n "$MAINTENANCE_LOCK_PID" ]]; then
    wait "$MAINTENANCE_LOCK_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$MAINTENANCE_LOCK_CONTROL_ROOT" ]]; then
    rm -rf -- "$MAINTENANCE_LOCK_CONTROL_ROOT"
  fi
  MAINTENANCE_LOCK_PID=""
  MAINTENANCE_LOCK_CONTROL_ROOT=""
  if [[ "$failure_mode" == "rollback" ]]; then
    printf '%s\n' \
      "[kestrel-install] ERROR: timed out reacquiring exclusive Kestrel maintenance ownership for rollback." >&2
    return 1
  fi
  die "Timed out acquiring exclusive Kestrel runtime maintenance ownership."
}

release_maintenance_lock() {
  [[ "$MAINTENANCE_LOCK_ACQUIRED" -eq 1 || -n "$MAINTENANCE_LOCK_PID" ]] || return 0
  exec 9>&-
  if [[ -n "$MAINTENANCE_LOCK_PID" ]]; then
    wait "$MAINTENANCE_LOCK_PID" >/dev/null 2>&1 ||
      log "WARNING: maintenance-lock helper exited unexpectedly."
  fi
  if [[ -n "$MAINTENANCE_LOCK_CONTROL_ROOT" ]]; then
    rm -rf -- "$MAINTENANCE_LOCK_CONTROL_ROOT"
  fi
  MAINTENANCE_LOCK_ACQUIRED=0
  MAINTENANCE_LOCK_PID=""
  MAINTENANCE_LOCK_CONTROL_ROOT=""
  log "Released Kestrel runtime and memory maintenance ownership."
}

is_nonempty_dir() {
  local dir="$1"
  [[ -d "$dir" ]] || return 1
  [[ -n "$(find "$dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]
}

is_kestrel_checkout() {
  local target="$1"
  [[ -f "${target}/pyproject.toml" ]] || return 1
  grep -Eq '^[[:space:]]*name[[:space:]]*=[[:space:]]*"nested-memvid-agent"[[:space:]]*$' \
    "${target}/pyproject.toml"
}

require_clean_kestrel_checkout() {
  local target="$1"
  local status
  is_kestrel_checkout "$target" ||
    die "Refusing to update an unrecognized git checkout: ${target}"
  if ! status="$(git -C "$target" status --porcelain --untracked-files=all)"; then
    die "Unable to inspect existing Kestrel checkout: ${target}"
  fi
  [[ -z "$status" ]] ||
    die "Refusing to update a dirty Kestrel checkout: ${target}. Commit, stash, or remove local changes first."
}

python_is_supported() {
  local candidate="$1"
  "$candidate" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else 1)' >/dev/null 2>&1
}

detect_python() {
  local candidates=()
  if [[ -n "${KESTREL_PYTHON:-}" ]]; then
    if command -v "$KESTREL_PYTHON" >/dev/null 2>&1 && python_is_supported "$KESTREL_PYTHON"; then
      printf '%s\n' "$KESTREL_PYTHON"
      return 0
    fi
    die "KESTREL_PYTHON must name a Python 3.11, 3.12, or 3.13 interpreter. Python 3.14 and newer are not supported."
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
    if command -v "$candidate" >/dev/null 2>&1 && python_is_supported "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  die "Python 3.11, 3.12, or 3.13 is required. Set KESTREL_PYTHON to a supported interpreter; Python 3.14 and newer are not supported."
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
  immutable release commit: ${RELEASE_SHA_LABEL}
  home: ${KESTREL_HOME}
  python: ${PYTHON_BIN}
  extras: .[${KESTREL_EXTRAS}]
  Python package: ${PYTHON_PACKAGE_LABEL}
  locked requirements: ${REQUIREMENTS_LABEL}
  memory: .nest/memory
  state: ${KESTREL_STATE_PATH}
  web build: ${WEB_BUILD_LABEL}
  smoke checks: ${SMOKE_LABEL}
  dry run: ${DRY_RUN_LABEL}
  server auto-start: ${SERVER_LABEL}
  browser open: ${BROWSER_LABEL}
  server session: ${KESTREL_SERVER_SESSION}
  server log: ${KESTREL_SERVER_LOG}
  server pid: ${KESTREL_SERVER_PID}
  supervisor pid: ${KESTREL_SERVER_SUPERVISOR_PID}
  server process group: ${KESTREL_SERVER_PROCESS_GROUP}
  health check: ${HEALTH_CHECK_LABEL}
  web UI: ${WEB_UI_LABEL}
  init command: nest-agent init --backend memvid --memory-dir .nest/memory
  verify command: nest-agent memory verify --backend memvid --memory-dir .nest/memory
  smoke command: nest-agent chat --backend memory --memory-dir .nest/install-smoke-memory --provider mock --model mock --message "hello from one-shot install"
  launch command: ${SERVER_COMMAND_LABEL}
EOF
}

ensure_git_target() {
  if [[ -L "$KESTREL_HOME" ]]; then
    die "Refusing symbolic-link install target: ${KESTREL_HOME}"
  fi
  if [[ -e "$KESTREL_HOME" && ! -d "$KESTREL_HOME" ]]; then
    die "Install target exists and is not a directory: ${KESTREL_HOME}"
  fi
  if is_nonempty_dir "$KESTREL_HOME" && [[ ! -d "${KESTREL_HOME}/.git" ]]; then
    die "Refusing to install into non-git nonempty directory: ${KESTREL_HOME}"
  fi

  if [[ -d "${KESTREL_HOME}/.git" ]]; then
    log "Updating existing Kestrel checkout at ${KESTREL_HOME}"
    require_clean_kestrel_checkout "$KESTREL_HOME"
    RELEASE_EXISTING_CHECKOUT=1
    if ! RELEASE_ORIGINAL_HEAD="$(git -C "$KESTREL_HOME" rev-parse --verify HEAD)"; then
      die "Unable to record the existing Kestrel checkout for rollback: ${KESTREL_HOME}"
    fi
    RELEASE_ORIGINAL_BRANCH="$(git -C "$KESTREL_HOME" symbolic-ref --quiet --short HEAD || true)"
    # Fetch the requested source directly so an operator-owned `origin` remote
    # is never rewritten. A second cleanliness check closes the fetch/checkout
    # gap.
    run git -C "$KESTREL_HOME" fetch "$KESTREL_REPO" "$KESTREL_REF"
    if ! is_true "${KESTREL_DRY_RUN:-}"; then
      require_clean_kestrel_checkout "$KESTREL_HOME"
    fi
    run git -C "$KESTREL_HOME" checkout --detach --no-overwrite-ignore FETCH_HEAD
    verify_release_checkout_identity "$KESTREL_HOME"
    return 0
  fi

  stage_fresh_checkout
}

verify_release_checkout_identity() {
  local checkout="$1"
  [[ -n "$DEFAULT_RELEASE_SHA" ]] || return 0
  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "+ verify checkout HEAD equals immutable release commit ${DEFAULT_RELEASE_SHA}"
    return 0
  fi

  local checkout_head
  if ! checkout_head="$(git -C "$checkout" rev-parse --verify 'HEAD^{commit}')"; then
    die "Unable to resolve the fetched release checkout commit."
  fi
  [[ "$checkout_head" == "$DEFAULT_RELEASE_SHA" ]] ||
    die "Fetched release ref resolved to ${checkout_head}, but this installer is bound to ${DEFAULT_RELEASE_SHA}. Refusing a moved tag or mismatched repository."
  log "Verified immutable release checkout ${DEFAULT_RELEASE_SHA}."
}

stage_fresh_checkout() (
  local parent base staging
  parent="$(dirname "$KESTREL_HOME")"
  base="$(basename "$KESTREL_HOME")"
  run mkdir -p "$parent"

  if is_true "${KESTREL_DRY_RUN:-}"; then
    staging="${KESTREL_HOME}.install-staging"
  else
    staging="$(mktemp -d "${parent}/.${base}.install.XXXXXX")" ||
      die "Unable to create a private staged checkout next to ${KESTREL_HOME}."
    trap '
      if [[ -n "${staging:-}" && ( -e "$staging" || -L "$staging" ) ]]; then
        rm -rf -- "$staging"
      fi
    ' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
  fi

  # Build a shallow promisor checkout away from the final path. A failed fetch
  # or checkout therefore cannot strand a misleading `.git` directory that
  # makes every retry look like an unrecognized operator checkout.
  run git init -q "$staging"
  run git -C "$staging" remote add origin "$KESTREL_REPO"
  run git -C "$staging" config remote.origin.promisor true
  run git -C "$staging" config remote.origin.partialclonefilter blob:none
  run git -C "$staging" fetch --depth 1 --filter=blob:none --no-tags origin "$KESTREL_REF"
  run git -C "$staging" checkout --detach --no-overwrite-ignore FETCH_HEAD
  verify_release_checkout_identity "$staging"

  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "+ atomically promote staged checkout ${staging} to ${KESTREL_HOME}"
    return 0
  fi
  is_kestrel_checkout "$staging" ||
    die "Fetched ref is not a recognized Kestrel checkout: ${KESTREL_REF}"
  log "+ atomically promote staged checkout ${staging} to ${KESTREL_HOME}"
  "$PYTHON_BIN" - "$staging" "$KESTREL_HOME" <<'PY'
import os
import stat
import sys

source, target = sys.argv[1:]
try:
    target_stat = os.lstat(target)
except FileNotFoundError:
    pass
else:
    if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
        raise SystemExit(f"install target changed type before commit: {target}")
    with os.scandir(target) as entries:
        if next(entries, None) is not None:
            raise SystemExit(f"install target became nonempty before commit: {target}")
os.rename(source, target)
PY
  staging=""
  trap - EXIT INT TERM
)

verify_release_artifact() {
  local manifest="$1"
  local artifact="$2"
  local filename expected actual
  filename="$(basename "$artifact")"
  expected="$(awk -v name="$filename" '$2 == name {print $1}' "$manifest")"
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] ||
    die "Missing or invalid checksum for ${filename} in ${manifest}"
  if ! actual="$("$PYTHON_BIN" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$artifact")"; then
    die "Unable to hash release artifact: ${artifact}"
  fi
  [[ "$actual" == "$expected" ]] || die "Checksum mismatch for release artifact: ${filename}"
}

install_python_deps() {
  if [[ -z "$KESTREL_WHEEL_URL" ]]; then
    # Source upgrades are transactional too: preserve the verified-offline
    # environment before replacement so setup/smoke failures can roll back.
    prepare_release_venv_replacement
    run "$PYTHON_BIN" -m venv .venv
    run .venv/bin/python -m pip install --require-hashes --only-binary=:all: \
      -r config/python-build-bootstrap.txt
    run .venv/bin/python -m pip install --no-build-isolation -e ".[${KESTREL_EXTRAS}]"
    run .venv/bin/python -m pip check
    return 0
  fi

  local release_dir=".nest/release"
  local requirements_path="${release_dir}/requirements-release.txt"
  local checksums_path="${release_dir}/SHA256SUMS"
  local wheel_name="${KESTREL_WHEEL_URL##*/}"
  local wheel_path="${release_dir}/${wheel_name}"
  run mkdir -p "$release_dir"
  run curl --fail --silent --show-error --location --retry 3 \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    "$KESTREL_REQUIREMENTS_URL" --output "$requirements_path"
  run curl --fail --silent --show-error --location --retry 3 \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    "$KESTREL_WHEEL_URL" --output "$wheel_path"
  run curl --fail --silent --show-error --location --retry 3 \
    --proto '=https' --proto-redir '=https' --tlsv1.2 \
    "$KESTREL_CHECKSUMS_URL" --output "$checksums_path"

  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "+ verify SHA256SUMS for ${requirements_path} and ${wheel_path}"
  else
    verify_release_artifact "$checksums_path" "$requirements_path"
    verify_release_artifact "$checksums_path" "$wheel_path"
  fi
  # Preserve the working environment until every remote artifact has passed
  # verification, then keep it as a rollback copy until all install smoke
  # checks complete.
  prepare_release_venv_replacement
  run "$PYTHON_BIN" -m venv .venv
  run .venv/bin/python -m pip install --require-hashes --only-binary=:all: \
    -r "$requirements_path"
  run .venv/bin/python -m pip install --no-deps "${wheel_path}[${KESTREL_EXTRAS}]"
  run .venv/bin/python -m pip check
}

install_web_deps() {
  if [[ -n "$KESTREL_WHEEL_URL" ]]; then
    log "Using the workbench bundled in the verified release wheel."
    return 0
  fi
  if is_true "${KESTREL_SKIP_WEB:-}"; then
    log "Skipping web install/build because KESTREL_SKIP_WEB is set."
    return 0
  fi
  command -v npm >/dev/null 2>&1 || die "npm is required for the web workbench. Set KESTREL_SKIP_WEB=1 to skip."
  run npm ci --prefix web
  if is_true "${KESTREL_DRY_RUN:-}"; then
    INSTALL_WEB_STAGED_DIR="${KESTREL_HOME}/web/.dist.install-staging"
  else
    INSTALL_WEB_STAGED_DIR="$(mktemp -d "${KESTREL_HOME}/web/.dist.install.XXXXXX")" ||
      die "Unable to create private staged web output directory."
  fi
  run npm run build --prefix web -- --outDir "$INSTALL_WEB_STAGED_DIR"
}

cleanup_staged_web_assets() {
  if [[ -n "$INSTALL_WEB_STAGED_DIR" ]] &&
    [[ -e "$INSTALL_WEB_STAGED_DIR" || -L "$INSTALL_WEB_STAGED_DIR" ]]; then
    rm -rf -- "$INSTALL_WEB_STAGED_DIR"
  fi
  INSTALL_WEB_STAGED_DIR=""
}

commit_staged_web_assets() {
  [[ -n "$INSTALL_WEB_STAGED_DIR" ]] || return 0
  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "+ transactionally replace web/dist from ${INSTALL_WEB_STAGED_DIR}"
    INSTALL_WEB_STAGED_DIR=""
    return 0
  fi
  [[ -s "${INSTALL_WEB_STAGED_DIR}/index.html" ]] ||
    die "Staged web workbench build is missing index.html"
  [[ ! -e "$RELEASE_WEB_DIST_PREVIOUS_PATH" && ! -L "$RELEASE_WEB_DIST_PREVIOUS_PATH" ]] ||
    die "A prior web asset recovery directory already exists: ${RELEASE_WEB_DIST_PREVIOUS_PATH}"
  if [[ -e "$RELEASE_WEB_DIST_PATH" || -L "$RELEASE_WEB_DIST_PATH" ]]; then
    [[ -d "$RELEASE_WEB_DIST_PATH" && ! -L "$RELEASE_WEB_DIST_PATH" ]] ||
      die "Refusing to replace non-directory or symbolic-link web assets: ${RELEASE_WEB_DIST_PATH}"
    RELEASE_WEB_DIST_BACKED_UP=1
    run mv -- "$RELEASE_WEB_DIST_PATH" "$RELEASE_WEB_DIST_PREVIOUS_PATH"
  fi
  RELEASE_WEB_DIST_NEW_STARTED=1
  run mv -- "$INSTALL_WEB_STAGED_DIR" "$RELEASE_WEB_DIST_PATH"
  INSTALL_WEB_STAGED_DIR=""
}

verify_installed_runtime() {
  if ! is_true "${KESTREL_DRY_RUN:-}"; then
    [[ -f scripts/installer-server-supervisor.sh && ! -L scripts/installer-server-supervisor.sh ]] ||
      die "Installed checkout is missing the regular installer server supervisor script."
  fi
  run .venv/bin/nest-agent --help
  run .venv/bin/python -c 'import importlib.util, nested_memvid_agent; required=("fastapi","keyring","mcp","memvid_sdk","uvicorn"); missing=[name for name in required if importlib.util.find_spec(name) is None]; assert not missing, f"missing runtime modules: {missing}"; assert nested_memvid_agent.__file__'
  if [[ -n "$KESTREL_WHEEL_URL" ]]; then
    run .venv/bin/python -c 'import importlib.metadata, sys; from importlib.resources import files; expected=sys.argv[1]; actual=importlib.metadata.version("nested-memvid-agent"); assert actual == expected, f"installed Kestrel version {actual!r} != release {expected!r}"; assert files("nested_memvid_agent").joinpath("web_dist/index.html").is_file(), "bundled workbench is missing"; import anthropic, fastapi, google.genai, keyring, mcp, memvid_sdk, openai, uvicorn' "$DEFAULT_RELEASE_VERSION"
  elif ! is_true "${KESTREL_SKIP_WEB:-}" && ! is_true "${KESTREL_DRY_RUN:-}"; then
    [[ -n "$INSTALL_WEB_STAGED_DIR" && -s "${INSTALL_WEB_STAGED_DIR}/index.html" ]] ||
      die "Staged web workbench build is missing index.html"
  fi
}

initialize_memory() {
  run .venv/bin/nest-agent init --backend memvid --memory-dir .nest/memory
  run .venv/bin/nest-agent memory verify --backend memvid --memory-dir .nest/memory
}

cleanup_install_canary() {
  if [[ -n "$INSTALL_CANARY_ROOT" ]] &&
    [[ -e "$INSTALL_CANARY_ROOT" || -L "$INSTALL_CANARY_ROOT" ]]; then
    rm -rf -- "$INSTALL_CANARY_ROOT"
  fi
  INSTALL_CANARY_ROOT=""
  INSTALL_CANARY_MEMORY_DIR=""
  INSTALL_CANARY_STATE_PATH=""
  INSTALL_MEMORY_STAGED_DIR=""
}

stage_candidate_state_isolated() {
  local live_state="$KESTREL_STATE_PATH"
  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "+ create a read-only SQLite backup of ${live_state}, when present, at ${INSTALL_CANARY_STATE_PATH}"
    return 0
  fi

  "$PYTHON_BIN" - "$live_state" "$INSTALL_CANARY_STATE_PATH" <<'PY'
import os
import shutil
import sqlite3
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
expected_uid = os.geteuid() if hasattr(os, "geteuid") else None
sidecar_before: dict[Path, os.stat_result] = {}


def validate_directory_chain(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"refusing unsafe state path ancestor: {current}")


try:
    source_before = os.lstat(source)
except FileNotFoundError:
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    raise SystemExit(0)

validate_directory_chain(source.parent)
if stat.S_ISLNK(source_before.st_mode) or not stat.S_ISREG(source_before.st_mode):
    raise SystemExit(f"refusing unsafe live state database: {source}")
if source_before.st_nlink != 1:
    raise SystemExit(f"refusing hard-linked live state database: {source}")
if expected_uid is not None and source_before.st_uid != expected_uid:
    raise SystemExit(f"live state database has the wrong owner: {source}")

for suffix in ("-wal", "-shm", "-journal"):
    sidecar = Path(f"{source}{suffix}")
    try:
        metadata = os.lstat(sidecar)
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"refusing unsafe live SQLite sidecar: {sidecar}")
    if metadata.st_nlink != 1:
        raise SystemExit(f"refusing hard-linked live SQLite sidecar: {sidecar}")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise SystemExit(f"live SQLite sidecar has the wrong owner: {sidecar}")
    sidecar_before[sidecar] = metadata

destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
if destination.exists() or destination.is_symlink():
    raise SystemExit(f"candidate state destination already exists: {destination}")

snapshot_root = destination.parent / "source-snapshot"
snapshot_root.mkdir(mode=0o700)
artifacts = {source: source_before, **sidecar_before}
try:
    for artifact, before in artifacts.items():
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(artifact, flags)
        copied = snapshot_root / artifact.name
        destination_descriptor = os.open(
            copied,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            opened = os.fstat(source_descriptor)
            if not os.path.samestat(before, opened):
                raise SystemExit(f"live state artifact changed before snapshot: {artifact}")
            with (
                os.fdopen(source_descriptor, "rb", closefd=False) as source_handle,
                os.fdopen(destination_descriptor, "wb", closefd=False) as destination_handle,
            ):
                shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
                destination_handle.flush()
                os.fsync(destination_descriptor)
            after_copy = os.fstat(source_descriptor)
            if not os.path.samestat(before, after_copy) or (
                before.st_size,
                before.st_mtime_ns,
            ) != (after_copy.st_size, after_copy.st_mtime_ns):
                raise SystemExit(f"live state artifact changed during snapshot: {artifact}")
        finally:
            os.close(destination_descriptor)
            os.close(source_descriptor)

    snapshot_source = snapshot_root / source.name
    with sqlite3.connect(snapshot_source, timeout=2.0) as source_connection:
        with sqlite3.connect(destination, timeout=2.0) as destination_connection:
            source_connection.backup(destination_connection)
            result = destination_connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise SystemExit(f"candidate state backup failed SQLite integrity: {result!r}")
finally:
    for artifact in snapshot_root.iterdir():
        metadata = os.lstat(artifact)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(f"unsafe private state snapshot artifact: {artifact}")
        artifact.unlink()
    snapshot_root.rmdir()

source_after = os.lstat(source)
if not os.path.samestat(source_before, source_after):
    raise SystemExit("live state database changed identity during candidate backup")
if (source_before.st_size, source_before.st_mtime_ns) != (
    source_after.st_size,
    source_after.st_mtime_ns,
):
    raise SystemExit("live state database changed during candidate backup")
for sidecar, before in sidecar_before.items():
    try:
        after = os.lstat(sidecar)
    except FileNotFoundError:
        raise SystemExit(f"live SQLite sidecar disappeared during candidate backup: {sidecar}")
    if not os.path.samestat(before, after):
        raise SystemExit(f"live SQLite sidecar changed identity during candidate backup: {sidecar}")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SystemExit(f"live SQLite sidecar changed during candidate backup: {sidecar}")
os.chmod(destination, 0o600)
PY
}

validate_candidate_state_compatibility() {
  run .venv/bin/nest-agent routines list \
    --backend memory \
    --memory-dir "${INSTALL_CANARY_ROOT}/state-check-memory" \
    --state-path "$INSTALL_CANARY_STATE_PATH" \
    --provider mock \
    --model mock \
    --json
}

prepare_migrated_state_for_commit() {
  if is_true "${KESTREL_DRY_RUN:-}"; then
    INSTALL_STATE_STAGED_ROOT="$(dirname "$KESTREL_STATE_PATH")/.$(basename "$KESTREL_STATE_PATH").install-state.dry-run"
    INSTALL_STATE_STAGED_PATH="${INSTALL_STATE_STAGED_ROOT}/candidate.db"
    log "+ consolidate and verify migrated candidate state at ${INSTALL_STATE_STAGED_PATH}"
    return 0
  fi

  INSTALL_STATE_STAGED_ROOT="$("$PYTHON_BIN" - \
    "$INSTALL_CANARY_STATE_PATH" "$KESTREL_STATE_PATH" <<'PY'
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
expected_uid = os.geteuid() if hasattr(os, "geteuid") else None


def validate_directory_chain(path: Path, label: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"unsafe {label} directory: {current}")


def ensure_owned_directory(path: Path) -> None:
    if not path.is_absolute():
        raise SystemExit(f"state parent must be absolute: {path}")
    current = Path(path.anchor)
    missing = False
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            missing = True
            os.mkdir(current, 0o700)
            metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"unsafe state directory: {current}")
        if missing:
            os.chmod(current, 0o700)
    metadata = os.lstat(path)
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise SystemExit(f"state directory has the wrong owner: {path}")


validate_directory_chain(source.parent, "candidate state")
source_before = os.lstat(source)
if stat.S_ISLNK(source_before.st_mode) or not stat.S_ISREG(source_before.st_mode):
    raise SystemExit(f"refusing unsafe migrated candidate state: {source}")
if source_before.st_nlink != 1:
    raise SystemExit(f"refusing hard-linked migrated candidate state: {source}")
if expected_uid is not None and source_before.st_uid != expected_uid:
    raise SystemExit(f"migrated candidate state has the wrong owner: {source}")

sidecars: dict[Path, os.stat_result] = {}
for suffix in ("-wal", "-shm", "-journal"):
    sidecar = Path(f"{source}{suffix}")
    try:
        metadata = os.lstat(sidecar)
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"refusing unsafe migrated candidate sidecar: {sidecar}")
    if metadata.st_nlink != 1:
        raise SystemExit(f"refusing hard-linked migrated candidate sidecar: {sidecar}")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise SystemExit(f"migrated candidate sidecar has the wrong owner: {sidecar}")
    sidecars[sidecar] = metadata

ensure_owned_directory(target.parent)
staging_root = Path(
    tempfile.mkdtemp(prefix=f".{target.name}.install-state.", dir=target.parent)
)
os.chmod(staging_root, 0o700)
destination = staging_root / "candidate.db"
try:
    source_uri = f"{source.absolute().as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=2.0) as source_connection:
        source_connection.execute("PRAGMA query_only = ON")
        with sqlite3.connect(destination, timeout=2.0) as destination_connection:
            source_connection.backup(destination_connection)
            result = destination_connection.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise SystemExit(
                    f"migrated state failed SQLite integrity validation: {result!r}"
                )
    os.chmod(destination, 0o600)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    source_after = os.lstat(source)
    if not os.path.samestat(source_before, source_after):
        raise SystemExit("migrated candidate state changed identity during consolidation")
    if (source_before.st_size, source_before.st_mtime_ns) != (
        source_after.st_size,
        source_after.st_mtime_ns,
    ):
        raise SystemExit("migrated candidate state changed during consolidation")
    for sidecar, before in sidecars.items():
        try:
            after = os.lstat(sidecar)
        except FileNotFoundError:
            raise SystemExit(
                f"migrated candidate sidecar disappeared during consolidation: {sidecar}"
            )
        if not os.path.samestat(before, after):
            raise SystemExit(
                f"migrated candidate sidecar changed identity during consolidation: {sidecar}"
            )
    directory_descriptor = os.open(staging_root, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
except BaseException:
    try:
        for path in staging_root.iterdir():
            path.unlink()
        staging_root.rmdir()
    except OSError:
        pass
    raise

print(staging_root)
PY
  )" || die "Unable to prepare the migrated state database for transactional commit."
  INSTALL_STATE_STAGED_PATH="${INSTALL_STATE_STAGED_ROOT}/candidate.db"
  [[ -f "$INSTALL_STATE_STAGED_PATH" && ! -L "$INSTALL_STATE_STAGED_PATH" ]] ||
    die "Prepared state candidate is missing or unsafe: ${INSTALL_STATE_STAGED_PATH}"
}

cleanup_staged_state() {
  if [[ -z "$INSTALL_STATE_STAGED_ROOT" ]]; then
    INSTALL_STATE_STAGED_PATH=""
    return 0
  fi
  if [[ -e "$INSTALL_STATE_STAGED_ROOT" || -L "$INSTALL_STATE_STAGED_ROOT" ]]; then
    "$PYTHON_BIN" - "$INSTALL_STATE_STAGED_ROOT" "$KESTREL_STATE_PATH" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
target = Path(sys.argv[2])
expected_prefix = f".{target.name}.install-state."
if root.parent != target.parent or not root.name.startswith(expected_prefix):
    raise SystemExit(f"refusing unexpected staged-state cleanup path: {root}")
metadata = os.lstat(root)
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit(f"refusing unsafe staged-state cleanup path: {root}")
if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
    raise SystemExit(f"staged-state cleanup path has the wrong owner: {root}")
for entry in root.iterdir():
    entry_metadata = os.lstat(entry)
    if stat.S_ISLNK(entry_metadata.st_mode) or not stat.S_ISREG(entry_metadata.st_mode):
        raise SystemExit(f"refusing unexpected staged-state entry: {entry}")
    if entry.name not in {
        "candidate.db",
        "candidate.db-wal",
        "candidate.db-shm",
        "candidate.db-journal",
    }:
        raise SystemExit(f"refusing undeclared staged-state entry: {entry}")
    entry.unlink()
root.rmdir()
PY
  fi
  INSTALL_STATE_STAGED_ROOT=""
  INSTALL_STATE_STAGED_PATH=""
}

commit_staged_state() {
  [[ -n "$INSTALL_STATE_STAGED_PATH" ]] || die "Migrated state staging was not prepared."
  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "+ transactionally replace ${KESTREL_STATE_PATH} from ${INSTALL_STATE_STAGED_PATH}"
    INSTALL_STATE_STAGED_ROOT=""
    INSTALL_STATE_STAGED_PATH=""
    return 0
  fi

  [[ ! -e "$RELEASE_STATE_PREVIOUS_PATH" && ! -L "$RELEASE_STATE_PREVIOUS_PATH" ]] ||
    die "Prior state recovery material already exists: ${RELEASE_STATE_PREVIOUS_PATH}"
  if [[ -e "$KESTREL_STATE_PATH" || -L "$KESTREL_STATE_PATH" ]]; then
    RELEASE_STATE_BACKED_UP=1
  fi
  RELEASE_STATE_NEW_STARTED=1

  "$PYTHON_BIN" - \
    "$INSTALL_STATE_STAGED_PATH" "$KESTREL_STATE_PATH" "$RELEASE_STATE_PREVIOUS_PATH" <<'PY'
import os
import sqlite3
import stat
import sys
from pathlib import Path

candidate = Path(sys.argv[1])
target = Path(sys.argv[2])
recovery = Path(sys.argv[3])
expected_uid = os.geteuid() if hasattr(os, "geteuid") else None
suffixes = ("", "-wal", "-shm", "-journal")


def safe_regular(path: Path, label: str) -> os.stat_result:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"refusing unsafe {label}: {path}")
    if metadata.st_nlink != 1:
        raise SystemExit(f"refusing hard-linked {label}: {path}")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise SystemExit(f"{label} has the wrong owner: {path}")
    return metadata


parent_metadata = os.lstat(target.parent)
if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
    raise SystemExit(f"refusing unsafe live state parent: {target.parent}")
if expected_uid is not None and parent_metadata.st_uid != expected_uid:
    raise SystemExit(f"live state parent has the wrong owner: {target.parent}")
safe_regular(candidate, "staged state candidate")
with sqlite3.connect(f"{candidate.absolute().as_uri()}?mode=ro", uri=True) as connection:
    result = connection.execute("PRAGMA quick_check").fetchone()
    if result is None or result[0] != "ok":
        raise SystemExit(f"staged state candidate failed SQLite integrity: {result!r}")

try:
    safe_regular(target, "live state artifact")
except FileNotFoundError:
    live_database_exists = False
else:
    live_database_exists = True
sidecar_artifacts: list[Path] = []
for suffix in suffixes[1:]:
    path = Path(f"{target}{suffix}")
    try:
        safe_regular(path, "live state artifact")
    except FileNotFoundError:
        continue
    sidecar_artifacts.append(path)
if sidecar_artifacts and not live_database_exists:
    raise SystemExit("refusing orphaned SQLite sidecars without a live state database")
live_artifacts = sidecar_artifacts + ([target] if live_database_exists else [])

if live_artifacts:
    os.mkdir(recovery, 0o700)
    recovery_metadata = os.lstat(recovery)
    if stat.S_ISLNK(recovery_metadata.st_mode) or not stat.S_ISDIR(recovery_metadata.st_mode):
        raise SystemExit(f"unsafe state recovery directory: {recovery}")
    for path in live_artifacts:
        os.replace(path, recovery / path.name)

os.replace(candidate, target)
os.chmod(target, 0o600)
parent_descriptor = os.open(target.parent, os.O_RDONLY)
try:
    os.fsync(parent_descriptor)
finally:
    os.close(parent_descriptor)
if live_artifacts:
    recovery_descriptor = os.open(recovery, os.O_RDONLY)
    try:
        os.fsync(recovery_descriptor)
    finally:
        os.close(recovery_descriptor)
PY
  INSTALL_STATE_STAGED_PATH=""
  log "Committed migrated state database at ${KESTREL_STATE_PATH}; prior SQLite recovery material remains protected until install acceptance."
}

restore_previous_state() {
  if [[ "$RELEASE_STATE_NEW_STARTED" -ne 1 && "$RELEASE_STATE_BACKED_UP" -ne 1 ]]; then
    return 0
  fi
  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "DRY RUN: rollback would restore ${RELEASE_STATE_PATH} and every SQLite sidecar."
    return 0
  fi

  if ! "$PYTHON_BIN" - \
    "$RELEASE_STATE_PATH" "$RELEASE_STATE_PREVIOUS_PATH" "$RELEASE_STATE_BACKED_UP" <<'PY'
import os
import stat
import sys
from pathlib import Path

target = Path(sys.argv[1])
recovery = Path(sys.argv[2])
restore_original = sys.argv[3] == "1"
expected_uid = os.geteuid() if hasattr(os, "geteuid") else None
suffixes = ("", "-wal", "-shm", "-journal")


def unlink_safe(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"refusing unsafe candidate state artifact during rollback: {path}")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise SystemExit(f"candidate state artifact has the wrong owner during rollback: {path}")
    path.unlink()


if restore_original:
    try:
        metadata = os.lstat(recovery)
    except FileNotFoundError:
        # The commit failed before moving any live artifact. The original
        # database is still authoritative and must not be unlinked.
        raise SystemExit(0)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit(f"refusing unsafe state recovery directory: {recovery}")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise SystemExit(f"state recovery directory has the wrong owner: {recovery}")
    expected_names = {f"{target.name}{suffix}" for suffix in suffixes}
    actual_names = {entry.name for entry in recovery.iterdir()}
    if not actual_names:
        recovery.rmdir()
        raise SystemExit(0)
    if not actual_names <= expected_names:
        raise SystemExit(f"state recovery directory has unexpected contents: {recovery}")
    for entry in recovery.iterdir():
        entry_metadata = os.lstat(entry)
        if stat.S_ISLNK(entry_metadata.st_mode) or not stat.S_ISREG(entry_metadata.st_mode):
            raise SystemExit(f"refusing unsafe state recovery artifact: {entry}")
    if target.name not in actual_names:
        # The swap failed while sidecars were being moved, before the original
        # database itself moved. Merge those byte-identical sidecars back and
        # leave the still-live original database untouched.
        for suffix in suffixes[1:]:
            source = recovery / f"{target.name}{suffix}"
            try:
                os.lstat(source)
            except FileNotFoundError:
                continue
            destination = Path(f"{target}{suffix}")
            if destination.exists() or destination.is_symlink():
                raise SystemExit(
                    f"state sidecar target unexpectedly exists during partial rollback: {destination}"
                )
            os.replace(source, destination)
        recovery.rmdir()
        raise SystemExit(0)

for suffix in suffixes:
    unlink_safe(Path(f"{target}{suffix}"))

if restore_original:
    for suffix in suffixes:
        source = recovery / f"{target.name}{suffix}"
        try:
            source_metadata = os.lstat(source)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(source_metadata.st_mode):
            raise SystemExit(f"refusing unsafe state recovery artifact: {source}")
        os.replace(source, Path(f"{target}{suffix}"))
    recovery.rmdir()

parent_descriptor = os.open(target.parent, os.O_RDONLY)
try:
    os.fsync(parent_descriptor)
finally:
    os.close(parent_descriptor)
PY
  then
    log "WARNING: automatic state rollback failed; recovery material remains at ${RELEASE_STATE_PREVIOUS_PATH}."
    return 1
  fi
  RELEASE_STATE_BACKED_UP=0
  RELEASE_STATE_NEW_STARTED=0
  log "Restored the previous state database and SQLite sidecars."
}

remove_previous_state_recovery() {
  [[ "$RELEASE_STATE_BACKED_UP" -eq 1 ]] || return 0
  "$PYTHON_BIN" - "$RELEASE_STATE_PATH" "$RELEASE_STATE_PREVIOUS_PATH" <<'PY'
import os
import stat
import sys
from pathlib import Path

target = Path(sys.argv[1])
recovery = Path(sys.argv[2])
metadata = os.lstat(recovery)
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit(f"refusing unsafe state recovery directory: {recovery}")
if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
    raise SystemExit(f"state recovery directory has the wrong owner: {recovery}")
expected_names = {
    f"{target.name}{suffix}" for suffix in ("", "-wal", "-shm", "-journal")
}
entries = list(recovery.iterdir())
if target.name not in {entry.name for entry in entries}:
    raise SystemExit(f"state recovery directory is missing the original database: {recovery}")
for entry in entries:
    entry_metadata = os.lstat(entry)
    if entry.name not in expected_names:
        raise SystemExit(f"refusing undeclared state recovery artifact: {entry}")
    if stat.S_ISLNK(entry_metadata.st_mode) or not stat.S_ISREG(entry_metadata.st_mode):
        raise SystemExit(f"refusing unsafe state recovery artifact: {entry}")
    entry.unlink()
recovery.rmdir()
parent_descriptor = os.open(target.parent, os.O_RDONLY)
try:
    os.fsync(parent_descriptor)
finally:
    os.close(parent_descriptor)
PY
}

copy_existing_memory_for_canary() {
  local source="$1"
  local destination="$2"
  "$PYTHON_BIN" - "$source" "$destination" <<'PY'
import os
import shutil
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
root = os.lstat(source)
if stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode):
    raise SystemExit(f"refusing unsafe memory source directory: {source}")
if hasattr(os, "geteuid") and root.st_uid != os.geteuid():
    raise SystemExit(f"memory source has the wrong owner: {source}")
destination.mkdir(mode=0o700)
for current_root, directory_names, file_names in os.walk(source, followlinks=False):
    current = Path(current_root)
    relative = current.relative_to(source)
    target_root = destination / relative
    target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in directory_names:
        path = current / name
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit(f"refusing unsafe memory directory entry: {path}")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise SystemExit(f"memory directory has the wrong owner: {path}")
    for name in file_names:
        path = current / name
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(f"refusing unsafe memory file: {path}")
        if metadata.st_nlink != 1:
            raise SystemExit(f"refusing hard-linked memory file: {path}")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise SystemExit(f"memory file has the wrong owner: {path}")
        shutil.copy2(path, target_root / name, follow_symlinks=False)
PY
}

validate_candidate_memory_isolated() {
  if is_true "${KESTREL_DRY_RUN:-}"; then
    INSTALL_CANARY_ROOT="${KESTREL_HOME}/.nest/.install-canary"
    INSTALL_CANARY_MEMORY_DIR="${INSTALL_CANARY_ROOT}/clean-memory"
    INSTALL_CANARY_STATE_PATH="${INSTALL_CANARY_ROOT}/state/agent.db"
  else
    run mkdir -p "${KESTREL_HOME}/.nest"
    INSTALL_CANARY_ROOT="$(mktemp -d "${KESTREL_HOME}/.nest/.install-canary.XXXXXX")" ||
      die "Unable to create private candidate-validation directory."
    chmod 700 "$INSTALL_CANARY_ROOT"
    INSTALL_CANARY_MEMORY_DIR="${INSTALL_CANARY_ROOT}/clean-memory"
    INSTALL_CANARY_STATE_PATH="${INSTALL_CANARY_ROOT}/state/agent.db"
  fi

  stage_candidate_state_isolated
  validate_candidate_state_compatibility
  run .venv/bin/nest-agent init --backend memvid --memory-dir "$INSTALL_CANARY_MEMORY_DIR"
  run .venv/bin/nest-agent memory verify --backend memvid --memory-dir "$INSTALL_CANARY_MEMORY_DIR"

  if [[ -L .nest/memory ]]; then
    die "Refusing symbolic-link live memory directory: ${KESTREL_HOME}/.nest/memory"
  fi
  if ! is_true "${KESTREL_DRY_RUN:-}" && is_nonempty_dir .nest/memory; then
    local compatibility_memory="${INSTALL_CANARY_ROOT}/existing-memory-copy"
    copy_existing_memory_for_canary .nest/memory "$compatibility_memory"
    local verify_command=(
      .venv/bin/nest-agent memory verify --backend memvid --memory-dir "$compatibility_memory"
    )
    if [[ -f .nest/config/layers.json && ! -L .nest/config/layers.json ]]; then
      local canary_layer_config="${INSTALL_CANARY_ROOT}/layers.json"
      run cp -- .nest/config/layers.json "$canary_layer_config"
      verify_command+=(--layer-config "$canary_layer_config")
    fi
    run "${verify_command[@]}"
    INSTALL_MEMORY_STAGED_DIR="$compatibility_memory"
  else
    INSTALL_MEMORY_STAGED_DIR="$INSTALL_CANARY_MEMORY_DIR"
  fi
}

commit_staged_memory() {
  [[ -n "$INSTALL_MEMORY_STAGED_DIR" ]] || die "Candidate memory staging was not prepared."
  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "+ transactionally replace .nest/memory from ${INSTALL_MEMORY_STAGED_DIR}"
    INSTALL_MEMORY_STAGED_DIR=""
    return 0
  fi
  [[ -d "$INSTALL_MEMORY_STAGED_DIR" && ! -L "$INSTALL_MEMORY_STAGED_DIR" ]] ||
    die "Candidate memory staging directory is unsafe or missing: ${INSTALL_MEMORY_STAGED_DIR}"
  [[ ! -e "$RELEASE_MEMORY_PREVIOUS_PATH" && ! -L "$RELEASE_MEMORY_PREVIOUS_PATH" ]] ||
    die "A prior memory recovery directory already exists: ${RELEASE_MEMORY_PREVIOUS_PATH}"
  if [[ -e "$RELEASE_MEMORY_PATH" || -L "$RELEASE_MEMORY_PATH" ]]; then
    [[ -d "$RELEASE_MEMORY_PATH" && ! -L "$RELEASE_MEMORY_PATH" ]] ||
      die "Refusing to replace non-directory or symbolic-link memory: ${RELEASE_MEMORY_PATH}"
    RELEASE_MEMORY_BACKED_UP=1
    run mv -- "$RELEASE_MEMORY_PATH" "$RELEASE_MEMORY_PREVIOUS_PATH"
  fi
  RELEASE_MEMORY_NEW_STARTED=1
  run mv -- "$INSTALL_MEMORY_STAGED_DIR" "$RELEASE_MEMORY_PATH"
  INSTALL_MEMORY_STAGED_DIR=""
}

run_smoke_checks() {
  local memory_dir="$1"
  local state_path="$2"
  if is_true "${KESTREL_SKIP_SMOKE:-}"; then
    log "Skipping smoke checks because KESTREL_SKIP_SMOKE is set."
    return 0
  fi
  run .venv/bin/nest-agent doctor --backend memvid --memory-dir "$memory_dir" --state-path "$state_path" --provider mock --model mock --timeout-seconds 300
  run .venv/bin/nest-agent chat --backend memory --memory-dir "${INSTALL_CANARY_ROOT}/chat-memory" --state-path "$state_path" --provider mock --model mock --message "hello from one-shot install"
}

server_url() {
  printf 'http://127.0.0.1:%s/' "$KESTREL_PORT"
}

health_url() {
  printf 'http://127.0.0.1:%s/api/health/ready' "$KESTREL_PORT"
}

server_is_healthy() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsS --max-time 2 "$(health_url)" >/dev/null 2>&1
}

port_is_available() {
  "$PYTHON_BIN" - "$KESTREL_PORT" <<'PY' >/dev/null 2>&1
import socket
import sys

try:
    port = int(sys.argv[1])
except ValueError:
    raise SystemExit(2)
if not 1 <= port <= 65535:
    raise SystemExit(2)

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", port))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

require_port_available_for_launch() {
  if port_is_available; then
    return 0
  fi
  die "Port ${KESTREL_PORT} is already in use by a process that this installer cannot verify as its own. Refusing to terminate the listener; stop it explicitly or choose another KESTREL_PORT."
}

read_private_pid_file() {
  local path="$1"
  local label="$2"
  "$PYTHON_BIN" - "$path" "$(id -u)" "$label" <<'PY'
import os
import re
import stat
import sys

path = sys.argv[1]
expected_uid = int(sys.argv[2])
label = sys.argv[3]
try:
    before = os.lstat(path)
except FileNotFoundError:
    print(f"{label} PID file disappeared before validation: {path}", file=sys.stderr)
    raise SystemExit(1)
if stat.S_ISLNK(before.st_mode):
    print(f"Refusing symbolic-link {label} PID file: {path}", file=sys.stderr)
    raise SystemExit(1)
if not stat.S_ISREG(before.st_mode):
    print(f"Refusing non-regular {label} PID file: {path}", file=sys.stderr)
    raise SystemExit(1)

flags = os.O_RDONLY
flags |= getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(path, flags)
except OSError as exc:
    print(f"Unable to open {label} PID file safely: {path}: {exc}", file=sys.stderr)
    raise SystemExit(1)
try:
    current = os.fstat(descriptor)
    if not stat.S_ISREG(current.st_mode):
        print(f"Refusing non-regular {label} PID file: {path}", file=sys.stderr)
        raise SystemExit(1)
    if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
        print(f"{label} PID file changed during validation: {path}", file=sys.stderr)
        raise SystemExit(1)
    if current.st_uid != expected_uid:
        print(
            f"Refusing {label} PID file owned by uid {current.st_uid}; expected {expected_uid}: {path}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if stat.S_IMODE(current.st_mode) & 0o077:
        print(f"Refusing non-private {label} PID file: {path}", file=sys.stderr)
        raise SystemExit(1)
    if current.st_size > 32:
        print(f"Refusing oversized {label} PID file: {path}", file=sys.stderr)
        raise SystemExit(1)
    with os.fdopen(descriptor, "r", encoding="ascii", errors="strict", closefd=False) as handle:
        value = handle.read(32)
finally:
    os.close(descriptor)

if not re.fullmatch(r"[1-9][0-9]*\n?", value):
    print(f"Refusing malformed {label} PID file: {path}", file=sys.stderr)
    raise SystemExit(1)
print(value.strip())
PY
}

read_private_server_pid_file() {
  read_private_pid_file "$KESTREL_SERVER_PID" "Kestrel server"
}

read_private_supervisor_pid_file() {
  read_private_pid_file "$KESTREL_SERVER_SUPERVISOR_PID" "Kestrel server supervisor"
}

read_private_server_process_group_file() {
  read_private_pid_file "$KESTREL_SERVER_PROCESS_GROUP" "Kestrel server process group"
}

process_working_directory() {
  local pid="$1"
  if [[ -e "/proc/${pid}/cwd" ]]; then
    readlink "/proc/${pid}/cwd" 2>/dev/null
    return
  fi
  command -v lsof >/dev/null 2>&1 || return 1
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
}

process_is_expected_kestrel_server() {
  local pid="$1"
  local expected_uid process_uid expected_cwd process_cwd process_command
  expected_uid="$(id -u)"
  process_uid="$(ps -p "$pid" -o uid= 2>/dev/null | tr -d '[:space:]')"
  [[ "$process_uid" == "$expected_uid" ]] || return 1

  expected_cwd="$(cd "$KESTREL_HOME" 2>/dev/null && pwd -P)" || return 1
  process_cwd="$(process_working_directory "$pid")" || return 1
  [[ -n "$process_cwd" && "$process_cwd" == "$expected_cwd" ]] || return 1

  process_command="$(ps -ww -p "$pid" -o command= 2>/dev/null)" || return 1
  [[ "$process_command" == *".venv/bin/nest-agent server"* ]] || return 1
  [[ " $process_command " == *" --backend memvid "* ]] || return 1
  [[ " $process_command " == *" --memory-dir .nest/memory "* ]] || return 1
  [[ " $process_command " == *" --provider mock "* ]] || return 1
  [[ " $process_command " == *" --model mock "* ]] || return 1
  [[ " $process_command " == *" --host 127.0.0.1 "* ]] || return 1
  [[ " $process_command " == *" --port ${KESTREL_PORT} "* ]] || return 1
}

process_is_expected_kestrel_supervisor() {
  local pid="$1"
  local expected_uid process_uid expected_cwd process_cwd process_command
  expected_uid="$(id -u)"
  process_uid="$(ps -p "$pid" -o uid= 2>/dev/null | tr -d '[:space:]')"
  [[ "$process_uid" == "$expected_uid" ]] || return 1

  expected_cwd="$(cd "$KESTREL_HOME" && pwd -P)"
  process_cwd="$(process_working_directory "$pid")" || return 1
  [[ -n "$process_cwd" && "$process_cwd" == "$expected_cwd" ]] || return 1

  process_command="$(ps -ww -p "$pid" -o command= 2>/dev/null)" || return 1
  [[ "$process_command" == *"scripts/installer-server-supervisor.sh"* ]] || return 1
  [[ " $process_command " == *" --pid-file ${KESTREL_SERVER_PID} "* ]] || return 1
  [[ " $process_command " == *" --supervisor-pid-file ${KESTREL_SERVER_SUPERVISOR_PID} "* ]] ||
    return 1
  [[ " $process_command " == *" --process-group-file ${KESTREL_SERVER_PROCESS_GROUP} "* ]] ||
    return 1
}

process_exists() {
  ps -p "$1" -o pid= >/dev/null 2>&1
}

process_group_id_for_pid() {
  ps -p "$1" -o pgid= 2>/dev/null | tr -d '[:space:]'
}

process_group_has_live_members() {
  local pgid="$1"
  ps -ax -o pid=,pgid=,stat= 2>/dev/null |
    awk -v target="$pgid" '$2 == target && $3 !~ /^Z/ { found=1 } END { exit(found ? 0 : 1) }'
}

process_group_is_expected_kestrel_server() {
  local pgid="$1"
  [[ "$(process_group_id_for_pid "$pgid")" == "$pgid" ]] || return 1
  process_is_expected_kestrel_server "$pgid"
}

terminate_expected_kestrel_process_group_status() {
  local pgid="$1"
  if [[ "$STARTED_SERVER_PGID" != "$pgid" ]] &&
    ! process_group_is_expected_kestrel_server "$pgid"; then
    log "ERROR: process group ${pgid} is not the verified installer-owned Kestrel server group. Refusing group termination."
    return 1
  fi
  log "Stopping installer-owned Kestrel process group ${pgid}."
  kill -TERM -- "-${pgid}" >/dev/null 2>&1 || true
  local _
  for _ in {1..30}; do
    if ! process_group_has_live_members "$pgid"; then
      return 0
    fi
    sleep 0.1
  done
  kill -KILL -- "-${pgid}" >/dev/null 2>&1 || true
  for _ in {1..30}; do
    if ! process_group_has_live_members "$pgid"; then
      return 0
    fi
    sleep 0.1
  done
  log "ERROR: Kestrel process group ${pgid} still has live members after SIGKILL."
  return 1
}

terminate_expected_kestrel_server_status() {
  local pid="$1"
  if ! process_is_expected_kestrel_server "$pid"; then
    log "ERROR: PID ${pid} from ${KESTREL_SERVER_PID} is not the expected current-user Kestrel server in ${KESTREL_HOME} on port ${KESTREL_PORT}. Refusing to terminate it."
    return 1
  fi

  log "Stopping verified installer-managed Kestrel server process ${pid}"
  if ! kill "$pid" >/dev/null 2>&1 && process_exists "$pid"; then
    log "ERROR: unable to send SIGTERM to verified Kestrel server process ${pid}."
    return 1
  fi
  local _
  for _ in {1..50}; do
    if ! process_exists "$pid"; then
      return 0
    fi
    sleep 0.1
  done

  # Revalidate immediately before SIGKILL so PID reuse cannot redirect the
  # destructive signal to a different process during the grace period.
  if ! process_is_expected_kestrel_server "$pid"; then
    log "ERROR: PID ${pid} changed identity while stopping. Refusing to send SIGKILL."
    return 1
  fi
  if ! kill -9 "$pid" >/dev/null 2>&1 && process_exists "$pid"; then
    log "ERROR: unable to send SIGKILL to verified Kestrel server process ${pid}."
    return 1
  fi
  for _ in {1..20}; do
    if ! process_exists "$pid"; then
      return 0
    fi
    sleep 0.1
  done
  log "ERROR: verified Kestrel server process ${pid} did not stop."
  return 1
}

terminate_expected_kestrel_server() {
  local pid="$1"
  terminate_expected_kestrel_server_status "$pid" ||
    die "Unable to stop the verified installer-managed Kestrel server process ${pid}."
}

remove_private_pid_file_for() {
  local path="$1"
  local label="$2"
  local expected_pid="$3"
  if [[ ! -e "$path" && ! -L "$path" ]]; then
    return 0
  fi
  local recorded_pid
  if ! recorded_pid="$(read_private_pid_file "$path" "$label")"; then
    log "ERROR: unsafe ${label} PID file while removing metadata: ${path}"
    return 1
  fi
  if [[ "$recorded_pid" != "$expected_pid" ]]; then
    log "ERROR: ${label} PID metadata changed from ${expected_pid} to ${recorded_pid}; refusing removal."
    return 1
  fi
  rm -f -- "$path"
}

remove_private_server_pid_file_for() {
  remove_private_pid_file_for "$KESTREL_SERVER_PID" "Kestrel server" "$1"
}

remove_private_supervisor_pid_file_for() {
  remove_private_pid_file_for \
    "$KESTREL_SERVER_SUPERVISOR_PID" "Kestrel server supervisor" "$1"
}

remove_private_server_process_group_file_for() {
  remove_private_pid_file_for \
    "$KESTREL_SERVER_PROCESS_GROUP" "Kestrel server process group" "$1"
}

wait_for_port_available() {
  local _
  for _ in {1..50}; do
    if port_is_available; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

wait_for_managed_server_pid() {
  STARTED_SERVER_PID=""
  local _ candidate_pid
  for _ in {1..100}; do
    if [[ -e "$KESTREL_SERVER_PID" || -L "$KESTREL_SERVER_PID" ]]; then
      if ! candidate_pid="$(read_private_server_pid_file)"; then
        log "ERROR: launched Kestrel server produced unsafe PID metadata: ${KESTREL_SERVER_PID}"
        return 1
      fi
      if process_exists "$candidate_pid" && process_is_expected_kestrel_server "$candidate_pid"; then
        STARTED_SERVER_PID="$candidate_pid"
        return 0
      fi
    fi
    sleep 0.05
  done
  log "ERROR: launched Kestrel server did not publish verifiable private PID metadata at ${KESTREL_SERVER_PID}."
  return 1
}

wait_for_managed_supervisor_pid() {
  STARTED_SUPERVISOR_PID=""
  local _ candidate_pid
  for _ in {1..100}; do
    if [[ -e "$KESTREL_SERVER_SUPERVISOR_PID" || -L "$KESTREL_SERVER_SUPERVISOR_PID" ]]; then
      if ! candidate_pid="$(read_private_supervisor_pid_file)"; then
        log "ERROR: launched Kestrel supervisor produced unsafe PID metadata: ${KESTREL_SERVER_SUPERVISOR_PID}"
        return 1
      fi
      if process_exists "$candidate_pid" && process_is_expected_kestrel_supervisor "$candidate_pid"; then
        STARTED_SUPERVISOR_PID="$candidate_pid"
        return 0
      fi
    fi
    sleep 0.05
  done
  log "ERROR: launched Kestrel supervisor did not publish verifiable private PID metadata before child startup."
  return 1
}

wait_for_managed_server_process_group() {
  STARTED_SERVER_PGID=""
  local _ candidate_pgid
  for _ in {1..100}; do
    if [[ -e "$KESTREL_SERVER_PROCESS_GROUP" || -L "$KESTREL_SERVER_PROCESS_GROUP" ]]; then
      if ! candidate_pgid="$(read_private_server_process_group_file)"; then
        log "ERROR: launched Kestrel server produced unsafe process-group metadata: ${KESTREL_SERVER_PROCESS_GROUP}"
        return 1
      fi
      if process_group_has_live_members "$candidate_pgid" &&
        process_group_is_expected_kestrel_server "$candidate_pgid"; then
        STARTED_SERVER_PGID="$candidate_pgid"
        return 0
      fi
    fi
    sleep 0.05
  done
  log "ERROR: launched Kestrel server did not publish a verifiable dedicated process group."
  return 1
}

wait_for_server_status() {
  if is_true "${KESTREL_DRY_RUN:-}"; then
    return 0
  fi

  local _
  for _ in {1..60}; do
    if server_is_healthy; then
      log "Kestrel server is healthy at $(server_url)"
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_server() {
  wait_for_server_status ||
    die "Kestrel server did not become healthy at $(health_url). Check ${KESTREL_SERVER_LOG}."
}

find_untracked_standard_kestrel_server_pid() {
  # Before a fresh checkout is promoted there cannot be a process whose cwd is
  # the configured install root. Avoid an expensive whole-process scan (and a
  # noisy failed `cd` for every PID) while that root does not exist.
  [[ -d "$KESTREL_HOME" ]] || return 1
  local candidate process_command
  while read -r candidate process_command; do
    candidate="${candidate//[[:space:]]/}"
    [[ -n "$candidate" ]] || continue
    [[ "$process_command" == *".venv/bin/nest-agent server"* ]] || continue
    if process_is_expected_kestrel_server "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(ps -ax -o pid=,command= 2>/dev/null)
  return 1
}

find_untracked_standard_kestrel_supervisor_pid() {
  [[ -d "$KESTREL_HOME" ]] || return 1
  local candidate process_command
  while read -r candidate process_command; do
    candidate="${candidate//[[:space:]]/}"
    [[ -n "$candidate" ]] || continue
    [[ "$process_command" == *"scripts/installer-server-supervisor.sh"* ]] || continue
    if process_is_expected_kestrel_supervisor "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(ps -ax -o pid=,command= 2>/dev/null)
  return 1
}

prove_failed_launch_absent_without_identity() {
  local untracked_pid
  # A successful detached command has already had up to five seconds to
  # publish supervisor metadata. Give an immediately failing launcher one
  # short grace period, then perform the expensive whole-process proof once.
  sleep 0.25
  if untracked_pid="$(find_untracked_standard_kestrel_supervisor_pid)"; then
    log "ERROR: untracked candidate supervisor PID ${untracked_pid} remains after failed launch."
    return 1
  fi
  if untracked_pid="$(find_untracked_standard_kestrel_server_pid)"; then
    log "ERROR: untracked candidate server PID ${untracked_pid} remains after failed launch."
    return 1
  fi
  if ! port_is_available; then
    log "ERROR: port ${KESTREL_PORT} remains occupied after failed launch."
    return 1
  fi

  local recorded
  if [[ -e "$KESTREL_SERVER_SUPERVISOR_PID" || -L "$KESTREL_SERVER_SUPERVISOR_PID" ]]; then
    recorded="$(read_private_supervisor_pid_file)" || return 1
    process_exists "$recorded" && return 1
    remove_private_supervisor_pid_file_for "$recorded" || return 1
  fi
  if [[ -e "$KESTREL_SERVER_PID" || -L "$KESTREL_SERVER_PID" ]]; then
    recorded="$(read_private_server_pid_file)" || return 1
    process_exists "$recorded" && return 1
    remove_private_server_pid_file_for "$recorded" || return 1
  fi
  if [[ -e "$KESTREL_SERVER_PROCESS_GROUP" || -L "$KESTREL_SERVER_PROCESS_GROUP" ]]; then
    recorded="$(read_private_server_process_group_file)" || return 1
    process_group_has_live_members "$recorded" && return 1
    remove_private_server_process_group_file_for "$recorded" || return 1
  fi
  SERVER_LAUNCH_ATTEMPTED=0
  STARTED_SUPERVISOR_PID=""
  STARTED_SERVER_PID=""
  STARTED_SERVER_PGID=""
  log "Proved no candidate supervisor, server process, process group, or port listener survived the failed launch."
}

require_offline_server_upgrade_preflight() {
  if is_true "${KESTREL_DRY_RUN:-}"; then
    log "DRY RUN: server lifecycle preflight would require private PID metadata and configured port to prove the installation offline."
    return 0
  fi

  if [[ -e "$KESTREL_SERVER_SUPERVISOR_PID" || -L "$KESTREL_SERVER_SUPERVISOR_PID" ]]; then
    local supervisor_pid
    if ! supervisor_pid="$(read_private_supervisor_pid_file)"; then
      die "Unsafe Kestrel supervisor PID metadata; refusing to mutate the installation: ${KESTREL_SERVER_SUPERVISOR_PID}"
    fi
    if process_exists "$supervisor_pid"; then
      process_is_expected_kestrel_supervisor "$supervisor_pid" ||
        die "PID ${supervisor_pid} from ${KESTREL_SERVER_SUPERVISOR_PID} is not the expected current-user Kestrel supervisor. Refusing to mutate or terminate it."
      die "A verified installer-managed Kestrel supervisor is running as PID ${supervisor_pid}. Stop the service explicitly, confirm port ${KESTREL_PORT} is free, and re-run. No checkout, .venv, or memory changes were made."
    fi
    log "Removing stale installer-managed supervisor PID metadata for non-running PID ${supervisor_pid}"
    remove_private_supervisor_pid_file_for "$supervisor_pid" ||
      die "Unable to remove stale Kestrel supervisor PID metadata safely."
  fi

  if [[ -e "$KESTREL_SERVER_PID" || -L "$KESTREL_SERVER_PID" ]]; then
    local old_pid
    if ! old_pid="$(read_private_server_pid_file)"; then
      die "Unsafe Kestrel server PID file; refusing an upgrade that could mutate a live service: ${KESTREL_SERVER_PID}"
    fi
    if process_exists "$old_pid"; then
      process_is_expected_kestrel_server "$old_pid" ||
        die "PID ${old_pid} from ${KESTREL_SERVER_PID} is not the expected current-user standard Kestrel server in ${KESTREL_HOME} on port ${KESTREL_PORT}. Refusing to mutate the installation or terminate it."
      die "A verified installer-managed Kestrel server is running as PID ${old_pid}. Stop the service explicitly, confirm port ${KESTREL_PORT} is free, and re-run. No checkout, .venv, or memory changes were made."
    fi
    log "Removing stale installer-managed server PID metadata for non-running PID ${old_pid}"
    remove_private_server_pid_file_for "$old_pid" ||
      die "Unable to remove stale Kestrel server PID metadata safely."
  fi

  if [[ -e "$KESTREL_SERVER_PROCESS_GROUP" || -L "$KESTREL_SERVER_PROCESS_GROUP" ]]; then
    local stale_pgid
    if ! stale_pgid="$(read_private_server_process_group_file)"; then
      die "Unsafe Kestrel server process-group metadata; refusing to mutate the installation: ${KESTREL_SERVER_PROCESS_GROUP}"
    fi
    if process_group_has_live_members "$stale_pgid"; then
      if process_group_is_expected_kestrel_server "$stale_pgid"; then
        die "A Kestrel server process group ${stale_pgid} is still live. Stop the entire service group and re-run. No checkout, .venv, or memory changes were made."
      fi
      die "Process group ${stale_pgid} from ${KESTREL_SERVER_PROCESS_GROUP} is live but not a verified Kestrel group. Refusing to mutate or terminate it."
    fi
    log "Removing stale Kestrel server process-group metadata ${stale_pgid}"
    remove_private_server_process_group_file_for "$stale_pgid" ||
      die "Unable to remove stale Kestrel process-group metadata safely."
  fi

  local untracked_pid
  if untracked_pid="$(find_untracked_standard_kestrel_server_pid)"; then
    die "A standard Kestrel server is running as untracked PID ${untracked_pid} without matching private installer metadata. Stop it explicitly and re-run. No checkout, .venv, or memory changes were made."
  fi
  if ! port_is_available; then
    die "Configured port ${KESTREL_PORT} is occupied or cannot be verified free. Stop the listener explicitly or choose another KESTREL_PORT, then re-run. No checkout, .venv, or memory changes were made."
  fi
}

launch_standard_server_detached() {
  local server_cmd=(
    .venv/bin/nest-agent
    server
    --backend
    memvid
    --memory-dir
    .nest/memory
    --state-path
    "$KESTREL_STATE_PATH"
    --provider
    mock
    --model
    mock
    --host
    127.0.0.1
    --port
    "$KESTREL_PORT"
  )
  local supervisor_cmd=(
    bash
    scripts/installer-server-supervisor.sh
    --pid-file
    "$KESTREL_SERVER_PID"
    --supervisor-pid-file
    "$KESTREL_SERVER_SUPERVISOR_PID"
    --process-group-file
    "$KESTREL_SERVER_PROCESS_GROUP"
    --log-file
    "$KESTREL_SERVER_LOG"
    --
    "${server_cmd[@]}"
  )

  log "Starting Kestrel server at $(server_url)"
  log "Server log: ${KESTREL_SERVER_LOG}"

  if ! is_true "${KESTREL_DRY_RUN:-}"; then
    SERVER_LAUNCH_ATTEMPTED=1
  fi
  if command -v screen >/dev/null 2>&1; then
    # shellcheck disable=SC2016  # Inner shell intentionally expands positional parameters.
    if ! run screen -dmS "$KESTREL_SERVER_SESSION" bash -lc 'cd "$1" || exit 1; shift; exec "$@"' bash "$KESTREL_HOME" "${supervisor_cmd[@]}"; then
      return 1
    fi
  elif command -v tmux >/dev/null 2>&1; then
    if ! run tmux new-session -d -s "$KESTREL_SERVER_SESSION" "cd $(printf '%q' "$KESTREL_HOME") && exec $(quote_cmd "${supervisor_cmd[@]}")"; then
      return 1
    fi
  else
    log "screen/tmux not found; falling back to nohup background launch."
    log "+ nohup $(quote_cmd "${supervisor_cmd[@]}") >>$(printf '%q' "$KESTREL_SERVER_LOG") 2>&1 &"
    if ! is_true "${KESTREL_DRY_RUN:-}"; then
      if ! (
        cd "$KESTREL_HOME" || exit 1
        nohup "${supervisor_cmd[@]}" >>"$KESTREL_SERVER_LOG" 2>&1 &
      ); then
        return 1
      fi
    fi
  fi

  if is_true "${KESTREL_DRY_RUN:-}"; then
    return 0
  fi
  wait_for_managed_supervisor_pid || return 1
  wait_for_managed_server_process_group || return 1
  wait_for_managed_server_pid || return 1
}

terminate_expected_kestrel_supervisor_status() {
  local pid="$1"
  if ! process_is_expected_kestrel_supervisor "$pid"; then
    log "ERROR: PID ${pid} is not the expected current-user Kestrel supervisor. Refusing to terminate it."
    return 1
  fi
  log "Stopping installer-owned Kestrel supervisor ${pid} after failed startup."
  if ! kill "$pid" >/dev/null 2>&1 && process_exists "$pid"; then
    return 1
  fi
  local _
  for _ in {1..80}; do
    if ! process_exists "$pid"; then
      return 0
    fi
    sleep 0.1
  done
  if ! process_is_expected_kestrel_supervisor "$pid"; then
    log "ERROR: supervisor PID ${pid} changed identity while stopping."
    return 1
  fi
  if ! kill -9 "$pid" >/dev/null 2>&1 && process_exists "$pid"; then
    return 1
  fi
  for _ in {1..20}; do
    if ! process_exists "$pid"; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

cleanup_failed_server_launch() {
  [[ "$SERVER_LAUNCH_ATTEMPTED" -eq 1 ]] || return 0
  local supervisor_pid="$STARTED_SUPERVISOR_PID"
  local server_pid="$STARTED_SERVER_PID"
  local server_pgid="$STARTED_SERVER_PGID"

  if [[ -e "$KESTREL_SERVER_SUPERVISOR_PID" || -L "$KESTREL_SERVER_SUPERVISOR_PID" ]]; then
    local recorded_supervisor_pid
    if ! recorded_supervisor_pid="$(read_private_supervisor_pid_file)"; then
      log "ERROR: failed launch left unsafe supervisor PID metadata."
      return 1
    fi
    if [[ -n "$supervisor_pid" && "$recorded_supervisor_pid" != "$supervisor_pid" ]]; then
      log "ERROR: supervisor PID metadata changed during failed-launch cleanup."
      return 1
    fi
    supervisor_pid="$recorded_supervisor_pid"
  fi
  if [[ -z "$supervisor_pid" ]]; then
    prove_failed_launch_absent_without_identity
    return
  fi

  if [[ -e "$KESTREL_SERVER_PROCESS_GROUP" || -L "$KESTREL_SERVER_PROCESS_GROUP" ]]; then
    local recorded_server_pgid
    if ! recorded_server_pgid="$(read_private_server_process_group_file)"; then
      log "ERROR: failed launch left unsafe process-group metadata."
      return 1
    fi
    if [[ -n "$server_pgid" && "$recorded_server_pgid" != "$server_pgid" ]]; then
      log "ERROR: server process-group metadata changed during failed-launch cleanup."
      return 1
    fi
    server_pgid="$recorded_server_pgid"
    if [[ "$STARTED_SERVER_PGID" != "$server_pgid" ]] &&
      process_group_has_live_members "$server_pgid" &&
      ! process_group_is_expected_kestrel_server "$server_pgid"; then
      log "ERROR: process group ${server_pgid} is no longer the expected Kestrel group."
      return 1
    fi
  fi
  if process_exists "$supervisor_pid"; then
    terminate_expected_kestrel_supervisor_status "$supervisor_pid" || return 1
  fi

  if [[ -n "$server_pgid" ]] && process_group_has_live_members "$server_pgid"; then
    terminate_expected_kestrel_process_group_status "$server_pgid" || return 1
  fi

  if [[ -e "$KESTREL_SERVER_PID" || -L "$KESTREL_SERVER_PID" ]]; then
    local recorded_server_pid
    if ! recorded_server_pid="$(read_private_server_pid_file)"; then
      return 1
    fi
    if [[ -n "$server_pid" && "$recorded_server_pid" != "$server_pid" ]]; then
      log "ERROR: child server PID metadata changed during failed-launch cleanup."
      return 1
    fi
    server_pid="$recorded_server_pid"
  fi
  if [[ -n "$server_pid" ]] && process_exists "$server_pid"; then
    terminate_expected_kestrel_server_status "$server_pid" || return 1
  fi

  if [[ -z "$server_pgid" ]]; then
    prove_failed_launch_absent_without_identity
    return
  fi

  remove_private_supervisor_pid_file_for "$supervisor_pid" || return 1
  if [[ -n "$server_pid" ]]; then
    remove_private_server_pid_file_for "$server_pid" || return 1
  fi
  remove_private_server_process_group_file_for "$server_pgid" || return 1
  if process_exists "$supervisor_pid"; then
    log "ERROR: supervisor PID ${supervisor_pid} remains after failed-launch cleanup."
    return 1
  fi
  if [[ -n "$server_pid" ]] && process_exists "$server_pid"; then
    log "ERROR: child server PID ${server_pid} remains after failed-launch cleanup."
    return 1
  fi
  if process_group_has_live_members "$server_pgid"; then
    log "ERROR: server process group ${server_pgid} remains after failed-launch cleanup."
    return 1
  fi
  local untracked_pid
  if untracked_pid="$(find_untracked_standard_kestrel_server_pid)"; then
    log "ERROR: standard Kestrel child PID ${untracked_pid} remains after supervisor cleanup."
    return 1
  fi
  if ! wait_for_port_available; then
    log "ERROR: port ${KESTREL_PORT} remains occupied after failed-launch cleanup."
    return 1
  fi
  SERVER_LAUNCH_ATTEMPTED=0
  STARTED_SUPERVISOR_PID=""
  STARTED_SERVER_PID=""
  STARTED_SERVER_PGID=""
  log "Proved the failed launch supervisor, full server process group, child, and port absent."
}

start_server_detached() {
  if ! is_true "$KESTREL_START_SERVER"; then
    log "Skipping server launch because KESTREL_START_SERVER is disabled."
    return 0
  fi

  run mkdir -p \
    "$(dirname "$KESTREL_SERVER_LOG")" \
    "$(dirname "$KESTREL_SERVER_PID")" \
    "$(dirname "$KESTREL_SERVER_SUPERVISOR_PID")" \
    "$(dirname "$KESTREL_SERVER_PROCESS_GROUP")"
  require_port_available_for_launch
  if ! launch_standard_server_detached; then
    if cleanup_failed_server_launch; then
      die "The candidate Kestrel server could not be launched; the failed supervisor and child were stopped and the install transaction will be restored."
    fi
    die "The candidate server launch failed and process absence could not be proven. Automatic rollback is unsafe; inspect ${KESTREL_SERVER_SUPERVISOR_PID}, ${KESTREL_SERVER_PID}, and port ${KESTREL_PORT} before retrying."
  fi
  if ! wait_for_server_status; then
    if cleanup_failed_server_launch; then
      die "The candidate Kestrel server did not become healthy and was stopped; the install transaction will be restored. Check ${KESTREL_SERVER_LOG}."
    fi
    die "Candidate server health failed and automatic cleanup could not prove the supervisor, child, and port absent. Automatic rollback is unsafe; manual recovery is required."
  fi
}

open_web_ui() {
  if ! is_true "$KESTREL_START_SERVER" || ! is_true "$KESTREL_OPEN_BROWSER"; then
    return 0
  fi

  local url
  url="$(server_url)"
  log "Opening Kestrel web UI at ${url}"
  if command -v open >/dev/null 2>&1; then
    run open "$url" || return 1
  elif command -v xdg-open >/dev/null 2>&1; then
    run xdg-open "$url" || return 1
  else
    log "No browser opener found. Open ${url} in your browser."
  fi
}

open_web_ui_best_effort() {
  if ! open_web_ui; then
    log "WARNING: the install and server startup committed successfully, but the browser could not be opened. Open $(server_url) manually."
  fi
  return 0
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
  kill "\$(cat "${KESTREL_SERVER_SUPERVISOR_PID}")"
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
  .venv/bin/nest-agent server --backend memvid --memory-dir .nest/memory --state-path "${KESTREL_STATE_PATH}" --provider mock --model mock --host 127.0.0.1 --port ${KESTREL_PORT}

Open:
  http://127.0.0.1:${KESTREL_PORT}/
EOF
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    return 0
  fi

  require_supported_platform

  if [[ ${KESTREL_REPO+x} == x ]]; then
    KESTREL_REPO_OVERRIDE_SET=1
  else
    KESTREL_REPO_OVERRIDE_SET=0
  fi
  if [[ ${KESTREL_REF+x} == x ]]; then
    KESTREL_REF_OVERRIDE_SET=1
  else
    KESTREL_REF_OVERRIDE_SET=0
  fi
  if [[ ${KESTREL_REQUIREMENTS_URL+x} == x ]]; then
    KESTREL_REQUIREMENTS_URL_OVERRIDE_SET=1
  else
    KESTREL_REQUIREMENTS_URL_OVERRIDE_SET=0
  fi
  if [[ ${KESTREL_WHEEL_URL+x} == x ]]; then
    KESTREL_WHEEL_URL_OVERRIDE_SET=1
  else
    KESTREL_WHEEL_URL_OVERRIDE_SET=0
  fi
  if [[ ${KESTREL_CHECKSUMS_URL+x} == x ]]; then
    KESTREL_CHECKSUMS_URL_OVERRIDE_SET=1
  else
    KESTREL_CHECKSUMS_URL_OVERRIDE_SET=0
  fi
  KESTREL_HOME="${KESTREL_HOME:-$DEFAULT_HOME}"
  KESTREL_REPO="${KESTREL_REPO:-$DEFAULT_REPO}"
  KESTREL_REF="${KESTREL_REF:-main}"
  KESTREL_EXTRAS="${KESTREL_EXTRAS:-$DEFAULT_EXTRAS}"
  KESTREL_REQUIREMENTS_URL="${KESTREL_REQUIREMENTS_URL-$DEFAULT_REQUIREMENTS_URL}"
  KESTREL_WHEEL_URL="${KESTREL_WHEEL_URL-$DEFAULT_WHEEL_URL}"
  KESTREL_CHECKSUMS_URL="${KESTREL_CHECKSUMS_URL-$DEFAULT_CHECKSUMS_URL}"
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
  KESTREL_SERVER_SUPERVISOR_PID="${KESTREL_SERVER_SUPERVISOR_PID:-${KESTREL_HOME}/.nest/server.supervisor.pid}"
  KESTREL_SERVER_PROCESS_GROUP="${KESTREL_SERVER_PROCESS_GROUP:-${KESTREL_HOME}/.nest/server.pgid}"
  # Keep the dedicated Kestrel virtual environment isolated from the caller's
  # Python packages. An inherited PYTHONPATH can make pip incorrectly treat
  # dependencies from another environment as already installed.
  unset PYTHONPATH
  PYTHON_BIN="$(detect_python)"
  KESTREL_HOME="$(absolute_path "$KESTREL_HOME")"
  KESTREL_STATE_PATH="$(resolve_runtime_state_path)"
  KESTREL_SERVER_LOG="$(absolute_path "$KESTREL_SERVER_LOG")"
  KESTREL_SERVER_PID="$(absolute_path "$KESTREL_SERVER_PID")"
  KESTREL_SERVER_SUPERVISOR_PID="$(absolute_path "$KESTREL_SERVER_SUPERVISOR_PID")"
  KESTREL_SERVER_PROCESS_GROUP="$(absolute_path "$KESTREL_SERVER_PROCESS_GROUP")"
  readonly KESTREL_HOME KESTREL_STATE_PATH KESTREL_REPO KESTREL_REF KESTREL_REPO_OVERRIDE_SET KESTREL_REF_OVERRIDE_SET KESTREL_REQUIREMENTS_URL_OVERRIDE_SET KESTREL_WHEEL_URL_OVERRIDE_SET KESTREL_CHECKSUMS_URL_OVERRIDE_SET KESTREL_EXTRAS KESTREL_REQUIREMENTS_URL KESTREL_WHEEL_URL KESTREL_CHECKSUMS_URL KESTREL_PORT KESTREL_START_SERVER KESTREL_OPEN_BROWSER KESTREL_SERVER_SESSION KESTREL_SERVER_LOG KESTREL_SERVER_PID KESTREL_SERVER_SUPERVISOR_PID KESTREL_SERVER_PROCESS_GROUP PYTHON_BIN
  validate_release_artifact_config
  require_safe_install_paths
  # Source and staged-release upgrades both mutate the checkout, .venv, and
  # memory. Keep one transaction around the full service handoff for either.
  start_release_install_transaction

  if is_true "${KESTREL_DRY_RUN:-}"; then
    DRY_RUN_LABEL="yes"
    log "DRY RUN: commands will be printed but not executed."
  else
    DRY_RUN_LABEL="no"
  fi
  if [[ -n "$KESTREL_WHEEL_URL" ]]; then
    PYTHON_PACKAGE_LABEL="$KESTREL_WHEEL_URL"
    REQUIREMENTS_LABEL="$KESTREL_REQUIREMENTS_URL"
    RELEASE_SHA_LABEL="$DEFAULT_RELEASE_SHA"
    WEB_BUILD_LABEL="bundled in verified release wheel"
  elif is_true "${KESTREL_SKIP_WEB:-}"; then
    PYTHON_PACKAGE_LABEL="editable checkout"
    REQUIREMENTS_LABEL="live resolver (development/source mode)"
    RELEASE_SHA_LABEL="not applicable (development/source mode)"
    WEB_BUILD_LABEL="skipped"
  else
    PYTHON_PACKAGE_LABEL="editable checkout"
    REQUIREMENTS_LABEL="live resolver (development/source mode)"
    RELEASE_SHA_LABEL="not applicable (development/source mode)"
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
    SERVER_COMMAND_LABEL="nest-agent server --backend memvid --memory-dir .nest/memory --state-path ${KESTREL_STATE_PATH} --provider mock --model mock --host 127.0.0.1 --port ${KESTREL_PORT}"
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
  readonly DRY_RUN_LABEL PYTHON_PACKAGE_LABEL REQUIREMENTS_LABEL RELEASE_SHA_LABEL WEB_BUILD_LABEL SMOKE_LABEL SERVER_LABEL BROWSER_LABEL HEALTH_CHECK_LABEL WEB_UI_LABEL SERVER_COMMAND_LABEL

  print_runtime_defaults
  print_install_plan
  if [[ -d "${KESTREL_HOME}/.git" ]]; then
    acquire_maintenance_lock
  fi
  # This is deliberately before fetch/checkout, virtual-environment creation,
  # and memory initialization. Upgrades are offline-only; the installer never
  # kills or hot-replaces an existing service.
  require_offline_server_upgrade_preflight
  ensure_git_target
  if [[ "$MAINTENANCE_LOCK_ACQUIRED" -ne 1 ]] && ! is_true "${KESTREL_DRY_RUN:-}"; then
    # A fresh target cannot host a runtime before checkout promotion. Acquire
    # the exact runtime lock immediately after the atomic promotion and before
    # any environment, memory, or state operation.
    acquire_maintenance_lock
  fi

  if ! is_true "${KESTREL_DRY_RUN:-}"; then
    cd "$KESTREL_HOME"
  fi

  install_python_deps
  install_web_deps
  verify_installed_runtime
  validate_candidate_memory_isolated
  run_smoke_checks "$INSTALL_CANARY_MEMORY_DIR" "$INSTALL_CANARY_STATE_PATH"
  prepare_migrated_state_for_commit
  commit_staged_web_assets
  commit_staged_memory
  commit_staged_state
  if is_true "$KESTREL_START_SERVER"; then
    # A real server may recover durable work as soon as it starts, so release
    # maintenance ownership only after every candidate artifact is committed.
    # Keep the original state database and sidecars until the launched server
    # passes readiness; a failed launch is stopped before the trap restores all
    # filesystem and state changes.
    cleanup_install_canary
    cleanup_staged_state
    cleanup_staged_web_assets
    MAINTENANCE_LOCK_RELEASED_FOR_SERVER=1
    release_maintenance_lock
    start_server_detached
    finalize_release_install_transaction
    # finalize_release_install_transaction is the acceptance linearization
    # point.  Until it clears RELEASE_TRANSACTION_ACTIVE, the EXIT trap must
    # still stop a healthy candidate before attempting rollback.
    SERVER_LAUNCH_ATTEMPTED=0
    MAINTENANCE_LOCK_RELEASED_FOR_SERVER=0
    finish_post_commit_maintenance
  else
    finalize_release_install_transaction
    finish_post_commit_maintenance
    start_server_detached
  fi
  open_web_ui_best_effort
  print_next_steps
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  main "$@"
fi
