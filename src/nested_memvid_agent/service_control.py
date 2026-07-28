from __future__ import annotations

import os
import shlex
import socket
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .file_lock import lock_exclusive, unlock
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

    def listener_pids(self, host: str, port: int) -> tuple[int, ...]: ...

    def group_has_live_members(self, pgid: int) -> bool: ...

    def port_is_bindable(self, host: str, port: int) -> bool: ...


class ProbeClient(Protocol):
    def probe(self) -> ServerProbe: ...


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
                "command=",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        parts = result.stdout.strip().split(None, 3)
        if len(parts) != 4:
            raise ServiceControlError(
                f"Could not parse process identity for PID {pid}.",
                code="process_inspection_failed",
                recovery="Run `kestrel doctor` and inspect the process manually.",
            )
        uid_text, pgid_text, state, command_text = parts
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
        cwd = self._process_cwd(pid)
        return ProcessSnapshot(
            pid=pid,
            uid=uid,
            cwd=cwd,
            command=command,
            pgid=pgid,
            state=state,
        )

    def listener_pids(self, host: str, port: int) -> tuple[int, ...]:
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
                    "-t",
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
        pids: set[int] = set()
        for line in result.stdout.splitlines():
            value = line.strip()
            if not value:
                continue
            if not value.isdigit() or int(value) <= 0:
                raise ServiceControlError(
                    f"Received an invalid listener PID for port {port}.",
                    code="port_inspection_failed",
                    recovery="Run `kestrel doctor` and inspect the listener manually.",
                )
            pids.add(int(value))
        return tuple(sorted(pids))

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
    ) -> None:
        self.paths = paths
        self.inspector = inspector or SystemProcessInspector()
        self.client = client or KestrelServerClient(paths.url)

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
        listeners = self.inspector.listener_pids(
            self.paths.host,
            self.paths.port,
        )
        if len(listeners) > 1:
            return self._conflict(
                "Multiple processes report ownership of the Kestrel loopback port.",
                lifecycle_busy=lifecycle_busy,
            )
        listener_pid = listeners[0] if listeners else None
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
    )
    return _server_identity_matches(child, paths, managed=True)


def _command_option(command: tuple[str, ...], option: str) -> str | None:
    for index, argument in enumerate(command):
        if argument == option:
            if index + 1 >= len(command):
                return None
            return command[index + 1]
        prefix = f"{option}="
        if argument.startswith(prefix):
            return argument[len(prefix) :]
    return None


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
    return candidate.resolve()


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else 0


def _with_lifecycle(detail: str, busy: bool) -> str:
    if not busy:
        return detail
    return f"{detail} A lifecycle command is currently in progress."
