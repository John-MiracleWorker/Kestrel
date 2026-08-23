#!/usr/bin/env python3
"""Install one exact Kestrel release payload into a fresh virtual environment,
start the installed entry point, await readiness, complete one bounded mock
mission over the authenticated channel API, and verify cleanup (REL-005).

Cleanup verification is threefold: the server process must exit after one
graceful signal, the bound port must refuse new connections, and a fresh CLI
invocation against the SAME state database must succeed — proving the
single-owner runtime lock was released by the clean shutdown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_exact_wheel_install import (  # noqa: E402
    DEFAULT_EXTRAS,
    verify_exact_wheel_install,
)

REPORT_SCHEMA = "kestrel.installed_artifact_mission.v1"
DEFAULT_MISSION_MESSAGE = "Deterministic installed-artifact mock mission (REL-005)."
READINESS_DEADLINE_SECONDS = 90.0
MISSION_DEADLINE_SECONDS = 180.0
SHUTDOWN_DEADLINE_SECONDS = 30.0
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "blocked"})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def entry_point(venv_root: Path, *, os_name: str = os.name) -> Path:
    if os_name == "nt":
        return venv_root / "Scripts" / "nest-agent.exe"
    return venv_root / "bin" / "nest-agent"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _run_checked(
    command: list[str], *, cwd: Path, environment: dict[str, str]
) -> str:
    completed = subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"command failed ({command[0]}): {detail}")
    return completed.stdout.strip()


def _http_json(
    url: str, *, headers: dict[str, str], method: str, payload: dict[str, Any] | None
) -> tuple[int, dict[str, Any]]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            body = response.read()
            parsed = json.loads(body) if body else {}
            if not isinstance(parsed, dict):
                raise ValueError(f"{method} {url} returned non-object JSON")
            return int(response.status), parsed
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {}


def _accepted_exit_code(exit_code: int, *, os_name: str) -> bool:
    """A clean shutdown either exits 0 itself or, on POSIX, is re-raised by
    uvicorn after the graceful shutdown completes — uvicorn restores the
    default handler and re-raises the captured SIGTERM so the parent observes
    signal death, the canonical POSIX daemon outcome. Windows terminate()
    yields an implementation-defined code, so only the port and state-lock
    release probes judge cleanliness there."""
    if os_name == "nt":
        return True
    return exit_code in (0, -signal.SIGTERM)


def _wait_for_server_exit(process: subprocess.Popen[Any], *, pid: int) -> int:
    try:
        return process.wait(timeout=SHUTDOWN_DEADLINE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise ValueError(
            f"installed server (pid {pid}) did not exit after SIGTERM"
        ) from exc


def run_installed_artifact_mission(
    *,
    payload: Path,
    expected_version: str,
    source_root: Path,
    work_root: Path,
    extras: str = DEFAULT_EXTRAS,
    mission_message: str = DEFAULT_MISSION_MESSAGE,
    readiness_deadline_seconds: float = READINESS_DEADLINE_SECONDS,
) -> dict[str, Any]:
    payload = payload.resolve(strict=True)
    source_root = source_root.resolve(strict=True)
    work_root = work_root.expanduser().resolve(strict=False)
    if work_root.exists() or work_root.is_symlink():
        raise ValueError(f"mission work root already exists: {work_root}")
    work_root.mkdir(parents=True, mode=0o700)
    steps = ["work_root_created"]

    install_root = work_root / "install"
    install_report = verify_exact_wheel_install(
        payload,
        expected_version=expected_version,
        source_root=source_root,
        work_root=install_root,
        extras=extras,
    )
    venv_root = install_root / "venv"
    assert isinstance(install_report["python"], str)
    entry = entry_point(venv_root)
    if not entry.is_file():
        raise ValueError(f"installed entry point is missing: {entry}")
    wheel_name = str(install_report["wheel"])
    wheel_path = payload / wheel_name
    wheel_digest = _sha256_file(wheel_path)
    steps.extend(["exact_wheel_installed", "installed_entry_point_present"])

    environment = _environment()
    environment["NEST_AGENT_API_TOKEN"] = secrets.token_urlsafe(24)
    environment["NEST_AGENT_REQUIRE_API_AUTH"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    token = environment["NEST_AGENT_API_TOKEN"]
    auth_headers = {"Authorization": f"Bearer {token}"}

    venv_python = Path(install_report["python"])
    python_version = _run_checked(
        [str(venv_python), "-c", "import sys; print(sys.version.split()[0])"],
        cwd=work_root,
        environment=environment,
    )
    _run_checked(
        [
            str(entry),
            "doctor",
            "--backend",
            "memory",
            "--memory-dir",
            str(work_root / "entry-doctor-memory"),
            "--provider",
            "mock",
            "--model",
            "mock",
        ],
        cwd=work_root,
        environment=environment,
    )
    steps.append("installed_entry_point_executed")

    server_root = work_root / "server-run"
    memory_dir = server_root / "memory"
    state_path = server_root / "state" / "agent.db"
    log_dir = server_root / "logs"
    secrets_path = server_root / "secrets" / "store.json"
    workspace_dir = server_root / "workspace"
    server_cwd = server_root / "cwd"
    for directory in (memory_dir, log_dir, server_cwd, workspace_dir):
        directory.mkdir(parents=True)
    state_path.parent.mkdir(parents=True)
    secrets_path.parent.mkdir(parents=True)
    port = free_port()
    server_log = server_root / "server.log"
    log_handle = server_log.open("wb")
    server = subprocess.Popen(  # noqa: S603
        [
            str(entry),
            "server",
            "--backend",
            "memory",
            "--memory-dir",
            str(memory_dir),
            "--state-path",
            str(state_path),
            "--log-dir",
            str(log_dir),
            "--secret-store-path",
            str(secrets_path),
            "--provider",
            "mock",
            "--model",
            "mock",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=server_cwd,
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    server_pid = server.pid
    assert server_pid is not None
    steps.append("installed_server_started")
    base_url = f"http://127.0.0.1:{port}"

    readiness: dict[str, Any]
    mission: dict[str, Any]
    try:
        readiness = _await_readiness(
            base_url,
            auth_headers=auth_headers,
            deadline_seconds=readiness_deadline_seconds,
        )
        steps.append("readiness_observed")
        mission = _complete_mock_mission(
            base_url,
            auth_headers=auth_headers,
            mission_message=mission_message,
            workspace_dir=workspace_dir,
        )
        steps.append("mock_mission_completed")
    finally:
        if server.poll() is None:
            os.kill(server_pid, signal.SIGTERM)
            if os.name == "nt":
                # The installed console-script launcher (nest-agent.exe shim)
                # spawns the real python.exe as a child; TerminateProcess on
                # the shim leaves the listener alive. Kill the whole tree so
                # the port and state-lock release probes observe true cleanup.
                try:
                    subprocess.run(  # noqa: S603
                        ["taskkill", "/PID", str(server_pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                except OSError:
                    pass
    exit_code = _wait_for_server_exit(server, pid=server_pid)
    log_handle.close()
    steps.append("server_stopped")

    if os.name != "nt" and not _accepted_exit_code(exit_code, os_name=os.name):
        raise ValueError(
            f"installed server exited uncleanly after SIGTERM: {exit_code} "
            f"(see {server_log})"
        )
    port_released = _port_released(port)
    if not port_released:
        raise ValueError(f"installed server port still accepts connections: {port}")
    steps.append("port_released")

    lock_released = _state_lock_released(
        entry=entry,
        state_path=state_path,
        work_root=work_root,
        environment=environment,
    )
    if not lock_released:
        raise ValueError(
            f"single-owner state lock was not released after shutdown: {state_path}"
        )
    steps.append("state_lock_released")

    return {
        "schema": REPORT_SCHEMA,
        "payload": {
            "wheel": wheel_name,
            "wheel_sha256": wheel_digest,
            "version": expected_version,
        },
        "python": {"version": python_version, "executable": str(venv_python)},
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "entry_point": str(entry),
        "server": {"pid": server_pid, "host": "127.0.0.1", "port": port},
        "readiness": readiness,
        "mission": mission,
        "cleanup": {
            "signal": "SIGTERM",
            "exit_code": exit_code,
            "port_released": port_released,
            "lock_released": lock_released,
        },
        "steps": steps,
        "passed": True,
    }


def _await_readiness(
    base_url: str,
    *,
    auth_headers: dict[str, str],
    deadline_seconds: float = READINESS_DEADLINE_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + deadline_seconds

    # The installed server needs a bounded window to import the runtime and
    # bind its port; a refused connection during that window means "not up
    # yet", not a failure. Only after a real HTTP response arrives must the
    # unauthenticated readiness probe be exactly 401.
    unauthenticated_status: int | None = None
    attempts = 0
    while unauthenticated_status is None:
        attempts += 1
        try:
            unauthenticated_status, _ = _http_json(
                f"{base_url}/api/health/ready",
                headers={},
                method="GET",
                payload=None,
            )
        except urllib.error.URLError:
            if time.monotonic() >= deadline:
                raise ValueError(
                    "installed server did not accept readiness probes within "
                    f"{READINESS_DEADLINE_SECONDS:.0f}s"
                ) from None
            time.sleep(1.0)
    if unauthenticated_status != 401:
        raise ValueError(
            "unauthenticated readiness probe returned "
            f"{unauthenticated_status}, expected 401"
        )
    while True:
        attempts += 1
        status, body = _http_json(
            f"{base_url}/api/health/ready",
            headers=auth_headers,
            method="GET",
            payload=None,
        )
        if status == 200 and body.get("ok") is True:
            return {
                "unauthenticated_status": unauthenticated_status,
                "authenticated_attempts": attempts,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "ok": True,
            }
        if time.monotonic() >= deadline:
            raise ValueError(
                "installed server did not report ready within "
                f"{READINESS_DEADLINE_SECONDS:.0f}s (last status {status})"
            )
        time.sleep(1.0)


def _complete_mock_mission(
    base_url: str,
    *,
    auth_headers: dict[str, str],
    mission_message: str,
    workspace_dir: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    status, created = _http_json(
        f"{base_url}/api/runs",
        headers=auth_headers,
        method="POST",
        payload={
            "message": mission_message,
            "autonomy_mode": "manual",
            "workspace": str(workspace_dir),
        },
    )
    if status not in {200, 201, 202} or not isinstance(created.get("run_id"), str):
        raise ValueError(
            f"mock mission was not admitted (status {status}): {created}"
        )
    run_id = str(created["run_id"])
    deadline = time.monotonic() + MISSION_DEADLINE_SECONDS
    attempts = 0
    terminal_status = "unknown"
    assistant_message = ""
    while time.monotonic() < deadline:
        attempts += 1
        _, run = _http_json(
            f"{base_url}/api/runs/{run_id}",
            headers=auth_headers,
            method="GET",
            payload=None,
        )
        record = run.get("run", run)
        terminal_status = str(record.get("status", ""))
        if terminal_status in _TERMINAL_RUN_STATUSES:
            assistant_message = str(record.get("assistant_message") or "").strip()
            break
        time.sleep(1.0)
    if terminal_status != "completed":
        raise ValueError(
            f"mock mission {run_id} ended in status {terminal_status!r} "
            f"with assistant response {assistant_message!r}"
        )
    if not assistant_message:
        raise ValueError(f"mock mission {run_id} completed without an assistant message")
    return {
        "run_id": run_id,
        "message_sha256": _sha256_text(mission_message),
        "terminal_status": terminal_status,
        "assistant_message_sha256": _sha256_text(assistant_message),
        "attempts": attempts,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def _port_released(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            return True
        return False


def _state_lock_released(
    *, entry: Path, state_path: Path, work_root: Path, environment: dict[str, str]
) -> bool:
    probe_memory = work_root / "lock-probe-memory"
    try:
        _run_checked(
            [
                str(entry),
                "chat",
                "--backend",
                "memory",
                "--memory-dir",
                str(probe_memory),
                "--provider",
                "mock",
                "--model",
                "mock",
                "--state-path",
                str(state_path),
                "--message",
                "cleanup lock release probe",
            ],
            cwd=work_root,
            environment=environment,
        )
    except ValueError:
        return False
    return True


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--extras", default=DEFAULT_EXTRAS)
    parser.add_argument("--mission-message", default=DEFAULT_MISSION_MESSAGE)
    parser.add_argument(
        "--readiness-deadline-seconds",
        type=float,
        default=READINESS_DEADLINE_SECONDS,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = run_installed_artifact_mission(
            payload=args.payload,
            expected_version=args.expected_version,
            source_root=args.source_root,
            work_root=args.work_root,
            extras=args.extras,
            mission_message=args.mission_message,
            readiness_deadline_seconds=args.readiness_deadline_seconds,
        )
        if args.output is not None:
            _write_json(args.output, report)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
