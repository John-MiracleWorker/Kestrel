from __future__ import annotations

import json
import math
import os
import shutil
import signal
import stat
import subprocess  # nosec B404
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, BinaryIO
from uuid import uuid4

from .extension_policy import (
    ExtensionPolicyError,
    ExtensionScopes,
    ResolvedFilesystemScope,
    copy_extension_snapshot,
    copy_readonly_filesystem_scope_snapshots,
    resolve_filesystem_scopes,
    validate_resolved_filesystem_scopes,
)

OCI_MEMORY_LIMIT = "256m"
OCI_CPU_LIMIT = "1.0"
OCI_PIDS_LIMIT = 64
OCI_NOFILE_LIMIT = 256
OCI_TMPFS_LIMIT = "64m"
OCI_OUTPUT_LIMIT_BYTES = 64 * 1024
OCI_STDIN_LIMIT_BYTES = 256 * 1024
OCI_CLEANUP_TIMEOUT_SECONDS = 5.0
OCI_CLEANUP_RETRY_MAX_SECONDS = 30.0
OCI_PROCESS_EXIT_TIMEOUT_SECONDS = 2.0
OCI_IO_JOIN_TIMEOUT_SECONDS = 1.0
# Registry execution must leave enough time after the in-container deadline to
# stop the CLI, remove the named container, prove absence, and drain all pipes.
OCI_TOOL_TIMEOUT_MARGIN_SECONDS = 16.0
OCI_ABSENCE_CONFIRMATIONS = 5
OCI_ABSENCE_CONFIRMATION_DELAY_SECONDS = 0.1


@dataclass(frozen=True)
class ContainerExecutionRequest:
    extension_id: str
    source_dir: Path
    expected_tree_digest: str
    workspace: Path
    scopes: ExtensionScopes
    image: str
    command: tuple[str, ...]
    stdin: str
    timeout_seconds: float


@dataclass(frozen=True)
class ContainerExecutionResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None
    content: str = ""
    tree_digest: str | None = None
    scope_digest: str | None = None


@dataclass(frozen=True)
class _PendingContainerCleanup:
    engine: tuple[str, ...]
    container_name: str
    environment: tuple[tuple[str, str], ...]

    @property
    def key(self) -> tuple[tuple[str, ...], str]:
        return self.engine, self.container_name


class OCIContainerCleanupQuarantine:
    """Retain exact OCI cleanup identities until absence is proven.

    The default instance is process-wide because validation runners may be
    short-lived.  Keeping the engine argv and generated container name here
    lets the runtime retry cleanup without trusting extension-controlled data
    or rediscovering a container by a broad label/name pattern.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._retry_lock = threading.Lock()
        self._pending: dict[tuple[tuple[str, ...], str], _PendingContainerCleanup] = {}

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def status(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            pending = tuple(self._pending.values())
        return tuple(
            {
                "engine": list(item.engine),
                "container_name": item.container_name,
            }
            for item in sorted(
                pending,
                key=lambda item: (item.engine, item.container_name),
            )
        )

    def retain(
        self,
        *,
        engine: tuple[str, ...],
        container_name: str,
        environment: dict[str, str],
    ) -> None:
        pending = _PendingContainerCleanup(
            engine=tuple(engine),
            container_name=container_name,
            environment=tuple(sorted(environment.items())),
        )
        with self._lock:
            self._pending[pending.key] = pending

    def retry_cleanup(self, *, timeout_seconds: float) -> bool:
        """Retry every retained exact-name cleanup within one total budget."""

        with self._retry_lock:
            with self._lock:
                pending = tuple(self._pending.values())
            if not pending:
                return True
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                return False
            deadline = time.monotonic() + min(
                timeout_seconds,
                OCI_CLEANUP_RETRY_MAX_SECONDS,
            )
            for item in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    cleanup_verified = _remove_and_confirm_container_absent(
                        item.engine,
                        item.container_name,
                        dict(item.environment),
                        timeout_seconds=remaining,
                    )
                except Exception:  # noqa: BLE001 - quarantine must remain owned
                    cleanup_verified = False
                if not cleanup_verified:
                    continue
                with self._lock:
                    if self._pending.get(item.key) is item:
                        self._pending.pop(item.key, None)
            with self._lock:
                return not self._pending


_PROCESS_OCI_CLEANUP_QUARANTINE = OCIContainerCleanupQuarantine()


class OCIContainerRunner:
    """Run a snapshotted extension in a fixed, default-deny OCI sandbox.

    The engine CLI is trusted infrastructure; the extension never receives its
    socket. The runner deliberately supports no host-process fallback. Docker
    is the default engine, while tests and compatible deployments may inject a
    command prefix (for example, a rootless Podman wrapper).
    """

    def __init__(
        self,
        *,
        engine_command: tuple[str, ...] = ("docker",),
        output_limit_bytes: int = OCI_OUTPUT_LIMIT_BYTES,
        cleanup_quarantine: OCIContainerCleanupQuarantine | None = None,
    ) -> None:
        if not engine_command or not all(isinstance(item, str) and item for item in engine_command):
            raise ValueError("engine_command must contain at least one non-empty argv item")
        self.engine_command = engine_command
        self.output_limit_bytes = max(1024, min(int(output_limit_bytes), 1024 * 1024))
        self.cleanup_quarantine = cleanup_quarantine or _PROCESS_OCI_CLEANUP_QUARANTINE

    @property
    def pending_cleanup_count(self) -> int:
        return self.cleanup_quarantine.pending_count

    def pending_cleanups(self) -> tuple[dict[str, object], ...]:
        return self.cleanup_quarantine.status()

    def retry_cleanup(self, *, timeout_seconds: float = OCI_CLEANUP_TIMEOUT_SECONDS) -> bool:
        return self.cleanup_quarantine.retry_cleanup(timeout_seconds=timeout_seconds)

    def shutdown(self, *, timeout_seconds: float = OCI_CLEANUP_TIMEOUT_SECONDS) -> bool:
        return self.retry_cleanup(timeout_seconds=timeout_seconds)

    def run(self, request: ContainerExecutionRequest) -> ContainerExecutionResult:
        if self.pending_cleanup_count:
            return _failure(
                "extension_cleanup_pending",
                "A previous OCI container cleanup is still unverified. "
                "Retry cleanup before admitting another container.",
            )
        engine = self._resolved_engine_command()
        if engine is None:
            return _failure("container_runtime_unavailable", "OCI container engine is unavailable.")
        environment = _engine_environment()
        if _is_docker_command(self.engine_command):
            endpoint, transport_error = _verified_docker_endpoint(
                engine, environment
            )
            if transport_error is not None:
                return _failure("container_runtime_nonlocal", transport_error)
            if endpoint is None:
                return _failure(
                    "container_runtime_nonlocal",
                    "Docker context endpoint could not be verified as local.",
                )
            engine = (*engine, "--host", endpoint)
            environment.pop("DOCKER_HOST", None)
            environment.pop("DOCKER_CONTEXT", None)
        if not _digest_pinned_image(request.image):
            return _failure(
                "container_image_not_digest_pinned",
                "Container image must use an immutable name@sha256:<64 hex> reference.",
            )
        if not request.command or any(
            not isinstance(item, str) or not item or _contains_control_character(item)
            for item in request.command
        ):
            return _failure("bad_container_command", "Container runtime requires a non-empty command list.")
        if request.scopes.network != "none" or request.scopes.secrets:
            return _failure("extension_scope_unsupported", "Only network-none, secret-free container scopes are supported.")
        try:
            stdin_payload = request.stdin.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            return _failure(
                "extension_stdin_invalid",
                "Container stdin must be valid UTF-8 text.",
            )
        if len(stdin_payload) > OCI_STDIN_LIMIT_BYTES:
            return _failure(
                "extension_stdin_limit_exceeded",
                f"Container stdin exceeded {OCI_STDIN_LIMIT_BYTES} bytes.",
            )

        try:
            with tempfile.TemporaryDirectory(prefix="kestrel-extension-") as temp_name:
                temp_root = _private_snapshot_root(
                    Path(temp_name),
                    source=request.source_dir,
                    workspace=request.workspace,
                )
                snapshot = temp_root / "snapshot"
                tree_digest = copy_extension_snapshot(request.source_dir, snapshot)
                if tree_digest != request.expected_tree_digest:
                    return _failure(
                        "extension_resource_changed",
                        "Executable skill files changed after discovery; rediscover and reauthorize the skill.",
                        tree_digest=tree_digest,
                        scope_digest=request.scopes.digest(),
                    )
                try:
                    mounts = resolve_filesystem_scopes(
                        request.scopes, request.workspace
                    )
                    mounts = copy_readonly_filesystem_scope_snapshots(
                        mounts,
                        temp_root / "workspace-scopes",
                        workspace=request.workspace,
                    )
                except ExtensionPolicyError as exc:
                    return _failure("extension_scope_invalid", str(exc))
                return self._run_snapshot(
                    request,
                    engine=engine,
                    environment=environment,
                    snapshot=snapshot,
                    mounts=mounts,
                    tree_digest=tree_digest,
                    stdin_payload=stdin_payload,
                )
        except (OSError, ExtensionPolicyError) as exc:
            return _failure("extension_snapshot_failed", f"{type(exc).__name__}: {exc}")

    def _resolved_engine_command(self) -> tuple[str, ...] | None:
        executable = self.engine_command[0]
        resolved = executable if Path(executable).is_absolute() else shutil.which(executable)
        if not resolved:
            return None
        return (resolved, *self.engine_command[1:])

    def _run_snapshot(
        self,
        request: ContainerExecutionRequest,
        *,
        engine: tuple[str, ...],
        environment: dict[str, str],
        snapshot: Path,
        mounts: tuple[ResolvedFilesystemScope, ...],
        tree_digest: str,
        stdin_payload: bytes,
    ) -> ContainerExecutionResult:
        container_name = _container_name(request.extension_id)
        try:
            # The original resolution and this launch-adjacent pass are both
            # required: extension snapshotting may take long enough for a
            # same-user workspace mutation to invalidate the first view.
            validate_resolved_filesystem_scopes(mounts)
            argv = _container_argv(
                engine,
                request=request,
                snapshot=snapshot,
                mounts=mounts,
                container_name=container_name,
            )
        except ExtensionPolicyError as exc:
            return _failure(
                "extension_mount_invalid",
                str(exc),
                tree_digest=tree_digest,
                scope_digest=request.scopes.digest(),
            )
        creation_flags = (
            int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            if os.name == "nt"
            else 0
        )
        try:
            process = subprocess.Popen(  # noqa: S603  # nosec B603
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                creationflags=creation_flags,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            return _failure("container_runtime_unavailable", f"{type(exc).__name__}: {exc}")

        capture = _BoundedCapture(self.output_limit_bytes)
        started_threads: list[threading.Thread] = []
        failure_error: str | None = None
        failure_content = ""
        returncode: int | None = None
        cleanup_verified = False
        try:
            readers = [
                threading.Thread(
                    target=capture.read, args=(process.stdout,), daemon=True
                ),
                threading.Thread(
                    target=capture.read_error, args=(process.stderr,), daemon=True
                ),
            ]
            for reader in readers:
                reader.start()
                started_threads.append(reader)
            writer = threading.Thread(
                target=_write_stdin,
                args=(process.stdin, stdin_payload),
                daemon=True,
            )
            writer.start()
            started_threads.append(writer)

            deadline = time.monotonic() + max(
                0.1, min(float(request.timeout_seconds), 120.0)
            )
            while process.poll() is None:
                if capture.overflowed.is_set():
                    failure_error = "extension_output_limit_exceeded"
                    failure_content = (
                        f"Container output exceeded {self.output_limit_bytes} bytes."
                    )
                    _terminate_process_group(process)
                    break
                if time.monotonic() >= deadline:
                    failure_error = "extension_timeout"
                    failure_content = (
                        f"Container execution exceeded {request.timeout_seconds:g} seconds."
                    )
                    _terminate_process_group(process)
                    break
                time.sleep(0.01)

            try:
                returncode = process.wait(
                    timeout=OCI_PROCESS_EXIT_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                _terminate_process_group(process, force=True)
                try:
                    returncode = process.wait(
                        timeout=OCI_PROCESS_EXIT_TIMEOUT_SECONDS
                    )
                except subprocess.TimeoutExpired:
                    returncode = process.poll()
                failure_error = failure_error or "extension_termination_failed"
                failure_content = failure_content or (
                    "Container process did not terminate cleanly."
                )
        except Exception as exc:  # noqa: BLE001 - cleanup must precede result
            failure_error = "extension_supervision_failed"
            failure_content = f"Container supervision failed: {type(exc).__name__}."
        finally:
            if process.poll() is None:
                _terminate_process_group(process, force=True)
                try:
                    returncode = process.wait(
                        timeout=OCI_PROCESS_EXIT_TIMEOUT_SECONDS
                    )
                except subprocess.TimeoutExpired:
                    returncode = process.poll()
            try:
                cleanup_verified = _remove_and_confirm_container_absent(
                    engine,
                    container_name,
                    environment,
                    timeout_seconds=OCI_CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: BLE001 - retain exact cleanup ownership
                cleanup_verified = False
            if not cleanup_verified:
                self.cleanup_quarantine.retain(
                    engine=engine,
                    container_name=container_name,
                    environment=environment,
                )
            for thread in started_threads:
                thread.join(timeout=OCI_IO_JOIN_TIMEOUT_SECONDS)
            if any(thread.is_alive() for thread in started_threads):
                for stream in (process.stdin, process.stdout, process.stderr):
                    _close_stream(stream)
                for thread in started_threads:
                    if thread.is_alive():
                        thread.join(timeout=0.1)

        stdout = capture.stdout_text()
        stderr = capture.stderr_text()
        io_drained = all(not thread.is_alive() for thread in started_threads)
        if not cleanup_verified or not io_drained:
            detail = (
                "Container absence could not be proven."
                if not cleanup_verified
                else "Container I/O workers did not terminate."
            )
            return _failure(
                "extension_cleanup_unverified",
                detail,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                tree_digest=tree_digest,
                scope_digest=request.scopes.digest(),
            )
        if capture.overflowed.is_set() and failure_error is None:
            failure_error = "extension_output_limit_exceeded"
            failure_content = f"Container output exceeded {self.output_limit_bytes} bytes."
        if failure_error is not None:
            return _failure(
                failure_error,
                failure_content,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                tree_digest=tree_digest,
                scope_digest=request.scopes.digest(),
            )
        success = returncode == 0
        return ContainerExecutionResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            error=None if success else "container_nonzero_exit",
            content="Container execution completed." if success else f"Container exited with code {returncode}.",
            tree_digest=tree_digest,
            scope_digest=request.scopes.digest(),
        )


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._used = 0
        self.overflowed = threading.Event()

    def read(self, stream: BinaryIO | None) -> None:
        self._read_into(stream, self._stdout)

    def read_error(self, stream: BinaryIO | None) -> None:
        self._read_into(stream, self._stderr)

    def _read_into(self, stream: BinaryIO | None, destination: bytearray) -> None:
        if stream is None:
            return
        try:
            while chunk := stream.read(8192):
                with self._lock:
                    remaining = max(0, self.limit - self._used)
                    if remaining:
                        accepted = chunk[:remaining]
                        destination.extend(accepted)
                        self._used += len(accepted)
                    if len(chunk) > remaining:
                        self.overflowed.set()
                        return
        except (OSError, ValueError):
            return

    def stdout_text(self) -> str:
        with self._lock:
            return bytes(self._stdout).decode("utf-8", errors="replace").strip()

    def stderr_text(self) -> str:
        with self._lock:
            return bytes(self._stderr).decode("utf-8", errors="replace").strip()


def _container_argv(
    engine: tuple[str, ...],
    *,
    request: ContainerExecutionRequest,
    snapshot: Path,
    mounts: tuple[ResolvedFilesystemScope, ...],
    container_name: str,
) -> list[str]:
    uid, gid = _non_root_identity()
    snapshot_source = _docker_mount_source(
        snapshot,
        error="extension_snapshot_path_contains_unsupported_comma",
    )
    argv = [
        *engine,
        "run",
        "--rm",
        "--pull=never",
        "--name",
        container_name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        f"--pids-limit={OCI_PIDS_LIMIT}",
        f"--memory={OCI_MEMORY_LIMIT}",
        f"--cpus={OCI_CPU_LIMIT}",
        f"--ulimit=nofile={OCI_NOFILE_LIMIT}:{OCI_NOFILE_LIMIT}",
        f"--ulimit=nproc={OCI_PIDS_LIMIT}:{OCI_PIDS_LIMIT}",
        "--ipc=none",
        "--init",
        "--stop-timeout=1",
        "--log-driver=none",
        f"--user={uid}:{gid}",
        "--env=HOME=/tmp",
        "--env=TMPDIR=/tmp",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        f"--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={OCI_TMPFS_LIMIT}",
        "--workdir=/extension",
        "--mount",
        f"type=bind,source={snapshot_source},target=/extension,readonly",
    ]
    for mount in mounts:
        if mount.access != "read":
            raise ExtensionPolicyError("extension_write_scope_unsupported")
        mount_source = _docker_mount_source(
            mount.source,
            error="extension_scope_path_contains_unsupported_comma",
        )
        option = f"type=bind,source={mount_source},target={mount.target},readonly"
        argv.extend(["--mount", option])
    # Explicitly terminate engine options before the untrusted manifest image
    # reference. This prevents an option-shaped value from changing the
    # container launch even if an engine's reference parser is permissive.
    argv.extend(["-i", "--", request.image, *request.command])
    return argv


def _docker_mount_source(path: Path, *, error: str) -> Path:
    if not path.is_absolute():
        raise ExtensionPolicyError(error)
    source = path
    if "," in os.fspath(source):
        raise ExtensionPolicyError(error)
    return source


def _engine_environment() -> dict[str, str]:
    allowed = ("HOME", "PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR")
    return {key: os.environ[key] for key in allowed if os.environ.get(key)}


def _write_stdin(stream: BinaryIO | None, payload: bytes) -> None:
    if stream is None:
        return
    try:
        stream.write(payload)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _close_stream(stream: IO[Any] | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        return


def _terminate_process_group(process: subprocess.Popen[bytes], *, force: bool = False) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            if not force:
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill() if force else process.terminate()
    except OSError:
        return


def _remove_and_confirm_container_absent(
    engine: tuple[str, ...],
    name: str,
    environment: dict[str, str],
    *,
    timeout_seconds: float,
) -> bool:
    """Force-remove a named container and require repeated exact-name absence."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return False
    deadline = time.monotonic() + timeout_seconds
    consecutive_absent = 0
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            subprocess.run(  # noqa: S603  # nosec B603
                [*engine, "rm", "-f", name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=min(1.0, remaining),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            consecutive_absent = 0

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            probe = subprocess.run(  # noqa: S603  # nosec B603
                [
                    *engine,
                    "ps",
                    "-a",
                    "--filter",
                    f"name=^/{name}$",
                    "--format={{.Names}}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=min(1.0, remaining),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            consecutive_absent = 0
        else:
            names = {
                line.strip()
                for line in probe.stdout.decode("utf-8", errors="replace").splitlines()
                if line.strip()
            }
            if probe.returncode == 0 and not names:
                consecutive_absent += 1
                if consecutive_absent >= OCI_ABSENCE_CONFIRMATIONS:
                    return True
            else:
                consecutive_absent = 0
        time.sleep(
            min(
                OCI_ABSENCE_CONFIRMATION_DELAY_SECONDS,
                max(0.0, deadline - time.monotonic()),
            )
        )
    return False


def _private_snapshot_root(root: Path, *, source: Path, workspace: Path) -> Path:
    """Require an owner-private system-temp root disjoint from user trees."""

    root.chmod(0o700)
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ExtensionPolicyError("extension_snapshot_location_unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ExtensionPolicyError("extension_snapshot_location_not_private")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ExtensionPolicyError("extension_snapshot_location_not_owned")
    resolved = root.resolve(strict=True)
    for protected in (source.expanduser().resolve(), workspace.expanduser().resolve()):
        if (
            resolved == protected
            or protected in resolved.parents
            or resolved in protected.parents
        ):
            raise ExtensionPolicyError("extension_snapshot_location_overlaps_user_tree")
    return resolved


def _is_docker_command(command: tuple[str, ...]) -> bool:
    return bool(command and Path(command[0]).name.casefold() in {"docker", "docker.exe"})


def _verified_docker_endpoint(
    engine: tuple[str, ...], environment: dict[str, str]
) -> tuple[str | None, str | None]:
    if len(engine) != 1:
        return (
            None,
            "Docker engine connection options are disabled; configure one verified local context.",
        )
    configured_host = environment.get("DOCKER_HOST", "").strip()
    if configured_host and not _local_container_endpoint(configured_host):
        return (
            None,
            "Remote DOCKER_HOST endpoints are disabled; use a local unix:// or npipe:// engine.",
        )
    try:
        inspected = subprocess.run(  # noqa: S603  # nosec B603
            [
                *engine,
                "context",
                "inspect",
                "--format",
                "{{json .Endpoints.docker.Host}}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "Docker context endpoint could not be verified as local."
    if inspected.returncode != 0 or len(inspected.stdout) > 4096:
        return None, "Docker context endpoint could not be verified as local."
    try:
        endpoint = json.loads(inspected.stdout.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "Docker context endpoint could not be verified as local."
    if not isinstance(endpoint, str) or not _local_container_endpoint(endpoint):
        return (
            None,
            "Remote Docker contexts are disabled; use a local unix:// or npipe:// engine.",
        )
    return endpoint, None


def _local_container_endpoint(value: str) -> bool:
    if _contains_control_character(value) or any(character.isspace() for character in value):
        return False
    if value.startswith("unix://"):
        socket_path = value.removeprefix("unix://")
        return bool(socket_path and Path(socket_path).is_absolute())
    if os.name == "nt" and value.casefold().startswith("npipe://"):
        return True
    return False


def _non_root_identity() -> tuple[int, int]:
    uid = os.getuid() if hasattr(os, "getuid") else 65532
    gid = os.getgid() if hasattr(os, "getgid") else 65532
    if uid == 0:
        uid = 65532
    if gid == 0:
        gid = 65532
    return uid, gid


def _container_name(extension_id: str) -> str:
    safe = "".join(character.lower() if character.isalnum() else "-" for character in extension_id).strip("-")
    return f"kestrel-skill-{(safe or 'extension')[:32]}-{uuid4().hex[:12]}"


def _digest_pinned_image(value: object) -> bool:
    reference = str(value).strip()
    name, marker, digest = reference.rpartition("@sha256:")
    return bool(
        marker
        and name
        and not name.startswith("-")
        and not any(character.isspace() or _contains_control_character(character) for character in name)
        and len(digest) == 64
        and all(character in "0123456789abcdefABCDEF" for character in digest)
    )


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _failure(
    error: str,
    content: str,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
    tree_digest: str | None = None,
    scope_digest: str | None = None,
) -> ContainerExecutionResult:
    return ContainerExecutionResult(
        success=False,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        error=error,
        content=content,
        tree_digest=tree_digest,
        scope_digest=scope_digest,
    )
