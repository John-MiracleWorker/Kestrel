from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from .backends.memvid_backend import MemvidBackend
from .config import AgentConfig
from .desktop_bootstrap import (
    DesktopLaunchConfig,
    consume_desktop_bootstrap,
)
from .layers import DEFAULT_LAYER_SPECS, prepare_private_memory_artifacts
from .platform_primitives import is_link_or_reparse_point
from .private_artifacts import (
    ensure_owner_only_directory,
    read_private_text,
    write_private_text,
)
from .runtime_profile_lease import (
    LeaseManagement,
    LeaseProcessInspector,
    LeaseProcessSnapshot,
    RuntimeLeaseIdentity,
    RuntimeProfileLease,
    current_runtime_lease_identity,
    inspect_lease_process,
    resolve_runtime_profile_root,
)
from .server import create_app

_UVICORN_BACKLOG = 2048
_SIDECAR_READINESS_SCHEMA = "kestrel.desktop.sidecar_readiness.v1"
_SIDECAR_READINESS_DIRECTORY = "runtime"
_SIDECAR_READINESS_NAME = "desktop-readiness.json"
_RESOURCE_MANIFEST_NAME = "kestrel-resource-manifest.json"


class _DesktopServer(Protocol):
    async def serve(self, *, sockets: list[socket.socket]) -> None: ...


class _PreflightBackend(Protocol):
    def open(self) -> None: ...

    def close(self) -> Any: ...


class _RuntimeLease(Protocol):
    def release(self) -> None: ...


BackendFactory = Callable[[Path], _PreflightBackend]
ServerFactory = Callable[[Any], _DesktopServer]
SocketFactory = Callable[[], socket.socket]
Preflight = Callable[[Path], None]


class IdentityFactory(Protocol):
    def __call__(
        self,
        *,
        profile_id: str,
        management: LeaseManagement,
        base_url: str,
        launch_nonce: str,
    ) -> RuntimeLeaseIdentity: ...


class LeaseAcquirer(Protocol):
    def __call__(
        self,
        profile_root: Path,
        identity: RuntimeLeaseIdentity,
    ) -> _RuntimeLease: ...


class AppFactory(Protocol):
    def __call__(
        self,
        config: AgentConfig,
        *,
        desktop_context: DesktopLaunchConfig,
    ) -> Any: ...


@dataclass(frozen=True)
class DesktopSidecarReadiness:
    pid: int
    process_birth_marker: str
    port: int
    profile_id: str
    sidecar_version: str
    executable_digest: str
    resource_manifest_digest: str
    launch_nonce_digest: str
    schema: str = _SIDECAR_READINESS_SCHEMA

    def __post_init__(self) -> None:
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("pid must be a positive integer")
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValueError("port must be an integer")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        for field_name in (
            "process_birth_marker",
            "profile_id",
            "sidecar_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        for field_name in ("executable_digest", "launch_nonce_digest"):
            object.__setattr__(
                self,
                field_name,
                _sha256_digest(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "resource_manifest_digest",
            _prefixed_sha256_digest(
                self.resource_manifest_digest,
                "resource_manifest_digest",
            ),
        )
        if self.schema != _SIDECAR_READINESS_SCHEMA:
            raise ValueError(f"schema must be {_SIDECAR_READINESS_SCHEMA}")

    @classmethod
    def from_runtime(
        cls,
        identity: RuntimeLeaseIdentity,
        *,
        port: int,
        resource_manifest_digest: str,
    ) -> DesktopSidecarReadiness:
        if identity.management != "desktop":
            raise ValueError("Desktop readiness requires a desktop-managed lease")
        return cls(
            pid=identity.pid,
            process_birth_marker=identity.process_birth_marker,
            port=port,
            profile_id=identity.profile_id,
            sidecar_version=identity.version,
            executable_digest=identity.executable_digest,
            resource_manifest_digest=resource_manifest_digest,
            launch_nonce_digest=identity.launch_nonce_digest,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "pid": self.pid,
            "process_birth_marker": self.process_birth_marker,
            "port": self.port,
            "profile_id": self.profile_id,
            "sidecar_version": self.sidecar_version,
            "executable_digest": self.executable_digest,
            "resource_manifest_digest": self.resource_manifest_digest,
            "launch_nonce_digest": self.launch_nonce_digest,
        }


def bind_desktop_socket(*, backlog: int = _UVICORN_BACKLOG) -> socket.socket:
    """Return one listening socket on an operating-system-assigned loopback port."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(backlog)
    except BaseException:
        sock.close()
        raise
    return sock


async def serve_desktop_app(
    app: Any,
    sock: socket.socket,
    *,
    server_factory: ServerFactory | None = None,
) -> None:
    """Serve an app without reopening or replacing its already-bound socket."""

    if server_factory is None:
        import uvicorn

        config = uvicorn.Config(app, access_log=False, lifespan="on")
        sock.listen(config.backlog)
        server: _DesktopServer = uvicorn.Server(config)
    else:
        server = server_factory(app)
    await server.serve(sockets=[sock])


def run_desktop_sidecar_preflight(
    memory_dir: Path,
    *,
    backend_factory: BackendFactory | None = None,
) -> None:
    """Open and close every canonical Memvid v2 layer before API readiness."""

    prepare_private_memory_artifacts(memory_dir)
    opened: list[_PreflightBackend] = []
    try:
        for layer, spec in DEFAULT_LAYER_SPECS.items():
            path = Path(memory_dir) / spec.mv2_file
            backend = (
                backend_factory(path)
                if backend_factory is not None
                else MemvidBackend(path=path, layer=layer)
            )
            backend.open()
            opened.append(backend)
    finally:
        cleanup_error: BaseException | None = None
        for backend in reversed(opened):
            try:
                backend.close()
            except BaseException as exc:  # noqa: BLE001 - close every opened layer
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise RuntimeError("desktop_memvid_preflight_cleanup_incomplete") from cleanup_error
def build_desktop_agent_config(launch: DesktopLaunchConfig) -> AgentConfig:
    """Build the immutable profile boundary around owner-tunable runtime settings."""

    root = launch.profile_root
    return AgentConfig(
        backend="memvid",
        provider="mock",
        model="mock",
        memory_dir=launch.memory_dir,
        workspace=root / "workspace",
        log_dir=root / "logs",
        state_path=launch.state_path,
        secret_store_path=root / "secrets" / "local_vault.json",
        skills_dir=root / "skills",
        plugins_dir=root / "plugins",
        mcp_config_path=root / "config" / "mcp_servers.json",
        channel_config_path=root / "config" / "channels.json",
        worker_worktree_dir=root / "worktrees",
        require_api_auth=True,
    )


def prepare_desktop_profile_directories(launch: DesktopLaunchConfig) -> None:
    """Create or repair the dedicated Desktop profile directories as owner-only."""

    for directory in (
        launch.profile_root,
        launch.state_path.parent,
        launch.memory_dir,
        launch.runtime_settings_path.parent,
        launch.profile_root / _SIDECAR_READINESS_DIRECTORY,
        launch.profile_root / "logs",
        launch.profile_root / "secrets",
        launch.profile_root / "skills",
        launch.profile_root / "plugins",
        launch.profile_root / "worktrees",
        launch.profile_root / "workspace",
    ):
        ensure_owner_only_directory(directory)


async def run_desktop_sidecar(
    bootstrap_path: Path,
    *,
    manifest_path: Path | None = None,
    inspector: LeaseProcessInspector = inspect_lease_process,
    socket_factory: SocketFactory = bind_desktop_socket,
    identity_factory: IdentityFactory = current_runtime_lease_identity,
    lease_acquirer: LeaseAcquirer = RuntimeProfileLease.acquire,
    preflight: Preflight = run_desktop_sidecar_preflight,
    app_factory: AppFactory = create_app,
    server_factory: ServerFactory | None = None,
) -> None:
    """Run one authenticated Desktop-owned Kestrel authority."""

    launch = consume_desktop_bootstrap(Path(bootstrap_path))
    verify_desktop_parent_identity(launch, inspector=inspector)
    verified_manifest_digest = verify_resource_manifest_binding(
        launch,
        manifest_path=manifest_path or resolve_resource_manifest_path(),
    )

    sock: socket.socket | None = None
    lease: _RuntimeLease | None = None
    readiness: DesktopSidecarReadiness | None = None
    readiness_path = desktop_readiness_path(launch)
    readiness_published = False
    failed = False
    cleanup_incomplete = False
    try:
        sock = socket_factory()
        host, raw_port = sock.getsockname()
        port = int(raw_port)
        if host != "127.0.0.1" or not 1 <= port <= 65535:
            raise RuntimeError("desktop_sidecar_socket_is_not_ipv4_loopback")
        base_url = f"http://127.0.0.1:{port}/"
        identity = identity_factory(
            profile_id=launch.profile_id,
            management="desktop",
            base_url=base_url,
            launch_nonce=launch.launch_nonce,
        )
        if (
            identity.profile_id != launch.profile_id
            or identity.management != "desktop"
            or identity.base_url != base_url
            or not secrets.compare_digest(
                identity.launch_nonce_digest,
                sha256(launch.launch_nonce.encode("utf-8")).hexdigest(),
            )
        ):
            raise RuntimeError("desktop_runtime_identity_mismatch")
        profile_root = resolve_runtime_profile_root(
            launch.state_path,
            launch.memory_dir,
            profile_id=launch.profile_id,
        )
        lease = lease_acquirer(profile_root, identity)
        prepare_desktop_profile_directories(launch)
        preflight(launch.memory_dir)
        config = build_desktop_agent_config(launch)
        app = app_factory(config, desktop_context=launch)
        readiness = DesktopSidecarReadiness.from_runtime(
            identity,
            port=port,
            resource_manifest_digest=verified_manifest_digest,
        )
        write_desktop_readiness(readiness_path, readiness)
        readiness_published = True
        await serve_desktop_app(
            app,
            sock,
            server_factory=server_factory,
        )
    except BaseException:
        failed = True
        raise
    finally:
        if readiness_published and readiness is not None:
            cleanup_incomplete = not remove_owned_desktop_readiness(
                readiness_path,
                readiness,
            )
        if sock is not None:
            sock.close()
        if lease is not None:
            lease.release()
        if cleanup_incomplete and not failed:
            raise RuntimeError("desktop_readiness_cleanup_incomplete")


def resolve_resource_manifest_path() -> Path:
    """Locate the immutable manifest in both one-file and app-resource layouts."""

    executable_parent = Path(sys.executable).resolve().parent
    bundle_root = Path(getattr(sys, "_MEIPASS", executable_parent))
    candidates = (
        bundle_root / _RESOURCE_MANIFEST_NAME,
        bundle_root / "resources" / _RESOURCE_MANIFEST_NAME,
        executable_parent / _RESOURCE_MANIFEST_NAME,
        executable_parent / "resources" / _RESOURCE_MANIFEST_NAME,
        executable_parent.parent / _RESOURCE_MANIFEST_NAME,
    )
    for candidate in candidates:
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            continue
        return candidate
    raise RuntimeError("packaged_resource_manifest_missing")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kestrel-desktop-sidecar")
    parser.add_argument("bootstrap_path", type=Path)
    arguments = parser.parse_args(argv)
    asyncio.run(run_desktop_sidecar(arguments.bootstrap_path))


def verify_desktop_parent_identity(
    launch: DesktopLaunchConfig,
    *,
    actual_parent_pid: int | None = None,
    current_pid: int | None = None,
    inspector: LeaseProcessInspector,
) -> LeaseProcessSnapshot:
    """Verify that the live parent matches the bootstrap's birth-bound identity."""

    observed_parent_pid = os.getppid() if actual_parent_pid is None else actual_parent_pid
    if observed_parent_pid != launch.parent_pid:
        raise RuntimeError("desktop_parent_pid_mismatch")
    own_pid = os.getpid() if current_pid is None else current_pid
    parent = inspector(launch.parent_pid)
    current = inspector(own_pid)
    if (
        parent is None
        or current is None
        or parent.pid != launch.parent_pid
        or current.pid != own_pid
        or not secrets.compare_digest(
            parent.process_birth_marker,
            launch.parent_birth_marker,
        )
        or not secrets.compare_digest(parent.owner_digest, current.owner_digest)
    ):
        raise RuntimeError("desktop_parent_identity_unverified")
    return parent


def verify_resource_manifest_binding(
    launch: DesktopLaunchConfig,
    *,
    manifest_path: Path,
) -> str:
    """Bind the consumed bootstrap to the exact packaged resource manifest bytes."""

    expected = _prefixed_sha256_digest(
        launch.resource_manifest_digest,
        "resource_manifest_digest",
    )
    actual = f"sha256:{_verified_file_digest(manifest_path)}"
    if not secrets.compare_digest(actual, expected):
        raise RuntimeError("resource_manifest_digest_mismatch")
    return actual


def desktop_readiness_path(launch: DesktopLaunchConfig) -> Path:
    return (
        launch.profile_root
        / _SIDECAR_READINESS_DIRECTORY
        / _SIDECAR_READINESS_NAME
    )


def write_desktop_readiness(
    path: Path,
    readiness: DesktopSidecarReadiness,
) -> None:
    ensure_owner_only_directory(Path(path).parent)
    rendered = json.dumps(
        readiness.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )
    write_private_text(path, f"{rendered}\n")


def remove_owned_desktop_readiness(
    path: Path,
    readiness: DesktopSidecarReadiness,
) -> bool:
    expected = json.dumps(
        readiness.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        current = read_private_text(path, missing_ok=True)
    except (OSError, PermissionError, ValueError):
        return False
    if current is None or current.strip() != expected:
        return False
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    if is_link_or_reparse_point(metadata) or not stat.S_ISREG(metadata.st_mode):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _verified_file_digest(path: Path) -> str:
    target = Path(path)
    before_open = os.lstat(target)
    if is_link_or_reparse_point(before_open) or not stat.S_ISREG(before_open.st_mode):
        raise RuntimeError("resource_manifest_must_be_a_regular_file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(target)
        if (
            is_link_or_reparse_point(opened)
            or is_link_or_reparse_point(after_open)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after_open.st_mode)
            or not os.path.samestat(before_open, opened)
            or not os.path.samestat(opened, after_open)
        ):
            raise RuntimeError("resource_manifest_changed_during_verification")
        digest = sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        final = os.lstat(target)
        if is_link_or_reparse_point(final) or not os.path.samestat(after_open, final):
            raise RuntimeError("resource_manifest_changed_during_verification")
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sha256_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    text = value.strip().lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return text


def _prefixed_sha256_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{field_name} must be a sha256-prefixed digest")
    return f"sha256:{_sha256_digest(value[7:], field_name)}"
