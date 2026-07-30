from __future__ import annotations

import asyncio
import hmac
import json
import os
import socket
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest
from fastapi.testclient import TestClient

import nested_memvid_agent.desktop_sidecar as desktop_sidecar_module
import nested_memvid_agent.server as server_module
from nested_memvid_agent.channels import ChannelManager
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.desktop_bootstrap import DesktopLaunchConfig
from nested_memvid_agent.desktop_memory_health import (
    capture_desktop_memvid_preflight_receipt,
)
from nested_memvid_agent.desktop_sidecar import (
    DesktopSidecarFailure,
    DesktopSidecarReadiness,
    DesktopStateCorruptError,
    DesktopStateIncompatibleError,
    bind_desktop_socket,
    bind_developer_runtime_identity,
    build_desktop_agent_config,
    classify_desktop_startup_failure,
    desktop_failure_path,
    desktop_readiness_path,
    remove_owned_desktop_readiness,
    run_desktop_sidecar,
    run_desktop_sidecar_preflight,
    run_desktop_state_preflight,
    serve_desktop_app,
    verify_desktop_parent_identity,
    verify_resource_manifest_binding,
    write_desktop_failure,
    write_desktop_readiness,
)
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS
from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.routing.runtime import build_run_manager
from nested_memvid_agent.runtime_profile_lease import (
    LeaseProcessSnapshot,
    RuntimeLeaseIdentity,
)
from nested_memvid_agent.runtime_settings import RuntimeSettings, RuntimeSettingsStore
from nested_memvid_agent.server_desktop_routes import (
    DesktopShutdownController,
    register_desktop_routes,
)
from nested_memvid_agent.state_store import SCHEMA_VERSION


class _RecordingServer:
    def __init__(self, app: object) -> None:
        self.app = app
        self.socket_fileno: int | None = None

    async def serve(self, *, sockets: list[socket.socket]) -> None:
        assert len(sockets) == 1
        self.socket_fileno = sockets[0].fileno()


def test_sidecar_serves_on_the_same_os_assigned_socket() -> None:
    app = object()
    servers: list[_RecordingServer] = []

    def server_factory(candidate: object) -> _RecordingServer:
        server = _RecordingServer(candidate)
        servers.append(server)
        return server

    sock = bind_desktop_socket()
    try:
        host, port = sock.getsockname()
        bound_socket_fileno = sock.fileno()
        asyncio.run(
            serve_desktop_app(
                app,
                sock,
                server_factory=server_factory,
            )
        )
    finally:
        sock.close()

    assert host == "127.0.0.1"
    assert 1 <= port <= 65535
    assert len(servers) == 1
    assert servers[0].app is app
    assert servers[0].socket_fileno == bound_socket_fileno


def test_production_uvicorn_config_reuses_its_backlog_on_the_bound_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    app = object()
    captured_servers: list[Any] = []

    class RecordingSocket:
        def __init__(self) -> None:
            self.listen_calls: list[int] = []

        def listen(self, backlog: int) -> None:
            self.listen_calls.append(backlog)

    class RecordingServer:
        def __init__(self, config: Any) -> None:
            self.config = config
            self.sockets: list[Any] = []
            captured_servers.append(self)

        async def serve(self, *, sockets: list[socket.socket]) -> None:
            self.sockets = sockets

    monkeypatch.setattr(uvicorn, "Server", RecordingServer)
    recording_socket: Any = RecordingSocket()

    asyncio.run(serve_desktop_app(app, recording_socket))

    assert len(captured_servers) == 1
    server = captured_servers[0]
    assert server.config.app is app
    assert server.config.access_log is False
    assert server.config.lifespan == "on"
    assert server.config.backlog == 2048
    assert recording_socket.listen_calls == [2048]
    assert server.sockets == [recording_socket]


def test_cli_invokes_sidecar_with_exactly_one_bootstrap_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = tmp_path / "bootstrap.json"
    calls: list[Path] = []

    async def record_run(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(
        desktop_sidecar_module,
        "run_desktop_sidecar",
        record_run,
    )

    desktop_sidecar_module.main([str(bootstrap)])

    assert calls == [bootstrap]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bootstrap.json", "unexpected-positional"],
        ["bootstrap.json", "--token", "secret"],
    ],
)
def test_cli_rejects_argument_shapes_other_than_one_positional_path(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_run(path: Path) -> None:
        pytest.fail(f"sidecar invoked for invalid CLI arguments: {path}")

    monkeypatch.setattr(
        desktop_sidecar_module,
        "run_desktop_sidecar",
        unexpected_run,
    )

    with pytest.raises(SystemExit) as caught:
        desktop_sidecar_module.main(argv)

    assert caught.value.code == 2


class _RecordingBackend:
    def __init__(
        self,
        *,
        path: Path,
        layer: MemoryLayer,
        opened: list[Path],
        created: list[Path],
    ) -> None:
        self.path = path
        self.layer = layer
        self._opened = opened
        self._created = created

    def open(self) -> None:
        self._opened.append(self.path)
        if not self.path.exists():
            self._created.append(self.path)
            self.path.write_bytes(b"new mv2")

    def close(self) -> None:
        return None


def test_sidecar_reopens_existing_six_mv2_files(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    expected = [memory_dir / spec.mv2_file for spec in DEFAULT_LAYER_SPECS.values()]
    for path in expected:
        path.write_bytes(b"existing mv2")

    opened: list[Path] = []
    created: list[Path] = []

    layers_by_name = {spec.mv2_file: layer for layer, spec in DEFAULT_LAYER_SPECS.items()}

    def backend_factory(path: Path) -> _RecordingBackend:
        return _RecordingBackend(
            path=path,
            layer=layers_by_name[path.name],
            opened=opened,
            created=created,
        )

    receipt = run_desktop_sidecar_preflight(
        memory_dir,
        backend_factory=backend_factory,
    )

    assert opened == expected
    assert created == []
    assert receipt.memory_dir == str(memory_dir.resolve())
    assert tuple(layer.filename for layer in receipt.layers) == tuple(
        path.name for path in expected
    )
    assert receipt.launch_nonce_digest is None
    assert receipt.resource_manifest_digest is None


@pytest.mark.parametrize(
    ("phase", "error", "expected"),
    [
        ("lease", RuntimeError("profile lease already held"), "profile_conflict"),
        (
            "state",
            RuntimeError("State schema 99 is newer than supported"),
            "state_incompatible",
        ),
        ("state", ValueError("database disk image is malformed"), "state_corrupt"),
        (
            "memvid",
            RuntimeError("sentinel provider text"),
            "memvid_reopen_failed",
        ),
        ("app", RuntimeError("unclassified"), None),
    ],
)
def test_startup_failure_classification_is_phase_bounded(
    phase: str,
    error: BaseException,
    expected: str | None,
) -> None:
    assert classify_desktop_startup_failure(phase, error) == expected


def test_desktop_state_preflight_rejects_newer_schema_without_mutation(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "agent.db"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?)",
            (SCHEMA_VERSION + 1,),
        )
    before = state_path.read_bytes()
    before_mtime = state_path.stat().st_mtime_ns

    with pytest.raises(
        DesktopStateIncompatibleError,
        match="desktop_state_schema_newer_than_supported",
    ):
        run_desktop_state_preflight(state_path)

    assert state_path.read_bytes() == before
    assert state_path.stat().st_mtime_ns == before_mtime


def test_desktop_state_preflight_rejects_corrupt_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "agent.db"
    state_path.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(
        DesktopStateCorruptError,
        match="desktop_state_integrity_failed",
    ):
        run_desktop_state_preflight(state_path)


def test_sidecar_failure_artifact_is_authenticated_and_secret_free(
    tmp_path: Path,
) -> None:
    launch = _launch(tmp_path)
    failure = DesktopSidecarFailure.create(
        launch,
        sidecar_version="0.5.0",
        reason="state_corrupt",
    )
    path = desktop_failure_path(launch)

    write_desktop_failure(path, failure)

    raw = path.read_bytes()
    payload = json.loads(raw)
    unsigned = dict(payload)
    authentication_tag = unsigned.pop("authentication_tag")
    canonical = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    expected = hmac.new(
        launch.api_token.encode(),
        b"kestrel.desktop.sidecar-failure.v1\0" + canonical,
        "sha256",
    ).hexdigest()
    assert hmac.compare_digest(authentication_tag, expected)
    assert payload["launch_nonce_digest"] == sha256(launch.launch_nonce.encode()).hexdigest()
    assert payload["reason"] == "state_corrupt"
    assert launch.api_token not in raw.decode()
    assert launch.launch_nonce not in raw.decode()
    assert len(raw) <= 16 * 1024
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_sidecar_failure_artifact_is_create_once(
    tmp_path: Path,
) -> None:
    launch = _launch(tmp_path)
    failure = DesktopSidecarFailure.create(
        launch,
        sidecar_version="0.5.0",
        reason="memvid_reopen_failed",
    )
    path = desktop_failure_path(launch)
    write_desktop_failure(path, failure)

    with pytest.raises(FileExistsError):
        write_desktop_failure(path, failure)


def _launch(
    tmp_path: Path,
    *,
    manifest_digest: str = "sha256:" + ("a" * 64),
    assurance_mode: str = "release",
) -> DesktopLaunchConfig:
    profile_root = tmp_path / "profile"
    profile_root.mkdir(exist_ok=True)
    return DesktopLaunchConfig(
        profile_id="default",
        profile_root=profile_root,
        state_path=profile_root / "state" / "agent.db",
        memory_dir=profile_root / "memory",
        runtime_settings_path=profile_root / "config" / "runtime_settings.json",
        launch_nonce="launch-nonce",
        api_token="desktop-secret-token",
        parent_pid=4242,
        parent_birth_marker="desktop-parent-birth-marker",
        resource_manifest_digest=manifest_digest,
        assurance_mode=assurance_mode,
    )


def _snapshot(
    pid: int,
    *,
    owner_digest: str = "1" * 64,
    birth_marker: str = "desktop-parent-birth-marker",
    executable_digest: str = "2" * 64,
) -> LeaseProcessSnapshot:
    return LeaseProcessSnapshot(
        pid=pid,
        owner_digest=owner_digest,
        process_birth_marker=birth_marker,
        executable_digest=executable_digest,
    )


def test_parent_identity_requires_actual_parent_birth_and_owner(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    snapshots = {
        4242: _snapshot(4242),
        9001: _snapshot(9001, birth_marker="sidecar-birth-marker"),
    }

    verified = verify_desktop_parent_identity(
        launch,
        actual_parent_pid=4242,
        current_pid=9001,
        inspector=lambda pid: snapshots.get(pid),
    )

    assert verified == snapshots[4242]


def test_developer_parent_identity_uses_tagged_ps_birth_marker(
    tmp_path: Path,
) -> None:
    launch = replace(
        _launch(tmp_path, assurance_mode="developer"),
        parent_birth_marker="developer-ps-lstart-ms:1753886400000",
    )
    snapshots = {
        4242: _snapshot(4242, birth_marker="darwin-proc-start:10:20"),
        9001: _snapshot(9001, birth_marker="darwin-proc-start:30:40"),
    }

    verified = verify_desktop_parent_identity(
        launch,
        actual_parent_pid=4242,
        current_pid=9001,
        inspector=lambda pid: snapshots.get(pid),
        developer_birth_marker_reader=(
            lambda pid: (
                "developer-ps-lstart-ms:1753886400000"
                if pid == 4242
                else None
            )
        ),
    )

    assert verified == snapshots[4242]


def test_developer_runtime_identity_reuses_tagged_ps_birth_marker(
    tmp_path: Path,
) -> None:
    identity = _lease_identity()
    launch = _launch(tmp_path, assurance_mode="developer")

    bound = bind_developer_runtime_identity(
        launch,
        identity,
        developer_birth_marker_reader=(
            lambda pid: (
                "developer-ps-lstart-ms:1753886400000"
                if pid == identity.pid
                else None
            )
        ),
    )

    assert bound == replace(
        identity,
        process_birth_marker="developer-ps-lstart-ms:1753886400000",
    )


@pytest.mark.parametrize(
    ("actual_parent_pid", "parent_snapshot", "message"),
    [
        (31337, _snapshot(4242), "parent_pid_mismatch"),
        (
            4242,
            _snapshot(4242, birth_marker="reused-parent-pid"),
            "parent_identity_unverified",
        ),
        (
            4242,
            _snapshot(4242, owner_digest="9" * 64),
            "parent_identity_unverified",
        ),
    ],
)
def test_parent_identity_mismatch_fails_closed(
    tmp_path: Path,
    actual_parent_pid: int,
    parent_snapshot: LeaseProcessSnapshot,
    message: str,
) -> None:
    launch = _launch(tmp_path)
    snapshots = {
        4242: parent_snapshot,
        9001: _snapshot(9001, birth_marker="sidecar-birth-marker"),
    }

    with pytest.raises(RuntimeError, match=message):
        verify_desktop_parent_identity(
            launch,
            actual_parent_pid=actual_parent_pid,
            current_pid=9001,
            inspector=lambda pid: snapshots.get(pid),
        )


def test_resource_manifest_binding_rejects_tampering(tmp_path: Path) -> None:
    manifest = tmp_path / "kestrel-resource-manifest.json"
    manifest.write_bytes(
        b'{"build_mode":"release","schema":"kestrel.desktop.resources.v1"}\n'
    )
    expected = "sha256:" + sha256(manifest.read_bytes()).hexdigest()
    launch = _launch(tmp_path, manifest_digest=expected)

    assert verify_resource_manifest_binding(launch, manifest_path=manifest) == expected

    manifest.write_bytes(b'{"schema":"kestrel.resource_manifest.v1","tampered":true}\n')
    with pytest.raises(RuntimeError, match="resource_manifest_digest_mismatch"):
        verify_resource_manifest_binding(launch, manifest_path=manifest)


def test_resource_manifest_binding_rejects_bootstrap_assurance_mode_mismatch(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "kestrel-resource-manifest.json"
    manifest.write_bytes(
        b'{"build_mode":"release","schema":"kestrel.desktop.resources.v1"}\n'
    )
    launch = _launch(
        tmp_path,
        manifest_digest="sha256:" + sha256(manifest.read_bytes()).hexdigest(),
        assurance_mode="developer",
    )

    with pytest.raises(RuntimeError, match="assurance_mode_mismatch"):
        verify_resource_manifest_binding(launch, manifest_path=manifest)


def _lease_identity() -> RuntimeLeaseIdentity:
    return RuntimeLeaseIdentity(
        profile_id="default",
        management="desktop",
        owner_digest="1" * 64,
        pid=9001,
        process_birth_marker="sidecar-birth-marker",
        executable_digest="2" * 64,
        launch_nonce_digest="3" * 64,
        base_url="http://127.0.0.1:43123/",
        version="0.5.0",
        created_at="2026-07-29T12:00:00+00:00",
    )


def test_readiness_is_atomic_owner_only_and_redacted(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    identity = _lease_identity()
    readiness = DesktopSidecarReadiness.from_runtime(
        identity,
        port=43123,
        resource_manifest_digest="sha256:" + ("4" * 64),
    )
    path = desktop_readiness_path(launch)

    write_desktop_readiness(path, readiness)

    assert path == launch.profile_root / "runtime" / "desktop-readiness.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema": "kestrel.desktop.sidecar_readiness.v1",
        "pid": 9001,
        "process_birth_marker": "sidecar-birth-marker",
        "port": 43123,
        "profile_id": "default",
        "sidecar_version": "0.5.0",
        "executable_digest": "2" * 64,
        "resource_manifest_digest": "sha256:" + ("4" * 64),
        "launch_nonce_digest": "3" * 64,
    }
    serialized = path.read_text(encoding="utf-8")
    assert "desktop-secret-token" not in serialized
    assert "launch-nonce" not in serialized
    assert str(launch.state_path) not in serialized
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_readiness_cleanup_removes_only_this_launch_record(tmp_path: Path) -> None:
    launch = _launch(tmp_path)
    path = desktop_readiness_path(launch)
    readiness = DesktopSidecarReadiness.from_runtime(
        _lease_identity(),
        port=43123,
        resource_manifest_digest="sha256:" + ("4" * 64),
    )
    write_desktop_readiness(path, readiness)

    replacement = replace(readiness, pid=9002)
    write_desktop_readiness(path, replacement)

    assert remove_owned_desktop_readiness(path, readiness) is False
    assert path.exists()
    assert remove_owned_desktop_readiness(path, replacement) is True
    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not a Windows ACL")
def test_readiness_repairs_dedicated_runtime_directory_to_owner_only(
    tmp_path: Path,
) -> None:
    launch = _launch(tmp_path)
    path = desktop_readiness_path(launch)
    path.parent.mkdir()
    path.parent.chmod(0o755)
    readiness = DesktopSidecarReadiness.from_runtime(
        _lease_identity(),
        port=43123,
        resource_manifest_digest="sha256:" + ("4" * 64),
    )

    write_desktop_readiness(path, readiness)

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def _write_bootstrap(path: Path, launch: DesktopLaunchConfig) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "kestrel.desktop.bootstrap.v1",
                "profile_id": launch.profile_id,
                "profile_root": str(launch.profile_root),
                "state_path": str(launch.state_path),
                "memory_dir": str(launch.memory_dir),
                "runtime_settings_path": str(launch.runtime_settings_path),
                "launch_nonce": launch.launch_nonce,
                "api_token": launch.api_token,
                "parent_pid": launch.parent_pid,
                "parent_birth_marker": launch.parent_birth_marker,
                "resource_manifest_digest": launch.resource_manifest_digest,
                "assurance_mode": launch.assurance_mode,
                "memory_layers": [layer.value for layer in DEFAULT_LAYER_SPECS],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _verified_sidecar_inputs(
    tmp_path: Path,
) -> tuple[
    Path,
    Path,
    DesktopLaunchConfig,
    dict[int, LeaseProcessSnapshot],
    Callable[..., RuntimeLeaseIdentity],
]:
    manifest = tmp_path / "kestrel-resource-manifest.json"
    manifest.write_bytes(
        b'{"build_mode":"release","schema":"kestrel.desktop.resources.v1"}\n'
    )
    parent_pid = os.getppid()
    current_pid = os.getpid()
    launch = replace(
        _launch(
            tmp_path,
            manifest_digest="sha256:" + sha256(manifest.read_bytes()).hexdigest(),
        ),
        parent_pid=parent_pid,
        parent_birth_marker="verified-parent-birth",
    )
    bootstrap = tmp_path / "bootstrap.json"
    _write_bootstrap(bootstrap, launch)
    snapshots = {
        parent_pid: _snapshot(parent_pid, birth_marker="verified-parent-birth"),
        current_pid: _snapshot(current_pid, birth_marker="verified-sidecar-birth"),
    }

    def identity_factory(**kwargs: object) -> RuntimeLeaseIdentity:
        return RuntimeLeaseIdentity(
            profile_id="default",
            management="desktop",
            owner_digest="1" * 64,
            pid=current_pid,
            process_birth_marker="verified-sidecar-birth",
            executable_digest="2" * 64,
            launch_nonce_digest=sha256(b"launch-nonce").hexdigest(),
            base_url=str(kwargs["base_url"]),
            version="0.5.0",
            created_at="2026-07-29T12:00:00+00:00",
        )

    return bootstrap, manifest, launch, snapshots, identity_factory


def test_sidecar_lifecycle_acquires_before_writers_and_releases_last(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "kestrel-resource-manifest.json"
    manifest.write_bytes(
        b'{"build_mode":"release","schema":"kestrel.desktop.resources.v1"}\n'
    )
    manifest_digest = "sha256:" + sha256(manifest.read_bytes()).hexdigest()
    parent_pid = os.getppid()
    current_pid = os.getpid()
    launch = replace(
        _launch(tmp_path, manifest_digest=manifest_digest),
        parent_pid=parent_pid,
        parent_birth_marker="verified-parent-birth",
    )
    bootstrap = tmp_path / "bootstrap.json"
    _write_bootstrap(bootstrap, launch)
    readiness_path = desktop_readiness_path(launch)
    events: list[str] = []
    socket_holder: list[socket.socket] = []
    captured_readiness: list[dict[str, object]] = []

    snapshots = {
        parent_pid: _snapshot(parent_pid, birth_marker="verified-parent-birth"),
        current_pid: _snapshot(current_pid, birth_marker="verified-sidecar-birth"),
    }

    def socket_factory() -> socket.socket:
        sock = bind_desktop_socket()
        socket_holder.append(sock)
        return sock

    def identity_factory(**kwargs: object) -> RuntimeLeaseIdentity:
        events.append("identity")
        assert kwargs["profile_id"] == "default"
        assert kwargs["management"] == "desktop"
        assert kwargs["launch_nonce"] == "launch-nonce"
        base_url = str(kwargs["base_url"])
        assert base_url.startswith("http://127.0.0.1:")
        int(base_url.removeprefix("http://127.0.0.1:").removesuffix("/"))
        return RuntimeLeaseIdentity(
            profile_id="default",
            management="desktop",
            owner_digest="1" * 64,
            pid=current_pid,
            process_birth_marker="verified-sidecar-birth",
            executable_digest="2" * 64,
            launch_nonce_digest=sha256(b"launch-nonce").hexdigest(),
            base_url=base_url,
            version="0.5.0",
            created_at="2026-07-29T12:00:00+00:00",
        )

    class FakeLease:
        def release(self) -> None:
            assert not readiness_path.exists()
            assert socket_holder[0].fileno() == -1
            events.append("release")

    def lease_acquirer(
        profile_root: Path,
        identity: RuntimeLeaseIdentity,
    ) -> FakeLease:
        events.append("lease")
        assert profile_root.parent.parent == launch.state_path.parent
        assert identity.base_url.endswith("/")
        return FakeLease()

    def preflight(memory_dir: Path):
        events.append("preflight")
        assert events.index("lease") < events.index("preflight")
        assert memory_dir == launch.memory_dir
        for private_directory in (
            launch.profile_root,
            launch.state_path.parent,
            launch.memory_dir,
            launch.runtime_settings_path.parent,
            launch.profile_root / "runtime",
        ):
            assert private_directory.is_dir()
            if os.name != "nt":
                assert private_directory.stat().st_mode & 0o777 == 0o700
        for spec in DEFAULT_LAYER_SPECS.values():
            path = memory_dir / spec.mv2_file
            path.write_bytes(b"startup-opened-mv2")
            path.chmod(0o600)
        return capture_desktop_memvid_preflight_receipt(memory_dir)

    app = object()

    def app_factory(
        config: AgentConfig,
        *,
        desktop_context: DesktopLaunchConfig,
        desktop_shutdown: DesktopShutdownController,
    ) -> object:
        del desktop_shutdown
        events.append("app")
        assert events.index("lease") < events.index("app")
        assert desktop_context == launch
        receipt = desktop_context.memory_preflight_receipt
        assert receipt is not None
        assert (
            receipt.launch_nonce_digest == sha256(launch.launch_nonce.encode("utf-8")).hexdigest()
        )
        assert receipt.resource_manifest_digest == manifest_digest
        assert config.backend == "memvid"
        assert config.state_path == launch.state_path
        assert config.memory_dir == launch.memory_dir
        return app

    class LifecycleServer:
        async def serve(self, *, sockets: list[socket.socket]) -> None:
            events.append("serve")
            assert len(sockets) == 1
            assert sockets[0] is socket_holder[0]
            payload = json.loads(readiness_path.read_text(encoding="utf-8"))
            captured_readiness.append(payload)
            assert payload["port"] == sockets[0].getsockname()[1]

    asyncio.run(
        run_desktop_sidecar(
            bootstrap,
            manifest_path=manifest,
            inspector=lambda pid: snapshots.get(pid),
            socket_factory=socket_factory,
            identity_factory=identity_factory,
            lease_acquirer=lease_acquirer,
            preflight=preflight,
            app_factory=app_factory,
            server_factory=lambda candidate: (
                LifecycleServer() if candidate is app else pytest.fail("unexpected app")
            ),
        )
    )

    assert events == ["identity", "lease", "preflight", "app", "serve", "release"]
    assert not bootstrap.exists()
    assert not readiness_path.exists()
    assert captured_readiness[0]["launch_nonce_digest"] == sha256(b"launch-nonce").hexdigest()
    serialized = json.dumps(captured_readiness[0], sort_keys=True)
    assert "desktop-secret-token" not in serialized
    assert "launch-nonce" not in serialized


def test_authenticated_shutdown_exits_real_serve_loop_before_lease_release(
    tmp_path: Path,
) -> None:
    from fastapi import FastAPI
    from fastapi import Request as FastAPIRequest
    from starlette.responses import JSONResponse

    bootstrap, manifest, launch, snapshots, identity_factory = _verified_sidecar_inputs(tmp_path)
    readiness_path = desktop_readiness_path(launch)
    events: list[str] = []

    class FakeLease:
        def release(self) -> None:
            assert not readiness_path.exists()
            events.append("lease_release")

    def app_factory(
        config: AgentConfig,
        *,
        desktop_context: DesktopLaunchConfig,
        desktop_shutdown: DesktopShutdownController,
    ) -> object:
        del config

        @asynccontextmanager
        async def lifespan(app: object) -> Any:
            del app
            events.append("lifespan_start")
            try:
                yield
            finally:
                events.append("lifespan_stop")

        app = FastAPI(lifespan=lifespan)

        @app.middleware("http")
        async def authenticate(request: FastAPIRequest, call_next: Any) -> Any:
            if request.headers.get("authorization") != "Bearer desktop-secret-token":
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

        register_desktop_routes(
            app,
            launch=desktop_context,
            shutdown_controller=desktop_shutdown,
        )
        return app

    async def exercise() -> tuple[int, dict[str, object]]:
        sidecar = asyncio.create_task(
            run_desktop_sidecar(
                bootstrap,
                manifest_path=manifest,
                inspector=lambda pid: snapshots.get(pid),
                identity_factory=identity_factory,
                lease_acquirer=lambda profile_root, identity: FakeLease(),
                preflight=lambda memory_dir: None,
                app_factory=app_factory,
            )
        )
        for _attempt in range(200):
            if readiness_path.exists():
                break
            await asyncio.sleep(0.01)
        else:
            sidecar.cancel()
            pytest.fail("sidecar readiness was not published")
        payload = json.loads(readiness_path.read_text(encoding="utf-8"))
        request = Request(
            f"http://127.0.0.1:{payload['port']}/api/desktop/shutdown",
            data=b"",
            method="POST",
            headers={"Authorization": "Bearer desktop-secret-token"},
        )

        def send() -> tuple[int, dict[str, object]]:
            with urlopen(request, timeout=5.0) as response:  # nosec B310 - loopback fixture
                return response.status, json.loads(response.read())

        response = await asyncio.to_thread(send)
        events.append("shutdown_response")
        await asyncio.wait_for(sidecar, timeout=10.0)
        return response

    status, response = asyncio.run(exercise())

    assert status == 202
    assert response == {
        "schema": "kestrel.desktop.shutdown.v1",
        "accepted": True,
    }
    assert events == [
        "lifespan_start",
        "shutdown_response",
        "lifespan_stop",
        "lease_release",
    ]
    assert not readiness_path.exists()


def test_prelease_identity_failure_does_not_claim_profile_conflict(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "kestrel-resource-manifest.json"
    manifest.write_bytes(
        b'{"build_mode":"release","schema":"kestrel.desktop.resources.v1"}\n'
    )
    parent_pid = os.getppid()
    current_pid = os.getpid()
    launch = replace(
        _launch(
            tmp_path,
            manifest_digest=("sha256:" + sha256(manifest.read_bytes()).hexdigest()),
        ),
        parent_pid=parent_pid,
        parent_birth_marker="verified-parent-birth",
    )
    bootstrap = tmp_path / "bootstrap.json"
    _write_bootstrap(bootstrap, launch)
    sock = bind_desktop_socket()
    snapshots = {
        parent_pid: _snapshot(
            parent_pid,
            birth_marker="verified-parent-birth",
        ),
        current_pid: _snapshot(
            current_pid,
            birth_marker="verified-sidecar-birth",
        ),
    }

    def mismatched_identity(**kwargs: object) -> RuntimeLeaseIdentity:
        return RuntimeLeaseIdentity(
            profile_id="other",
            management="desktop",
            owner_digest="1" * 64,
            pid=current_pid,
            process_birth_marker="verified-sidecar-birth",
            executable_digest="2" * 64,
            launch_nonce_digest=sha256(b"launch-nonce").hexdigest(),
            base_url=str(kwargs["base_url"]),
            version="0.5.0",
            created_at="2026-07-29T12:00:00+00:00",
        )

    with pytest.raises(
        RuntimeError,
        match="desktop_runtime_identity_mismatch",
    ):
        asyncio.run(
            run_desktop_sidecar(
                bootstrap,
                manifest_path=manifest,
                inspector=lambda pid: snapshots.get(pid),
                socket_factory=lambda: sock,
                identity_factory=mismatched_identity,
                lease_acquirer=lambda profile_root, identity: pytest.fail(
                    f"lease acquired for invalid identity: {profile_root} {identity}"
                ),
            )
        )

    assert not desktop_failure_path(launch).exists()
    assert sock.fileno() == -1


def test_preflight_failure_closes_socket_before_releasing_lease(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "kestrel-resource-manifest.json"
    manifest.write_bytes(
        b'{"build_mode":"release","schema":"kestrel.desktop.resources.v1"}\n'
    )
    parent_pid = os.getppid()
    current_pid = os.getpid()
    launch = replace(
        _launch(
            tmp_path,
            manifest_digest="sha256:" + sha256(manifest.read_bytes()).hexdigest(),
        ),
        parent_pid=parent_pid,
        parent_birth_marker="verified-parent-birth",
    )
    bootstrap = tmp_path / "bootstrap.json"
    _write_bootstrap(bootstrap, launch)
    readiness_path = desktop_readiness_path(launch)
    sock = bind_desktop_socket()
    events: list[str] = []
    snapshots = {
        parent_pid: _snapshot(parent_pid, birth_marker="verified-parent-birth"),
        current_pid: _snapshot(current_pid, birth_marker="verified-sidecar-birth"),
    }

    def identity_factory(**kwargs: object) -> RuntimeLeaseIdentity:
        return RuntimeLeaseIdentity(
            profile_id="default",
            management="desktop",
            owner_digest="1" * 64,
            pid=current_pid,
            process_birth_marker="verified-sidecar-birth",
            executable_digest="2" * 64,
            launch_nonce_digest=sha256(b"launch-nonce").hexdigest(),
            base_url=str(kwargs["base_url"]),
            version="0.5.0",
            created_at="2026-07-29T12:00:00+00:00",
        )

    class FakeLease:
        def release(self) -> None:
            assert sock.fileno() == -1
            events.append("release")

    def lease_acquirer(
        profile_root: Path,
        identity: RuntimeLeaseIdentity,
    ) -> FakeLease:
        del profile_root, identity
        events.append("lease")
        return FakeLease()

    def fail_preflight(memory_dir: Path) -> None:
        assert memory_dir == launch.memory_dir
        events.append("preflight")
        raise RuntimeError("sentinel_preflight_failure")

    with pytest.raises(RuntimeError, match="sentinel_preflight_failure"):
        asyncio.run(
            run_desktop_sidecar(
                bootstrap,
                manifest_path=manifest,
                inspector=lambda pid: snapshots.get(pid),
                socket_factory=lambda: sock,
                identity_factory=identity_factory,
                lease_acquirer=lease_acquirer,
                preflight=fail_preflight,
                app_factory=lambda config, *, desktop_context, desktop_shutdown: pytest.fail(
                    f"app opened after failed preflight: {config} {desktop_context}"
                ),
            )
        )

    assert events == ["lease", "preflight", "release"]
    assert not readiness_path.exists()
    assert not bootstrap.exists()
    failure_payload = json.loads(desktop_failure_path(launch).read_text(encoding="utf-8"))
    assert failure_payload["reason"] == "memvid_reopen_failed"
    assert launch.api_token not in json.dumps(
        failure_payload,
        sort_keys=True,
    )


def test_post_publication_failure_removes_owned_readiness_before_lease_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, manifest, launch, snapshots, identity_factory = _verified_sidecar_inputs(tmp_path)
    readiness_path = desktop_readiness_path(launch)
    sock = bind_desktop_socket()
    events: list[str] = []
    real_write = desktop_sidecar_module.write_desktop_readiness

    def fail_after_publication(
        path: Path,
        readiness: DesktopSidecarReadiness,
    ) -> None:
        real_write(path, readiness)
        assert path.exists()
        events.append("published")
        raise RuntimeError("sentinel_post_publication_failure")

    monkeypatch.setattr(
        desktop_sidecar_module,
        "write_desktop_readiness",
        fail_after_publication,
    )

    class FakeLease:
        def release(self) -> None:
            assert sock.fileno() == -1
            events.append("release")

    with pytest.raises(RuntimeError, match="sentinel_post_publication_failure"):
        asyncio.run(
            run_desktop_sidecar(
                bootstrap,
                manifest_path=manifest,
                inspector=lambda pid: snapshots.get(pid),
                socket_factory=lambda: sock,
                identity_factory=identity_factory,
                lease_acquirer=lambda profile_root, identity: FakeLease(),
                preflight=lambda memory_dir: None,
                app_factory=lambda config, *, desktop_context, desktop_shutdown: object(),
                server_factory=lambda app: pytest.fail(
                    f"server started after readiness publication failed: {app}"
                ),
            )
        )

    assert events == ["published", "release"]
    assert not readiness_path.exists()


def test_readiness_removal_failure_still_closes_socket_and_releases_lease_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, manifest, launch, snapshots, identity_factory = _verified_sidecar_inputs(tmp_path)
    sock = bind_desktop_socket()
    events: list[str] = []

    def fail_removal(
        path: Path,
        readiness: DesktopSidecarReadiness,
    ) -> bool:
        del path, readiness
        events.append("remove")
        raise RuntimeError("sentinel_readiness_removal_failure")

    monkeypatch.setattr(
        desktop_sidecar_module,
        "remove_owned_desktop_readiness",
        fail_removal,
    )

    class FakeLease:
        def release(self) -> None:
            assert sock.fileno() == -1
            events.append("release")

    class ReturningServer:
        async def serve(self, *, sockets: list[socket.socket]) -> None:
            assert sockets == [sock]
            events.append("serve")

    with pytest.raises(RuntimeError, match="desktop_sidecar_cleanup_incomplete"):
        asyncio.run(
            run_desktop_sidecar(
                bootstrap,
                manifest_path=manifest,
                inspector=lambda pid: snapshots.get(pid),
                socket_factory=lambda: sock,
                identity_factory=identity_factory,
                lease_acquirer=lambda profile_root, identity: FakeLease(),
                preflight=lambda memory_dir: None,
                app_factory=lambda config, *, desktop_context, desktop_shutdown: object(),
                server_factory=lambda app: ReturningServer(),
            )
        )

    assert events == ["serve", "remove", "release"]
    assert sock.fileno() == -1


def test_server_failure_remains_primary_when_readiness_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap, manifest, launch, snapshots, identity_factory = _verified_sidecar_inputs(tmp_path)
    sock = bind_desktop_socket()
    events: list[str] = []

    def fail_removal(
        path: Path,
        readiness: DesktopSidecarReadiness,
    ) -> bool:
        del path, readiness
        events.append("remove")
        raise RuntimeError("sentinel_readiness_removal_failure")

    monkeypatch.setattr(
        desktop_sidecar_module,
        "remove_owned_desktop_readiness",
        fail_removal,
    )

    class FakeLease:
        def release(self) -> None:
            assert sock.fileno() == -1
            events.append("release")

    class FailingServer:
        async def serve(self, *, sockets: list[socket.socket]) -> None:
            assert sockets == [sock]
            events.append("serve")
            raise RuntimeError("sentinel_server_failure")

    with pytest.raises(RuntimeError, match="sentinel_server_failure") as caught:
        asyncio.run(
            run_desktop_sidecar(
                bootstrap,
                manifest_path=manifest,
                inspector=lambda pid: snapshots.get(pid),
                socket_factory=lambda: sock,
                identity_factory=identity_factory,
                lease_acquirer=lambda profile_root, identity: FakeLease(),
                preflight=lambda memory_dir: None,
                app_factory=lambda config, *, desktop_context, desktop_shutdown: object(),
                server_factory=lambda app: FailingServer(),
            )
        )

    assert events == ["serve", "remove", "release"]
    assert sock.fileno() == -1
    assert any(
        "readiness" in note and "cleanup" in note for note in getattr(caught.value, "__notes__", ())
    )


def test_desktop_server_uses_bootstrap_settings_and_locks_memvid_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch = _launch(tmp_path)
    base = build_desktop_agent_config(launch)
    assert base.secret_backend == "desktop"
    assert base.secret_store_path == (
        launch.profile_root / "secrets" / "desktop-keyring-metadata.json"
    )
    outside_memory = tmp_path / "outside-memory"
    configured = replace(
        base,
        provider="codex-cli",
        model="gpt-5.4",
        backend="memory",
        memory_dir=outside_memory,
    )
    RuntimeSettingsStore(launch.runtime_settings_path).save(RuntimeSettings.from_config(configured))
    captured: list[AgentConfig] = []

    class StopConstruction(RuntimeError):
        pass

    def capture(
        config: AgentConfig,
        *,
        harden_existing_memory: bool = True,
    ) -> None:
        assert harden_existing_memory is False
        captured.append(config)
        raise StopConstruction

    monkeypatch.setattr(server_module, "_prepare_private_runtime_artifacts", capture)

    with pytest.raises(StopConstruction):
        server_module.create_app(base, desktop_context=launch)

    assert len(captured) == 1
    assert captured[0].provider == "codex-cli"
    assert captured[0].model == "gpt-5.4"
    assert captured[0].backend == "memvid"
    assert captured[0].memory_dir == launch.memory_dir
    assert captured[0].state_path == launch.state_path
    assert captured[0].secret_backend == "desktop"
    assert captured[0].secret_store_path == (
        launch.profile_root / "secrets" / "desktop-keyring-metadata.json"
    )


def test_desktop_runtime_update_canonicalizes_memory_authority_everywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch = _launch(tmp_path)
    base = build_desktop_agent_config(launch)
    outside_memory = tmp_path / "outside-memory"
    store = RuntimeSettingsStore(launch.runtime_settings_path)
    store.save(
        RuntimeSettings.from_config(
            replace(
                base,
                backend="memory",
                memory_dir=outside_memory,
            )
        )
    )
    captured_runs: list[Any] = []
    captured_channels: list[Any] = []
    real_build_run_manager = build_run_manager
    real_channel_manager = ChannelManager

    def capture_run_manager(**kwargs: Any) -> Any:
        result = real_build_run_manager(**kwargs)
        captured_runs.append(result.runs)
        return result

    def capture_channel_manager(*args: Any, **kwargs: Any) -> Any:
        manager = real_channel_manager(*args, **kwargs)
        captured_channels.append(manager)
        return manager

    monkeypatch.setattr(server_module, "build_run_manager", capture_run_manager)
    monkeypatch.setattr(server_module, "ChannelManager", capture_channel_manager)

    app = server_module.create_app(base, desktop_context=launch)
    headers = {"Authorization": f"Bearer {launch.api_token}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        initial = client.get("/api/runtime/settings", headers=headers)
        assert initial.status_code == 200
        initial_settings = initial.json()["settings"]
        assert initial_settings["backend"] == "memvid"
        assert initial_settings["memory_dir"] == str(launch.memory_dir)
        migrated = json.loads(launch.runtime_settings_path.read_text(encoding="utf-8"))
        assert migrated["backend"] == "memvid"
        assert migrated["memory_dir"] == str(launch.memory_dir)
        updated = client.put(
            "/api/runtime/settings",
            headers=headers,
            json={
                "expected_revision": initial_settings["revision"],
                "provider": "codex-cli",
                "model": "gpt-5.4",
                "max_tool_rounds": 9,
                "backend": "memory",
                "memory_dir": str(outside_memory),
            },
        )
        settings = client.get("/api/runtime/settings", headers=headers)
        runtime = client.get("/api/runtime/config", headers=headers)

    assert updated.status_code == 200
    updated_payload = updated.json()
    assert updated_payload["settings"]["provider"] == "codex-cli"
    assert updated_payload["settings"]["max_tool_rounds"] == 9
    assert updated_payload["settings"]["backend"] == "memvid"
    assert updated_payload["settings"]["memory_dir"] == str(launch.memory_dir)
    assert updated_payload["runtime"]["backend"] == "memvid"
    assert updated_payload["runtime"]["memory_dir"] == str(launch.memory_dir)

    persisted = json.loads(launch.runtime_settings_path.read_text(encoding="utf-8"))
    assert persisted["provider"] == "codex-cli"
    assert persisted["max_tool_rounds"] == 9
    assert persisted["backend"] == "memvid"
    assert persisted["memory_dir"] == str(launch.memory_dir)
    assert persisted["revision"] == updated_payload["settings"]["revision"]

    assert settings.status_code == 200
    saved_settings = settings.json()["settings"]
    assert saved_settings["provider"] == "codex-cli"
    assert saved_settings["max_tool_rounds"] == 9
    assert saved_settings["backend"] == "memvid"
    assert saved_settings["memory_dir"] == str(launch.memory_dir)

    assert runtime.status_code == 200
    runtime_payload = runtime.json()
    assert runtime_payload["provider"]["name"] == "codex-cli"
    assert runtime_payload["limits"]["max_tool_rounds"] == 9
    assert runtime_payload["paths"]["memory_dir"] == str(launch.memory_dir)
    assert runtime_payload["settings"]["runtime"]["backend"] == "memvid"
    assert runtime_payload["settings"]["runtime"]["memory_dir"] == str(launch.memory_dir)

    assert len(captured_runs) == 1
    assert captured_runs[0].config.provider == "codex-cli"
    assert captured_runs[0].config.max_tool_rounds == 9
    assert captured_runs[0].config.backend == "memvid"
    assert captured_runs[0].config.memory_dir == launch.memory_dir
    assert len(captured_channels) == 1
    assert captured_channels[0].config.provider == "codex-cli"
    assert captured_channels[0].config.max_tool_rounds == 9
    assert captured_channels[0].config.backend == "memvid"
    assert captured_channels[0].config.memory_dir == launch.memory_dir
