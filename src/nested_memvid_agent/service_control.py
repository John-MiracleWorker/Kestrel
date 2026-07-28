from __future__ import annotations

import math
import os
import shlex
import socket
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .file_lock import lock_exclusive, unlock
from .platform_primitives import required_signal, signal_process_group
from .private_artifacts import (
    create_private_empty_file,
    ensure_private_directory,
    ensure_owner_only_directory,
    open_private_file_descriptor,
)
from .server_client import KestrelServerClient, ServerProbe


class ServiceState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    CONFLICT = "conflict"


class ServiceManagement(StrEnum):
    MANAGED = "managed"
    EXTERNAL = "external"
    NONE = "none"


@dataclass(frozen=True)
class ServicePaths:
    home: Path
    state_path: Path
    memory_dir: Path
    log_path: Path
    pid_path: Path
    supervisor_pid_path: Path
    pgid_path: Path
    lifecycle_lock_path: Path
    supervisor_script: Path
    server_executable: Path
    host: str
    port: int
    url: str


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    uid: int
    cwd: Path
    command: tuple[str, ...]
    pgid: int
    state: str
    birth_marker: str = ""


@dataclass(frozen=True)
class BoundListener:
    """A TCP listener together with the exact local endpoint lsof reported."""

    pid: int
    host: str
    port: int


@dataclass(frozen=True)
class ServiceStatus:
    state: ServiceState
    management: ServiceManagement
    url: str
    pid: int | None
    supervisor_pid: int | None
    pgid: int | None
    detail: str
    lifecycle_busy: bool = False


class ServiceControlError(RuntimeError):
    def __init__(self, message: str, *, code: str, recovery: str) -> None:
        super().__init__(message)
        self.code = code
        self.recovery = recovery


class ProcessInspector(Protocol):
    def process(self, pid: int) -> ProcessSnapshot | None: ...

    def listeners(self, host: str, port: int) -> tuple[BoundListener, ...]: ...

    def group_has_live_members(self, pgid: int) -> bool: ...

    def port_is_bindable(self, host: str, port: int) -> bool: ...


class ProbeClient(Protocol):
    def probe(self) -> ServerProbe: ...


class ProcessSignaler(Protocol):
    def signal_pid(self, pid: int, signal_number: int) -> None: ...

    def signal_group(self, pgid: int, signal_number: int) -> None: ...


class SystemProcessSignaler:
    def signal_pid(self, pid: int, signal_number: int) -> None:
        os.kill(pid, signal_number)

    def signal_group(self, pgid: int, signal_number: int) -> None:
        signal_process_group(pgid, signal_number)


class SystemProcessInspector:
    def process(self, pid: int) -> ProcessSnapshot | None:
        if pid <= 0:
            return None
        result = subprocess.run(
            [
                "ps",
                "-ww",
                "-p",
                str(pid),
                "-o",
                "uid=",
                "-o",
                "pgid=",
                "-o",
                "state=",
                "-o",
                "lstart=",
                "-o",
                "command=",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        parts = result.stdout.strip().split(None, 8)
        if len(parts) != 9:
            raise ServiceControlError(
                f"Could not parse process identity for PID {pid}.",
                code="process_inspection_failed",
                recovery="Run `kestrel doctor` and inspect the process manually.",
            )
        uid_text, pgid_text, state, *identity_parts, command_text = parts
        birth_marker = self._birth_marker(pid, " ".join(identity_parts))
        try:
            uid = int(uid_text)
            pgid = int(pgid_text)
        except ValueError as exc:
            raise ServiceControlError(
                f"Could not parse process ownership for PID {pid}.",
                code="process_inspection_failed",
                recovery="Run `kestrel doctor` and inspect the process manually.",
            ) from exc
        try:
            command = tuple(shlex.split(command_text))
        except ValueError:
            command = (command_text,)
        try:
            cwd = self._process_cwd(pid)
        except ServiceControlError:
            verification = subprocess.run(
                ["ps", "-p", str(pid), "-o", "pid="],
                check=False,
                capture_output=True,
                text=True,
            )
            if verification.returncode != 0 or not verification.stdout.strip():
                return None
            raise
        return ProcessSnapshot(
            pid=pid,
            uid=uid,
            cwd=cwd,
            command=command,
            pgid=pgid,
            state=state,
            birth_marker=birth_marker,
        )

    def listeners(self, host: str, port: int) -> tuple[BoundListener, ...]:
        if host != "127.0.0.1":
            raise ServiceControlError(
                "Easy-launch listener inspection is restricted to loopback.",
                code="unsafe_host",
                recovery="Use the advanced `nest-agent server` command for non-loopback serving.",
            )
        try:
            result = subprocess.run(
                [
                    "lsof",
                    "-nP",
                    f"-iTCP:{port}",
                    "-sTCP:LISTEN",
                    "-Fpn",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ServiceControlError(
                "The `lsof` process-inspection tool is unavailable.",
                code="process_inspection_unavailable",
                recovery="Install `lsof` or inspect the loopback listener manually.",
            ) from exc
        if result.returncode not in {0, 1}:
            raise ServiceControlError(
                f"Could not inspect loopback port {port}.",
                code="port_inspection_failed",
                recovery="Run `kestrel doctor` and inspect the listener manually.",
            )
        listeners: set[BoundListener] = set()
        current_pid: int | None = None
        current_has_endpoint = False
        for line in result.stdout.splitlines():
            if not line:
                continue
            field, value = line[0], line[1:]
            if field == "p":
                if current_pid is not None and not current_has_endpoint:
                    raise ServiceControlError(
                        f"Received an incomplete listener record for port {port}.",
                        code="port_inspection_failed",
                        recovery="Run `kestrel doctor` and inspect the listener manually.",
                    )
                if not value.isdigit() or int(value) <= 0:
                    raise ServiceControlError(
                        f"Received an invalid listener PID for port {port}.",
                        code="port_inspection_failed",
                        recovery="Run `kestrel doctor` and inspect the listener manually.",
                    )
                current_pid = int(value)
                current_has_endpoint = False
                continue
            if field != "n" or current_pid is None:
                continue
            endpoint = _parse_listener_endpoint(value)
            if endpoint is None:
                raise ServiceControlError(
                    f"Could not parse a listener endpoint for port {port}.",
                    code="port_inspection_failed",
                    recovery="Run `kestrel doctor` and inspect the listener manually.",
                )
            endpoint_host, endpoint_port = endpoint
            if endpoint_port != port:
                raise ServiceControlError(
                    f"Received an unexpected listener endpoint for port {port}.",
                    code="port_inspection_failed",
                    recovery="Run `kestrel doctor` and inspect the listener manually.",
                )
            listeners.add(
                BoundListener(
                    pid=current_pid,
                    host=endpoint_host,
                    port=endpoint_port,
                )
            )
            current_has_endpoint = True
        if current_pid is not None and not current_has_endpoint:
            raise ServiceControlError(
                f"Received an incomplete listener record for port {port}.",
                code="port_inspection_failed",
                recovery="Run `kestrel doctor` and inspect the listener manually.",
            )
        return tuple(
            sorted(
                listeners,
                key=lambda item: (item.pid, item.host, item.port),
            )
        )

    def group_has_live_members(self, pgid: int) -> bool:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pgid=", "-o", "state="],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ServiceControlError(
                f"Could not inspect process group {pgid}.",
                code="process_inspection_failed",
                recovery="Run `kestrel doctor` and inspect the process group manually.",
            )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 2 or not parts[0].isdigit():
                continue
            if int(parts[0]) == pgid and not parts[1].startswith("Z"):
                return True
        return False

    def port_is_bindable(self, host: str, port: int) -> bool:
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            candidate.bind((host, port))
        except OSError:
            return False
        finally:
            candidate.close()
        return True

    def _process_cwd(self, pid: int) -> Path:
        proc_cwd = Path("/proc") / str(pid) / "cwd"
        try:
            return Path(os.readlink(proc_cwd)).resolve()
        except (FileNotFoundError, OSError):
            pass
        try:
            result = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ServiceControlError(
                f"Cannot resolve the working directory for PID {pid}.",
                code="process_inspection_unavailable",
                recovery="Install `lsof` or inspect the process manually.",
            ) from exc
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("n") and len(line) > 1:
                    return Path(line[1:]).resolve()
        raise ServiceControlError(
            f"Cannot resolve the working directory for PID {pid}.",
            code="process_inspection_failed",
            recovery="Run `kestrel doctor` and inspect the process manually.",
        )

    def _birth_marker(self, pid: int, fallback: str) -> str:
        """Return the highest-resolution creation marker the OS exposes."""
        proc_stat = Path("/proc") / str(pid) / "stat"
        try:
            remainder = proc_stat.read_text(encoding="ascii").rsplit(") ", 1)[1]
            fields = remainder.split()
            start_ticks = fields[19]
            if start_ticks.isdigit():
                return f"proc-start-ticks:{start_ticks}"
        except (FileNotFoundError, IndexError, OSError, UnicodeDecodeError):
            pass
        if fallback:
            return f"ps-lstart:{fallback}"
        raise ServiceControlError(
            f"Could not determine a stable process birth marker for PID {pid}.",
            code="process_inspection_failed",
            recovery="Run `kestrel doctor` and inspect the process manually.",
        )


def resolve_kestrel_home(
    *,
    explicit_home: str | Path | None,
    environ: Mapping[str, str] | None = None,
    embedded_home: str | Path | None = None,
    cwd: Path | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    working_directory = (cwd or Path.cwd()).resolve()
    selected: str | Path | None
    source: str
    if explicit_home is not None:
        selected = explicit_home
        source = "--home"
    elif environment.get("KESTREL_HOME", "").strip():
        selected = environment["KESTREL_HOME"].strip()
        source = "KESTREL_HOME"
    elif embedded_home is not None:
        selected = embedded_home
        source = "installed launcher"
    elif _is_kestrel_installation(working_directory):
        return working_directory
    else:
        user_home = environment.get("HOME", "").strip()
        if not user_home:
            user_home = str(Path.home())
        selected = Path(user_home) / ".kestrel-agent"
        source = "default installation"
    candidate = _absolute_home(selected, base=working_directory)
    if not _is_kestrel_installation(candidate):
        raise ValueError(
            f"{source} does not identify a complete Kestrel installation: {candidate}"
        )
    return candidate


def resolve_service_paths(
    home: str | Path,
    *,
    port: int | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ServicePaths:
    environment = os.environ if environ is None else environ
    canonical_home = _absolute_home(home, base=Path.cwd())
    if not _is_kestrel_installation(canonical_home):
        raise ValueError(
            f"Path does not identify a complete Kestrel installation: {canonical_home}"
        )
    configured_port: int | str = (
        port
        if port is not None
        else environment.get("KESTREL_PORT", "8765").strip() or "8765"
    )
    resolved_port = _positive_port(configured_port)
    nest_dir = canonical_home / ".nest"
    state_path = _configured_path(
        environment.get("NEST_AGENT_STATE_PATH"),
        default=nest_dir / "state" / "agent.db",
        home=canonical_home,
    )
    memory_dir = _configured_path(
        environment.get("NEST_AGENT_MEMORY_DIR"),
        default=nest_dir / "memory",
        home=canonical_home,
    )
    return ServicePaths(
        home=canonical_home,
        state_path=state_path,
        memory_dir=memory_dir,
        log_path=nest_dir / "server.log",
        pid_path=nest_dir / "server.pid",
        supervisor_pid_path=nest_dir / "server.supervisor.pid",
        pgid_path=nest_dir / "server.pgid",
        lifecycle_lock_path=nest_dir / "server.lifecycle.lock",
        supervisor_script=canonical_home
        / "scripts"
        / "installer-server-supervisor.sh",
        server_executable=canonical_home / ".venv" / "bin" / "nest-agent",
        host="127.0.0.1",
        port=resolved_port,
        url=f"http://127.0.0.1:{resolved_port}/",
    )


class ServiceController:
    def __init__(
        self,
        paths: ServicePaths,
        *,
        inspector: ProcessInspector | None = None,
        client: ProbeClient | None = None,
        signaler: ProcessSignaler | None = None,
        popen: Callable[..., object] = subprocess.Popen,
    ) -> None:
        self.paths = paths
        self.inspector = inspector or SystemProcessInspector()
        self.client = client or KestrelServerClient(paths.url)
        self.signaler = signaler or SystemProcessSignaler()
        self.popen = popen

    def status(self) -> ServiceStatus:
        try:
            lifecycle_busy = _lifecycle_lock_busy(self.paths.lifecycle_lock_path)
            metadata = self._metadata()
            return self._status_from_inspection(
                metadata,
                lifecycle_busy=lifecycle_busy,
            )
        except ServiceControlError as exc:
            return ServiceStatus(
                state=ServiceState.CONFLICT,
                management=ServiceManagement.NONE,
                url=self.paths.url,
                pid=None,
                supervisor_pid=None,
                pgid=None,
                detail=f"{exc} {exc.recovery}",
            )
        except OSError as exc:
            return ServiceStatus(
                state=ServiceState.CONFLICT,
                management=ServiceManagement.NONE,
                url=self.paths.url,
                pid=None,
                supervisor_pid=None,
                pgid=None,
                detail=(
                    f"Service ownership inspection failed: {exc}. "
                    "Run `kestrel doctor` and inspect the listener manually."
                ),
            )

    def start(
        self,
        *,
        readiness_timeout: float = 15.0,
        lifecycle_lock_timeout: float = 5.0,
        poll_interval: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> ServiceStatus:
        readiness = _bounded_seconds(
            "readiness timeout",
            readiness_timeout,
            allow_zero=True,
        )
        interval = _bounded_seconds(
            "poll interval",
            poll_interval,
            allow_zero=False,
        )
        with _exclusive_lifecycle_lock(
            self.paths.lifecycle_lock_path,
            timeout_seconds=lifecycle_lock_timeout,
            poll_interval=interval,
            clock=clock,
            sleep=sleep,
        ):
            current = self._locked_status()
            if current.state == ServiceState.RUNNING:
                return current
            if current.state == ServiceState.CONFLICT:
                raise _status_conflict(current)
            if current.state == ServiceState.STARTING:
                return self._wait_for_running(
                    timeout_seconds=readiness,
                    poll_interval=interval,
                    clock=clock,
                    sleep=sleep,
                    existing_startup=True,
                )
            self._remove_proven_stale_metadata()
            self._prepare_service_artifacts()
            prelaunch = self._locked_status()
            if prelaunch.state == ServiceState.RUNNING:
                return prelaunch
            if prelaunch.state == ServiceState.CONFLICT:
                raise _status_conflict(prelaunch)
            if prelaunch.state == ServiceState.STARTING:
                return self._wait_for_running(
                    timeout_seconds=readiness,
                    poll_interval=interval,
                    clock=clock,
                    sleep=sleep,
                    existing_startup=True,
                )
            command = self._supervisor_command()
            try:
                launched = self.popen(
                    command,
                    cwd=self.paths.home,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                    shell=False,
                )
            except OSError as exc:
                raise ServiceControlError(
                    f"Could not launch the Kestrel supervisor: {exc}",
                    code="launch_failed",
                    recovery=(
                        f"Inspect {self.paths.log_path} and run `kestrel doctor`."
                    ),
                ) from exc
            launched_pid = getattr(launched, "pid", None)
            if not isinstance(launched_pid, int) or launched_pid <= 0:
                raise ServiceControlError(
                    "The Kestrel supervisor did not return a valid process ID.",
                    code="launch_failed",
                    recovery=(
                        f"Inspect {self.paths.log_path} and run `kestrel doctor`."
                    ),
                )
            try:
                return self._wait_for_running(
                    timeout_seconds=readiness,
                    poll_interval=interval,
                    clock=clock,
                    sleep=sleep,
                    existing_startup=False,
                )
            except ServiceControlError as startup_error:
                cleanup_error = self._cleanup_failed_start(
                    launched_pid,
                    timeout_seconds=max(1.0, interval * 10),
                    poll_interval=interval,
                    clock=clock,
                    sleep=sleep,
                )
                if cleanup_error is not None:
                    raise cleanup_error from startup_error
                raise ServiceControlError(
                    f"Kestrel did not become ready: {startup_error}",
                    code="startup_failed",
                    recovery=(
                        f"Inspect {self.paths.log_path}, then run `kestrel doctor`."
                    ),
                ) from startup_error

    def stop(
        self,
        *,
        grace_timeout: float = 5.0,
        kill_timeout: float = 3.0,
        lifecycle_lock_timeout: float = 5.0,
        poll_interval: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> ServiceStatus:
        grace = _bounded_seconds(
            "grace timeout",
            grace_timeout,
            allow_zero=True,
        )
        hard_kill = _bounded_seconds(
            "kill timeout",
            kill_timeout,
            allow_zero=True,
        )
        interval = _bounded_seconds(
            "poll interval",
            poll_interval,
            allow_zero=False,
        )
        with _exclusive_lifecycle_lock(
            self.paths.lifecycle_lock_path,
            timeout_seconds=lifecycle_lock_timeout,
            poll_interval=interval,
            clock=clock,
            sleep=sleep,
        ):
            current = self._locked_status()
            if current.state == ServiceState.CONFLICT:
                raise _status_conflict(current)
            if current.state == ServiceState.STOPPED:
                self._remove_proven_stale_metadata()
                return self._stopped_status()
            if current.management == ServiceManagement.EXTERNAL:
                self._stop_external(
                    current,
                    grace_timeout=grace,
                    kill_timeout=hard_kill,
                    poll_interval=interval,
                    clock=clock,
                    sleep=sleep,
                )
            elif current.management == ServiceManagement.MANAGED:
                self._stop_managed(
                    current,
                    grace_timeout=grace,
                    kill_timeout=hard_kill,
                    poll_interval=interval,
                    clock=clock,
                    sleep=sleep,
                )
            else:
                raise ServiceControlError(
                    "Kestrel process ownership could not be verified for stop.",
                    code="service_conflict",
                    recovery="Run `kestrel doctor`; do not signal the process manually until ownership is proven.",
                )
            self._remove_proven_stale_metadata()
            if not self.inspector.port_is_bindable(
                self.paths.host,
                self.paths.port,
            ):
                raise ServiceControlError(
                    "The Kestrel process exited, but the loopback port is still occupied.",
                    code="termination_incomplete",
                    recovery="Run `kestrel doctor` and inspect the new listener; no further process was signalled.",
                )
            return self._stopped_status()

    def _locked_status(self) -> ServiceStatus:
        try:
            return self._status_from_inspection(
                self._metadata(),
                lifecycle_busy=False,
            )
        except ServiceControlError:
            raise
        except OSError as exc:
            raise ServiceControlError(
                f"Service ownership inspection failed: {exc}",
                code="process_inspection_failed",
                recovery="Run `kestrel doctor` and inspect the loopback listener manually.",
            ) from exc

    def _wait_for_running(
        self,
        *,
        timeout_seconds: float,
        poll_interval: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        existing_startup: bool,
    ) -> ServiceStatus:
        deadline = clock() + timeout_seconds
        while True:
            status = self._locked_status()
            if status.state == ServiceState.RUNNING:
                return status
            if status.state == ServiceState.CONFLICT:
                raise _status_conflict(status)
            remaining = deadline - clock()
            if remaining <= 0:
                qualifier = "existing " if existing_startup else ""
                raise ServiceControlError(
                    f"The {qualifier}Kestrel startup did not reach API readiness.",
                    code="startup_timeout",
                    recovery=(
                        f"Inspect {self.paths.log_path} and run `kestrel doctor`."
                    ),
                )
            sleep(min(poll_interval, remaining))

    def _prepare_service_artifacts(self) -> None:
        if not self.paths.supervisor_script.is_file():
            raise ServiceControlError(
                f"Kestrel supervisor script is missing: {self.paths.supervisor_script}",
                code="installation_incomplete",
                recovery="Repair or reinstall Kestrel before starting the service.",
            )
        if (
            not self.paths.server_executable.is_file()
            or not os.access(self.paths.server_executable, os.X_OK)
        ):
            raise ServiceControlError(
                f"Kestrel server executable is unavailable: {self.paths.server_executable}",
                code="installation_incomplete",
                recovery="Repair or reinstall the Kestrel virtual environment.",
            )
        for directory in {
            self.paths.lifecycle_lock_path.parent,
            self.paths.memory_dir,
            self.paths.state_path.parent,
            self.paths.log_path.parent,
            self.paths.pid_path.parent,
            self.paths.supervisor_pid_path.parent,
            self.paths.pgid_path.parent,
        }:
            try:
                ensure_owner_only_directory(directory)
            except ValueError as exc:
                raise ServiceControlError(
                    f"Cannot secure Kestrel lifecycle directory {directory}: {exc}",
                    code="unsafe_metadata",
                    recovery="Inspect the path manually and remove any symlink or foreign-owned directory.",
                ) from exc
        create_private_empty_file(self.paths.log_path)

    def _supervisor_command(self) -> list[str]:
        server_command = [
            str(self.paths.server_executable),
            "server",
            "--backend",
            "memvid",
            "--memory-dir",
            str(self.paths.memory_dir),
            "--state-path",
            str(self.paths.state_path),
            "--provider",
            "mock",
            "--model",
            "mock",
            "--host",
            self.paths.host,
            "--port",
            str(self.paths.port),
        ]
        return [
            "bash",
            str(self.paths.supervisor_script),
            "--pid-file",
            str(self.paths.pid_path),
            "--supervisor-pid-file",
            str(self.paths.supervisor_pid_path),
            "--process-group-file",
            str(self.paths.pgid_path),
            "--log-file",
            str(self.paths.log_path),
            "--",
            *server_command,
        ]

    def _remove_proven_stale_metadata(self) -> None:
        metadata = self._metadata()
        if metadata.pid is not None and self.inspector.process(metadata.pid) is not None:
            raise ServiceControlError(
                "Recorded Kestrel server PID is still live.",
                code="metadata_in_use",
                recovery="Run `kestrel doctor` and verify process ownership before retrying.",
            )
        if (
            metadata.supervisor_pid is not None
            and self.inspector.process(metadata.supervisor_pid) is not None
        ):
            raise ServiceControlError(
                "Recorded Kestrel supervisor PID is still live.",
                code="metadata_in_use",
                recovery="Run `kestrel doctor` and verify process ownership before retrying.",
            )
        if (
            metadata.pgid is not None
            and self.inspector.group_has_live_members(metadata.pgid)
        ):
            raise ServiceControlError(
                "Recorded Kestrel process group is still live.",
                code="metadata_in_use",
                recovery="Run `kestrel doctor` and verify process ownership before retrying.",
            )
        if self.inspector.listeners(self.paths.host, self.paths.port):
            raise ServiceControlError(
                "The Kestrel loopback port still has a listener.",
                code="metadata_in_use",
                recovery="Run `kestrel doctor` and verify listener ownership before retrying.",
            )
        for path, expected in (
            (self.paths.pid_path, metadata.pid),
            (self.paths.supervisor_pid_path, metadata.supervisor_pid),
            (self.paths.pgid_path, metadata.pgid),
        ):
            if expected is not None:
                _unlink_private_identifier(path, expected)

    def _cleanup_failed_start(
        self,
        launched_pid: int,
        *,
        timeout_seconds: float,
        poll_interval: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> ServiceControlError | None:
        metadata = self._metadata()
        expected_supervisor = self.inspector.process(launched_pid)
        if metadata.supervisor_pid is not None:
            if metadata.supervisor_pid != launched_pid:
                return ServiceControlError(
                    "Startup cleanup could not prove supervisor ownership; evidence was preserved.",
                    code="cleanup_indeterminate",
                    recovery="Run `kestrel doctor` and inspect the recorded supervisor before taking action.",
                )
            if expected_supervisor is not None and not _supervisor_identity_matches(
                expected_supervisor,
                self.paths,
            ):
                return ServiceControlError(
                    "Startup cleanup detected a changed supervisor identity; evidence was preserved.",
                    code="cleanup_indeterminate",
                    recovery="Run `kestrel doctor`; do not signal the reused PID.",
                )
        if (
            metadata.pgid is not None
            and self.inspector.group_has_live_members(metadata.pgid)
        ):
            server = (
                self.inspector.process(metadata.pid)
                if metadata.pid is not None
                else None
            )
            if (
                server is None
                or not _server_identity_matches(
                    server,
                    self.paths,
                    managed=True,
                )
                or server.pgid != metadata.pgid
            ):
                return ServiceControlError(
                    "Startup cleanup could not verify the new server process group; evidence was preserved.",
                    code="cleanup_indeterminate",
                    recovery="Run `kestrel doctor`; do not signal the process group manually.",
                )
            try:
                self.signaler.signal_group(
                    metadata.pgid,
                    required_signal("SIGTERM"),
                )
            except ProcessLookupError:
                pass
        else:
            if expected_supervisor is not None:
                if not _supervisor_identity_matches(expected_supervisor, self.paths):
                    return ServiceControlError(
                        "Startup cleanup detected a reused supervisor PID; evidence was preserved.",
                        code="cleanup_indeterminate",
                        recovery="Run `kestrel doctor`; do not signal the reused PID.",
                    )
                try:
                    self.signaler.signal_pid(
                        launched_pid,
                        required_signal("SIGTERM"),
                    )
                except ProcessLookupError:
                    pass
        stopped = _wait_until(
            lambda: self._startup_processes_absent(metadata, launched_pid),
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            clock=clock,
            sleep=sleep,
        )
        if not stopped:
            current_metadata = self._metadata()
            current_supervisor = self.inspector.process(launched_pid)
            if (
                current_metadata != metadata
                or expected_supervisor is None
                or current_supervisor is None
                or not _same_process_identity(expected_supervisor, current_supervisor)
                or not _supervisor_identity_matches(current_supervisor, self.paths)
            ):
                return ServiceControlError(
                    "Startup cleanup detected changed process evidence; it was preserved.",
                    code="cleanup_indeterminate",
                    recovery="Run `kestrel doctor`; no reused process was signalled.",
                )
            try:
                if metadata.pgid is not None and self.inspector.group_has_live_members(metadata.pgid):
                    self.signaler.signal_group(metadata.pgid, required_signal("SIGKILL"))
                else:
                    self.signaler.signal_pid(launched_pid, required_signal("SIGKILL"))
            except ProcessLookupError:
                pass
            if not _wait_until(
                lambda: self._startup_processes_absent(metadata, launched_pid),
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
                clock=clock,
                sleep=sleep,
            ):
                return ServiceControlError(
                    "The verified startup process survived cleanup; lifecycle evidence was preserved.",
                    code="cleanup_indeterminate",
                    recovery="Run `kestrel doctor` and inspect the service log before taking action.",
                )
        try:
            self._remove_proven_stale_metadata()
        except ServiceControlError as exc:
            return ServiceControlError(
                f"Startup processes exited, but cleanup proof failed: {exc}",
                code="cleanup_indeterminate",
                recovery="Run `kestrel doctor`; lifecycle evidence was preserved.",
            )
        return None

    def _startup_processes_absent(
        self,
        metadata: _ServiceMetadata,
        launched_pid: int,
    ) -> bool:
        if self.inspector.process(launched_pid) is not None:
            return False
        if (
            metadata.pid is not None
            and self.inspector.process(metadata.pid) is not None
        ):
            return False
        if (
            metadata.pgid is not None
            and self.inspector.group_has_live_members(metadata.pgid)
        ):
            return False
        return not self.inspector.listeners(
            self.paths.host,
            self.paths.port,
        )

    def _stop_external(
        self,
        status: ServiceStatus,
        *,
        grace_timeout: float,
        kill_timeout: float,
        poll_interval: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        if status.pid is None:
            raise ServiceControlError(
                "Verified external service has no process ID.",
                code="service_conflict",
                recovery="Run `kestrel doctor`; no process was signalled.",
            )
        external_pid = status.pid
        expected = self.inspector.process(external_pid)
        if expected is None or not _server_identity_matches(
            expected,
            self.paths,
            managed=False,
        ):
            raise _identity_changed(external_pid)
        try:
            self.signaler.signal_pid(
                external_pid,
                required_signal("SIGTERM"),
            )
        except ProcessLookupError:
            pass
        if _wait_until(
            lambda: self._external_absent(external_pid),
            timeout_seconds=grace_timeout,
            poll_interval=poll_interval,
            clock=clock,
            sleep=sleep,
        ):
            return
        current = self.inspector.process(external_pid)
        if (
            current is None
            or not _same_process_identity(expected, current)
            or not _server_identity_matches(
                current,
                self.paths,
                managed=False,
            )
        ):
            raise _identity_changed(external_pid)
        try:
            self.signaler.signal_pid(
                external_pid,
                required_signal("SIGKILL"),
            )
        except ProcessLookupError:
            pass
        if not _wait_until(
            lambda: self._external_absent(external_pid),
            timeout_seconds=kill_timeout,
            poll_interval=poll_interval,
            clock=clock,
            sleep=sleep,
        ):
            raise ServiceControlError(
                "The verified external Kestrel process survived termination.",
                code="termination_failed",
                recovery="Run `kestrel doctor`; no other process was signalled.",
            )

    def _stop_managed(
        self,
        status: ServiceStatus,
        *,
        grace_timeout: float,
        kill_timeout: float,
        poll_interval: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        metadata = self._metadata()
        if (
            metadata.pgid is not None
            and metadata.pid is not None
            and self.inspector.group_has_live_members(metadata.pgid)
        ):
            expected = self.inspector.process(metadata.pid)
            if (
                expected is None
                or expected.pgid != metadata.pgid
                or not _server_identity_matches(
                    expected,
                    self.paths,
                    managed=True,
                )
            ):
                raise _identity_changed(metadata.pid)
            try:
                self.signaler.signal_group(
                    metadata.pgid,
                    required_signal("SIGTERM"),
                )
            except ProcessLookupError:
                pass
            if _wait_until(
                lambda: self._managed_absent(metadata),
                timeout_seconds=grace_timeout,
                poll_interval=poll_interval,
                clock=clock,
                sleep=sleep,
            ):
                return
            current_metadata = self._metadata()
            current = self.inspector.process(metadata.pid)
            if (
                current_metadata != metadata
                or current is None
                or not _same_process_identity(expected, current)
                or current.pgid != metadata.pgid
                or not _server_identity_matches(
                    current,
                    self.paths,
                    managed=True,
                )
            ):
                raise _identity_changed(metadata.pid)
            try:
                self.signaler.signal_group(
                    metadata.pgid,
                    required_signal("SIGKILL"),
                )
            except ProcessLookupError:
                pass
            if not _wait_until(
                lambda: self._managed_absent(metadata),
                timeout_seconds=kill_timeout,
                poll_interval=poll_interval,
                clock=clock,
                sleep=sleep,
            ):
                raise ServiceControlError(
                    "The verified Kestrel process group survived termination.",
                    code="termination_failed",
                    recovery="Run `kestrel doctor`; no unrelated process was signalled.",
                )
            return
        supervisor_pid = metadata.supervisor_pid or status.supervisor_pid
        if supervisor_pid is None:
            raise ServiceControlError(
                "Managed Kestrel startup has no verified signal target.",
                code="service_conflict",
                recovery="Run `kestrel doctor`; no process was signalled.",
            )
        expected_supervisor = self.inspector.process(supervisor_pid)
        if expected_supervisor is None or not _supervisor_identity_matches(
            expected_supervisor,
            self.paths,
        ):
            raise _identity_changed(supervisor_pid)
        try:
            self.signaler.signal_pid(
                supervisor_pid,
                required_signal("SIGTERM"),
            )
        except ProcessLookupError:
            pass
        if _wait_until(
            lambda: self.inspector.process(supervisor_pid) is None,
            timeout_seconds=grace_timeout,
            poll_interval=poll_interval,
            clock=clock,
            sleep=sleep,
        ):
            return
        current = self.inspector.process(supervisor_pid)
        if (
            current is None
            or not _same_process_identity(expected_supervisor, current)
            or not _supervisor_identity_matches(current, self.paths)
        ):
            raise _identity_changed(supervisor_pid)
        try:
            self.signaler.signal_pid(
                supervisor_pid,
                required_signal("SIGKILL"),
            )
        except ProcessLookupError:
            pass
        if not _wait_until(
            lambda: self.inspector.process(supervisor_pid) is None,
            timeout_seconds=kill_timeout,
            poll_interval=poll_interval,
            clock=clock,
            sleep=sleep,
        ):
            raise ServiceControlError(
                "The verified Kestrel supervisor survived termination.",
                code="termination_failed",
                recovery="Run `kestrel doctor`; no unrelated process was signalled.",
            )

    def _external_absent(self, pid: int) -> bool:
        return (
            self.inspector.process(pid) is None
            and pid
            not in _listener_pids(
                self.inspector.listeners(
                    self.paths.host,
                    self.paths.port,
                )
            )
        )

    def _managed_absent(self, metadata: _ServiceMetadata) -> bool:
        if (
            metadata.pid is not None
            and self.inspector.process(metadata.pid) is not None
        ):
            return False
        if (
            metadata.supervisor_pid is not None
            and self.inspector.process(metadata.supervisor_pid) is not None
        ):
            return False
        if (
            metadata.pgid is not None
            and self.inspector.group_has_live_members(metadata.pgid)
        ):
            return False
        listeners = self.inspector.listeners(
            self.paths.host,
            self.paths.port,
        )
        return (
            metadata.pid is None
            or metadata.pid not in _listener_pids(listeners)
        )

    def _stopped_status(self) -> ServiceStatus:
        return ServiceStatus(
            state=ServiceState.STOPPED,
            management=ServiceManagement.NONE,
            url=self.paths.url,
            pid=None,
            supervisor_pid=None,
            pgid=None,
            detail="Kestrel is stopped.",
        )

    def _metadata(self) -> _ServiceMetadata:
        return _ServiceMetadata(
            pid=_read_private_identifier(self.paths.pid_path),
            supervisor_pid=_read_private_identifier(
                self.paths.supervisor_pid_path
            ),
            pgid=_read_private_identifier(self.paths.pgid_path),
        )

    def _status_from_inspection(
        self,
        metadata: _ServiceMetadata,
        *,
        lifecycle_busy: bool,
    ) -> ServiceStatus:
        server = (
            self.inspector.process(metadata.pid)
            if metadata.pid is not None
            else None
        )
        supervisor = (
            self.inspector.process(metadata.supervisor_pid)
            if metadata.supervisor_pid is not None
            else None
        )
        group_live = (
            self.inspector.group_has_live_members(metadata.pgid)
            if metadata.pgid is not None
            else False
        )
        if server is not None and not _server_identity_matches(
            server,
            self.paths,
            managed=True,
        ):
            return self._conflict(
                "Recorded server PID has a mismatched process identity.",
                pid=metadata.pid,
                supervisor_pid=metadata.supervisor_pid,
                pgid=metadata.pgid,
                lifecycle_busy=lifecycle_busy,
            )
        if supervisor is not None and not _supervisor_identity_matches(
            supervisor,
            self.paths,
        ):
            return self._conflict(
                "Recorded supervisor PID has a mismatched process identity.",
                pid=metadata.pid,
                supervisor_pid=metadata.supervisor_pid,
                pgid=metadata.pgid,
                lifecycle_busy=lifecycle_busy,
            )
        if (
            group_live
            and server is not None
            and metadata.pgid != server.pgid
        ):
            return self._conflict(
                "Recorded process group does not match the verified server.",
                pid=metadata.pid,
                supervisor_pid=metadata.supervisor_pid,
                pgid=metadata.pgid,
                lifecycle_busy=lifecycle_busy,
            )
        listeners = self.inspector.listeners(
            self.paths.host,
            self.paths.port,
        )
        if any(
            not isinstance(listener, BoundListener)
            or listener.host != self.paths.host
            or listener.port != self.paths.port
            for listener in listeners
        ):
            return self._conflict(
                "The configured port is not bound exactly to the Kestrel "
                "loopback endpoint.",
                lifecycle_busy=lifecycle_busy,
            )
        if len(listeners) > 1:
            return self._conflict(
                "Multiple processes report ownership of the Kestrel loopback port.",
                lifecycle_busy=lifecycle_busy,
            )
        listener_pid = listeners[0].pid if listeners else None
        if listener_pid is None:
            return self._status_without_listener(
                metadata,
                server=server,
                supervisor=supervisor,
                group_live=group_live,
                lifecycle_busy=lifecycle_busy,
            )
        listener = self.inspector.process(listener_pid)
        if listener is None or not _server_identity_matches(
            listener,
            self.paths,
            managed=False,
        ):
            return self._conflict(
                "The configured port listener is not a verified Kestrel server.",
                pid=listener_pid,
                lifecycle_busy=lifecycle_busy,
            )
        if metadata.pid is not None and metadata.pid != listener_pid:
            return self._conflict(
                "The listener PID does not match the recorded Kestrel server PID.",
                pid=listener_pid,
                supervisor_pid=metadata.supervisor_pid,
                pgid=metadata.pgid,
                lifecycle_busy=lifecycle_busy,
            )
        probe = self.client.probe()
        verified_api = probe.healthy or probe.locked
        if not verified_api:
            if supervisor is not None:
                return ServiceStatus(
                    state=ServiceState.STARTING,
                    management=ServiceManagement.MANAGED,
                    url=self.paths.url,
                    pid=listener_pid,
                    supervisor_pid=supervisor.pid,
                    pgid=metadata.pgid,
                    detail=_with_lifecycle(
                        "Verified Kestrel processes exist, but the API is not ready.",
                        lifecycle_busy,
                    ),
                    lifecycle_busy=lifecycle_busy,
                )
            return self._conflict(
                "A matching process owns the port, but its Kestrel API could not be verified.",
                pid=listener_pid,
                lifecycle_busy=lifecycle_busy,
            )
        if not metadata.present:
            return ServiceStatus(
                state=ServiceState.RUNNING,
                management=ServiceManagement.EXTERNAL,
                url=self.paths.url,
                pid=listener_pid,
                supervisor_pid=None,
                pgid=listener.pgid,
                detail=_with_lifecycle(
                    "Verified external Kestrel service is running.",
                    lifecycle_busy,
                ),
                lifecycle_busy=lifecycle_busy,
            )
        fully_managed = (
            metadata.pid == listener_pid
            and metadata.supervisor_pid is not None
            and supervisor is not None
            and metadata.pgid == listener.pgid
            and group_live
        )
        if not fully_managed:
            return self._conflict(
                "Kestrel lifecycle metadata is incomplete or does not match the listener.",
                pid=listener_pid,
                supervisor_pid=metadata.supervisor_pid,
                pgid=metadata.pgid,
                lifecycle_busy=lifecycle_busy,
            )
        assert supervisor is not None
        detail = (
            "Verified managed Kestrel service is running; API access is locked."
            if probe.locked
            else "Verified managed Kestrel service is running."
        )
        return ServiceStatus(
            state=ServiceState.RUNNING,
            management=ServiceManagement.MANAGED,
            url=self.paths.url,
            pid=listener_pid,
            supervisor_pid=supervisor.pid,
            pgid=metadata.pgid,
            detail=_with_lifecycle(detail, lifecycle_busy),
            lifecycle_busy=lifecycle_busy,
        )

    def _status_without_listener(
        self,
        metadata: _ServiceMetadata,
        *,
        server: ProcessSnapshot | None,
        supervisor: ProcessSnapshot | None,
        group_live: bool,
        lifecycle_busy: bool,
    ) -> ServiceStatus:
        if supervisor is not None:
            return ServiceStatus(
                state=ServiceState.STARTING,
                management=ServiceManagement.MANAGED,
                url=self.paths.url,
                pid=server.pid if server is not None else metadata.pid,
                supervisor_pid=supervisor.pid,
                pgid=metadata.pgid,
                detail=_with_lifecycle(
                    "Verified Kestrel supervisor is waiting for API readiness.",
                    lifecycle_busy,
                ),
                lifecycle_busy=lifecycle_busy,
            )
        if server is not None or group_live:
            return self._conflict(
                "Kestrel lifecycle processes exist without a verified port listener.",
                pid=server.pid if server is not None else metadata.pid,
                supervisor_pid=metadata.supervisor_pid,
                pgid=metadata.pgid,
                lifecycle_busy=lifecycle_busy,
            )
        if not self.inspector.port_is_bindable(
            self.paths.host,
            self.paths.port,
        ):
            return self._conflict(
                "The configured loopback port is occupied by an unidentifiable listener.",
                lifecycle_busy=lifecycle_busy,
            )
        stale = metadata.present
        detail = (
            "Kestrel is stopped; safe lifecycle metadata is stale."
            if stale
            else "Kestrel is stopped."
        )
        return ServiceStatus(
            state=ServiceState.STOPPED,
            management=ServiceManagement.NONE,
            url=self.paths.url,
            pid=None,
            supervisor_pid=None,
            pgid=None,
            detail=_with_lifecycle(detail, lifecycle_busy),
            lifecycle_busy=lifecycle_busy,
        )

    def _conflict(
        self,
        detail: str,
        *,
        pid: int | None = None,
        supervisor_pid: int | None = None,
        pgid: int | None = None,
        lifecycle_busy: bool = False,
    ) -> ServiceStatus:
        return ServiceStatus(
            state=ServiceState.CONFLICT,
            management=ServiceManagement.NONE,
            url=self.paths.url,
            pid=pid,
            supervisor_pid=supervisor_pid,
            pgid=pgid,
            detail=_with_lifecycle(
                f"{detail} Run `kestrel doctor`; no process was signalled.",
                lifecycle_busy,
            ),
            lifecycle_busy=lifecycle_busy,
        )


@dataclass(frozen=True)
class _ServiceMetadata:
    pid: int | None
    supervisor_pid: int | None
    pgid: int | None

    @property
    def present(self) -> bool:
        return any(
            value is not None
            for value in (self.pid, self.supervisor_pid, self.pgid)
        )


def _read_private_identifier(path: Path) -> int | None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    _validate_private_metadata(before, path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        after = os.lstat(path)
        _validate_private_metadata(opened, path)
        _validate_private_metadata(after, path)
        if (
            not os.path.samestat(before, opened)
            or not os.path.samestat(opened, after)
        ):
            raise ServiceControlError(
                f"Lifecycle metadata changed during inspection: {path}",
                code="unsafe_metadata",
                recovery="Inspect the file manually and remove it only after proving process absence.",
            )
        raw = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(raw) > 32:
        raise _unsafe_metadata(path, "is oversized")
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise _unsafe_metadata(path, "is not ASCII") from None
    if not value.isdigit() or int(value) <= 0:
        raise _unsafe_metadata(path, "does not contain a positive identifier")
    return int(value)


def _validate_private_metadata(metadata: os.stat_result, path: Path) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise _unsafe_metadata(path, "is not a regular file")
    if metadata.st_uid != _current_uid():
        raise _unsafe_metadata(path, "is not owned by the current user")
    if metadata.st_nlink != 1:
        raise _unsafe_metadata(path, "has multiple hard links")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise _unsafe_metadata(path, "is not mode 0600")
    if metadata.st_size > 32:
        raise _unsafe_metadata(path, "is oversized")


def _unsafe_metadata(path: Path, reason: str) -> ServiceControlError:
    return ServiceControlError(
        f"Lifecycle metadata {path} {reason}.",
        code="unsafe_metadata",
        recovery="Inspect it manually and remove it only after proving process absence.",
    )


def _lifecycle_lock_busy(path: Path) -> bool:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return False
    _validate_private_metadata(before, path)
    flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        opened = os.fstat(handle.fileno())
        after = os.lstat(path)
        _validate_private_metadata(opened, path)
        _validate_private_metadata(after, path)
        if (
            not os.path.samestat(before, opened)
            or not os.path.samestat(opened, after)
        ):
            raise _unsafe_metadata(path, "changed during inspection")
        try:
            lock_exclusive(handle, blocking=False)
        except BlockingIOError:
            return True
        unlock(handle)
        return False
    finally:
        handle.close()


def _server_identity_matches(
    snapshot: ProcessSnapshot,
    paths: ServicePaths,
    *,
    managed: bool,
) -> bool:
    if (
        snapshot.uid != _current_uid()
        or snapshot.cwd.resolve() != paths.home
        or snapshot.state.startswith("Z")
    ):
        return False
    command = snapshot.command
    if not _contains_path(command, paths.server_executable):
        return False
    try:
        server_index = command.index("server")
    except ValueError:
        return False
    executable_index = next(
        (
            index
            for index, argument in enumerate(command)
            if _argument_is_path(argument, paths.server_executable)
        ),
        -1,
    )
    if executable_index < 0 or server_index <= executable_index:
        return False
    if _command_option(command, "--backend") != "memvid":
        return False
    if not _option_path_matches(
        command,
        "--memory-dir",
        paths.memory_dir,
        cwd=snapshot.cwd,
    ):
        return False
    if not _option_path_matches(
        command,
        "--state-path",
        paths.state_path,
        cwd=snapshot.cwd,
    ):
        return False
    if _command_option(command, "--host") != paths.host:
        return False
    if _command_option(command, "--port") != str(paths.port):
        return False
    if managed and (
        _command_option(command, "--provider") != "mock"
        or _command_option(command, "--model") != "mock"
    ):
        return False
    return True


def _supervisor_identity_matches(
    snapshot: ProcessSnapshot,
    paths: ServicePaths,
) -> bool:
    if (
        snapshot.uid != _current_uid()
        or snapshot.cwd.resolve() != paths.home
        or snapshot.state.startswith("Z")
        or not _contains_path(snapshot.command, paths.supervisor_script)
    ):
        return False
    required_paths = (
        ("--pid-file", paths.pid_path),
        ("--supervisor-pid-file", paths.supervisor_pid_path),
        ("--process-group-file", paths.pgid_path),
        ("--log-file", paths.log_path),
    )
    if any(
        not _option_path_matches(
            snapshot.command,
            option,
            expected,
            cwd=snapshot.cwd,
        )
        for option, expected in required_paths
    ):
        return False
    try:
        separator = snapshot.command.index("--")
    except ValueError:
        return False
    child_command = snapshot.command[separator + 1 :]
    child = ProcessSnapshot(
        pid=snapshot.pid,
        uid=snapshot.uid,
        cwd=snapshot.cwd,
        command=child_command,
        pgid=snapshot.pgid,
        state=snapshot.state,
        birth_marker=snapshot.birth_marker,
    )
    return _server_identity_matches(child, paths, managed=True)


def _command_option(command: tuple[str, ...], option: str) -> str | None:
    values: list[str] = []
    for index, argument in enumerate(command):
        if argument == option:
            if index + 1 >= len(command):
                return None
            values.append(command[index + 1])
        elif argument.startswith(f"{option}="):
            values.append(argument[len(option) + 1 :])
    return values[0] if len(values) == 1 else None


def _option_path_matches(
    command: tuple[str, ...],
    option: str,
    expected: Path,
    *,
    cwd: Path,
) -> bool:
    value = _command_option(command, option)
    if value is None:
        return False
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve() == expected.resolve()


def _contains_path(command: tuple[str, ...], expected: Path) -> bool:
    return any(_argument_is_path(argument, expected) for argument in command)


def _argument_is_path(argument: str, expected: Path) -> bool:
    if not argument or argument.startswith("-"):
        return False
    try:
        return Path(argument).expanduser().resolve() == expected.resolve()
    except OSError:
        return False


def _is_kestrel_installation(path: Path) -> bool:
    if not path.is_absolute() or not path.is_dir():
        return False
    supervisor = path / "scripts" / "installer-server-supervisor.sh"
    if not supervisor.is_file():
        return False
    release_runtime = path / ".venv" / "bin" / "nest-agent"
    source_runtime = (
        (path / "pyproject.toml").is_file()
        and (path / "src" / "nested_memvid_agent").is_dir()
    )
    return release_runtime.is_file() or source_runtime


def _absolute_home(value: str | Path, *, base: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return candidate.resolve(strict=False)


def _positive_port(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError("Kestrel port must be an integer from 1 through 65535")
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            "Kestrel port must be an integer from 1 through 65535"
        ) from None
    if str(value).strip() != str(port) or not 1 <= port <= 65_535:
        raise ValueError("Kestrel port must be an integer from 1 through 65535")
    return port


def _configured_path(
    configured: str | None,
    *,
    default: Path,
    home: Path,
) -> Path:
    if not configured or not configured.strip():
        return default.resolve()
    candidate = Path(configured.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = home / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(home)
        except ValueError as exc:
            raise ValueError(
                "Relative Kestrel state and memory paths must be contained by "
                "the Kestrel home"
            ) from exc
        return resolved
    return candidate.resolve()


def _parse_listener_endpoint(value: str) -> tuple[str, int] | None:
    endpoint = value.strip()
    if endpoint.startswith("["):
        closing = endpoint.find("]")
        if closing < 1 or closing + 1 >= len(endpoint) or endpoint[closing + 1] != ":":
            return None
        host = endpoint[1:closing]
        port_text = endpoint[closing + 2 :]
    else:
        if ":" not in endpoint:
            return None
        host, port_text = endpoint.rsplit(":", 1)
    if not host or not port_text.isdigit():
        return None
    port = int(port_text)
    if not 1 <= port <= 65_535:
        return None
    return host, port


def _listener_pids(listeners: tuple[BoundListener, ...]) -> tuple[int, ...]:
    return tuple(sorted({listener.pid for listener in listeners}))


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else 0


@contextmanager
def _exclusive_lifecycle_lock(
    path: Path,
    *,
    timeout_seconds: float,
    poll_interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> Iterator[None]:
    timeout = _bounded_seconds(
        "lifecycle lock timeout",
        timeout_seconds,
        allow_zero=True,
    )
    try:
        descriptor = open_private_file_descriptor(path)
    except (OSError, ValueError) as exc:
        raise ServiceControlError(
            f"Cannot open the Kestrel lifecycle lock: {exc}",
            code="unsafe_metadata",
            recovery="Inspect the lifecycle lock path and run `kestrel doctor`.",
        ) from exc
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    acquired = False
    deadline = clock() + timeout
    try:
        while True:
            try:
                lock_exclusive(handle, blocking=False)
                acquired = True
                break
            except BlockingIOError:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise ServiceControlError(
                        "Another Kestrel lifecycle command is still in progress.",
                        code="lifecycle_busy",
                        recovery="Wait for that start/open/stop command to finish, then retry.",
                    ) from None
                sleep(min(poll_interval, remaining))
        yield
    finally:
        if acquired:
            unlock(handle)
        handle.close()


def _unlink_private_identifier(path: Path, expected: int) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_descriptor = os.open(path.parent, flags)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ServiceControlError(
            f"Cannot safely open lifecycle metadata directory: {path.parent}",
            code="unsafe_metadata",
            recovery="Run `kestrel doctor`; the metadata was preserved.",
        ) from exc
    file_descriptor = -1
    try:
        try:
            before = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        _validate_private_metadata(before, path)
        file_descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(file_descriptor)
        _validate_private_metadata(opened, path)
        if not os.path.samestat(before, opened):
            raise _metadata_replaced(path)
        raw = os.read(file_descriptor, 33)
        if len(raw) > 32:
            raise _unsafe_metadata(path, "is oversized")
        try:
            current = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise _unsafe_metadata(path, "is not ASCII") from None
        if not current.isdigit() or int(current) <= 0:
            raise _unsafe_metadata(path, "does not contain a positive identifier")
        if int(current) != expected:
            raise _metadata_replaced(path)
        after = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        _validate_private_metadata(after, path)
        if not os.path.samestat(opened, after):
            raise _metadata_replaced(path)
        quarantine_name = f".{path.name}.{uuid4().hex}.cleanup"
        os.rename(
            path.name,
            quarantine_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        quarantined = os.stat(
            quarantine_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _validate_private_metadata(quarantined, path)
        if not os.path.samestat(opened, quarantined):
            raise _metadata_replaced(path)
        os.unlink(quarantine_name, dir_fd=parent_descriptor)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        os.close(parent_descriptor)


def _metadata_replaced(path: Path) -> ServiceControlError:
    return ServiceControlError(
        f"Lifecycle metadata changed before cleanup: {path}",
        code="identity_changed",
        recovery="Run `kestrel doctor`; the changed file was preserved.",
    )


def _bounded_seconds(
    name: str,
    value: float,
    *,
    allow_zero: bool,
) -> float:
    seconds = float(value)
    minimum_valid = seconds >= 0 if allow_zero else seconds > 0
    if not math.isfinite(seconds) or not minimum_valid:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a finite {qualifier} number")
    return seconds


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float,
    poll_interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    deadline = clock() + timeout_seconds
    while True:
        if predicate():
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(poll_interval, remaining))


def _same_process_identity(
    expected: ProcessSnapshot,
    current: ProcessSnapshot,
) -> bool:
    return (
        expected.pid == current.pid
        and expected.uid == current.uid
        and expected.cwd.resolve() == current.cwd.resolve()
        and expected.command == current.command
        and expected.pgid == current.pgid
        and expected.birth_marker == current.birth_marker
    )


def _identity_changed(pid: int) -> ServiceControlError:
    return ServiceControlError(
        f"Process identity changed for PID {pid}; escalation was refused.",
        code="identity_changed",
        recovery="Run `kestrel doctor`; no reused or unverified process was signalled.",
    )


def _status_conflict(status: ServiceStatus) -> ServiceControlError:
    return ServiceControlError(
        status.detail,
        code="service_conflict",
        recovery="Resolve the ownership conflict with `kestrel doctor` before retrying.",
    )


def _with_lifecycle(detail: str, busy: bool) -> str:
    if not busy:
        return detail
    return f"{detail} A lifecycle command is currently in progress."
