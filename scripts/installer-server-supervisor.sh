#!/usr/bin/env bash
set -Eeuo pipefail

die() {
  printf '[kestrel-server-supervisor] ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "${1:-}" == "--pid-file" && -n "${2:-}" ]] || die "missing --pid-file"
pid_file="$2"
shift 2
[[ "${1:-}" == "--supervisor-pid-file" && -n "${2:-}" ]] ||
  die "missing --supervisor-pid-file"
supervisor_pid_file="$2"
shift 2
[[ "${1:-}" == "--process-group-file" && -n "${2:-}" ]] ||
  die "missing --process-group-file"
process_group_file="$2"
shift 2
[[ "${1:-}" == "--log-file" && -n "${2:-}" ]] || die "missing --log-file"
log_file="$2"
shift 2
[[ "${1:-}" == "--" ]] || die "missing command separator"
shift
[[ "$#" -gt 0 ]] || die "missing server command"

pid_tmp=""
supervisor_pid_tmp=""
process_group_tmp=""
child_pid=""
child_pgid=""

# shellcheck disable=SC2317,SC2329  # Invoked by the EXIT-trap cleanup path.
process_group_has_live_members() {
  ps -ax -o pid=,pgid=,stat= 2>/dev/null |
    awk -v target="$1" '$2 == target && $3 !~ /^Z/ { found=1 } END { exit(found ? 0 : 1) }'
}

# shellcheck disable=SC2317,SC2329  # Invoked indirectly by the EXIT trap.
cleanup() {
  local status="$?"
  trap - EXIT INT TERM
  if [[ -n "$child_pgid" ]]; then
    kill -TERM -- "-${child_pgid}" >/dev/null 2>&1 || true
    local _
    for _ in {1..30}; do
      process_group_has_live_members "$child_pgid" || break
      sleep 0.1
    done
    if process_group_has_live_members "$child_pgid"; then
      kill -KILL -- "-${child_pgid}" >/dev/null 2>&1 || true
      for _ in {1..30}; do
        process_group_has_live_members "$child_pgid" || break
        sleep 0.1
      done
    fi
    if process_group_has_live_members "$child_pgid"; then
      printf '[kestrel-server-supervisor] ERROR: process group %s survived cleanup\n' "$child_pgid" >&2
      status=1
    fi
  elif [[ -n "$child_pid" ]] && kill -0 "$child_pid" >/dev/null 2>&1; then
    kill "$child_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$child_pid" ]]; then
    wait "$child_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$pid_tmp" ]]; then
    rm -f -- "$pid_tmp"
  fi
  if [[ -n "$supervisor_pid_tmp" ]]; then
    rm -f -- "$supervisor_pid_tmp"
  fi
  if [[ -n "$process_group_tmp" ]]; then
    rm -f -- "$process_group_tmp"
  fi
  if [[ -n "$child_pid" && -f "$pid_file" && ! -L "$pid_file" ]]; then
    local recorded_pid
    recorded_pid="$(tr -d '[:space:]' <"$pid_file" 2>/dev/null || true)"
    if [[ "$recorded_pid" == "$child_pid" ]]; then
      rm -f -- "$pid_file"
    fi
  fi
  if [[ -f "$supervisor_pid_file" && ! -L "$supervisor_pid_file" ]]; then
    local recorded_supervisor_pid
    recorded_supervisor_pid="$(tr -d '[:space:]' <"$supervisor_pid_file" 2>/dev/null || true)"
    if [[ "$recorded_supervisor_pid" == "$$" ]]; then
      rm -f -- "$supervisor_pid_file"
    fi
  fi
  if [[ -n "$child_pgid" && -f "$process_group_file" && ! -L "$process_group_file" ]]; then
    local recorded_process_group
    recorded_process_group="$(tr -d '[:space:]' <"$process_group_file" 2>/dev/null || true)"
    if [[ "$recorded_process_group" == "$child_pgid" ]]; then
      rm -f -- "$process_group_file"
    fi
  fi
  exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -L "$pid_file" ]]; then
  die "refusing symbolic-link PID file: ${pid_file}"
fi
if [[ -e "$pid_file" && ! -f "$pid_file" ]]; then
  die "refusing non-regular PID file: ${pid_file}"
fi
if [[ -L "$supervisor_pid_file" ]]; then
  die "refusing symbolic-link supervisor PID file: ${supervisor_pid_file}"
fi
if [[ -e "$supervisor_pid_file" && ! -f "$supervisor_pid_file" ]]; then
  die "refusing non-regular supervisor PID file: ${supervisor_pid_file}"
fi
if [[ -L "$process_group_file" ]]; then
  die "refusing symbolic-link process-group file: ${process_group_file}"
fi
if [[ -e "$process_group_file" && ! -f "$process_group_file" ]]; then
  die "refusing non-regular process-group file: ${process_group_file}"
fi

umask 077
supervisor_pid_tmp="$(mktemp "${supervisor_pid_file}.tmp.XXXXXX")"
printf '%s\n' "$$" >"$supervisor_pid_tmp"
chmod 600 "$supervisor_pid_tmp"
if [[ -L "$supervisor_pid_file" ]]; then
  die "supervisor PID file became a symbolic link during launch: ${supervisor_pid_file}"
fi
if [[ -e "$supervisor_pid_file" && ! -f "$supervisor_pid_file" ]]; then
  die "supervisor PID file became non-regular during launch: ${supervisor_pid_file}"
fi
mv -f -- "$supervisor_pid_tmp" "$supervisor_pid_file"
supervisor_pid_tmp=""

# The supervisor identity is durably published before the child can exist.
# This gives the installer a process handle even if child PID publication or
# health readiness later fails.
set -m
"$@" >>"$log_file" 2>&1 &
child_pid="$!"
set +m
child_pgid="$(ps -p "$child_pid" -o pgid= 2>/dev/null | tr -d '[:space:]')"
[[ "$child_pgid" == "$child_pid" ]] ||
  die "server child did not enter its dedicated process group"
process_group_tmp="$(mktemp "${process_group_file}.tmp.XXXXXX")"
printf '%s\n' "$child_pgid" >"$process_group_tmp"
chmod 600 "$process_group_tmp"
if [[ -L "$process_group_file" ]]; then
  die "process-group file became a symbolic link during launch: ${process_group_file}"
fi
if [[ -e "$process_group_file" && ! -f "$process_group_file" ]]; then
  die "process-group file became non-regular during launch: ${process_group_file}"
fi
mv -f -- "$process_group_tmp" "$process_group_file"
process_group_tmp=""

pid_tmp="$(mktemp "${pid_file}.tmp.XXXXXX")"
printf '%s\n' "$child_pid" >"$pid_tmp"
chmod 600 "$pid_tmp"

# Recheck immediately before the atomic rename. mv replaces a symlink itself,
# never its target, but refusing it keeps the PID-path contract explicit.
if [[ -L "$pid_file" ]]; then
  die "PID file became a symbolic link during launch: ${pid_file}"
fi
if [[ -e "$pid_file" && ! -f "$pid_file" ]]; then
  die "PID file became non-regular during launch: ${pid_file}"
fi
mv -f -- "$pid_tmp" "$pid_file"
pid_tmp=""

set +e
wait "$child_pid"
status="$?"
set -e
exit "$status"
