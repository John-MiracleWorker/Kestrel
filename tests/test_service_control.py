from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.file_lock import lock_exclusive, unlock
from nested_memvid_agent.private_artifacts import (
    open_private_file_descriptor,
    write_private_text,
)
from nested_memvid_agent.server_client import ServerProbe
from nested_memvid_agent.service_control import (
    ProcessSnapshot,
    ServiceController,
    ServiceManagement,
    ServiceState,
    resolve_kestrel_home,
    resolve_service_paths,
)


class FakeInspector:
    def __init__(
        self,
        *,
        processes: dict[int, ProcessSnapshot] | None = None,
        listeners: tuple[int, ...] = (),
        live_groups: frozenset[int] = frozenset(),
        bindable: bool = True,
    ) -> None:
        self.processes = processes or {}
        self.listeners = listeners
        self.live_groups = live_groups
        self.bindable = bindable
        self.signals: list[tuple[str, int, int]] = []

    def process(self, pid: int) -> ProcessSnapshot | None:
        return self.processes.get(pid)

    def listener_pids(self, host: str, port: int) -> tuple[int, ...]:
        assert host == "127.0.0.1"
        assert port > 0
        return self.listeners

    def group_has_live_members(self, pgid: int) -> bool:
        return pgid in self.live_groups

    def port_is_bindable(self, host: str, port: int) -> bool:
        assert host == "127.0.0.1"
        assert port > 0
        return self.bindable


class FakeClient:
    def __init__(self, probe: ServerProbe) -> None:
        self._probe = probe
        self.probe_count = 0

    def probe(self) -> ServerProbe:
        self.probe_count += 1
        return self._probe


def _installation(home: Path, *, with_venv: bool = True) -> Path:
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "installer-server-supervisor.sh").write_text(
        "#!/usr/bin/env bash\n",
        encoding="utf-8",
    )
    (home / "pyproject.toml").write_text(
        "[project]\nname='nested-memvid-agent'\n",
        encoding="utf-8",
    )
    (home / "src" / "nested_memvid_agent").mkdir(parents=True)
    if with_venv:
        executable = home / ".venv" / "bin" / "nest-agent"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/usr/bin/env python\n", encoding="utf-8")
        executable.chmod(0o755)
    return home


def _server_snapshot(paths: Any, *, pid: int = 201, **changes: object) -> ProcessSnapshot:
    command = (
        str(paths.server_executable),
        "server",
        "--backend",
        "memvid",
        "--memory-dir",
        str(paths.memory_dir),
        "--state-path",
        str(paths.state_path),
        "--provider",
        "mock",
        "--model",
        "mock",
        "--host",
        paths.host,
        "--port",
        str(paths.port),
    )
    snapshot = ProcessSnapshot(
        pid=pid,
        uid=os.getuid(),
        cwd=paths.home,
        command=command,
        pgid=pid,
        state="S",
    )
    return replace(snapshot, **changes)


def _supervisor_snapshot(
    paths: Any,
    server: ProcessSnapshot,
    *,
    pid: int = 200,
) -> ProcessSnapshot:
    return ProcessSnapshot(
        pid=pid,
        uid=os.getuid(),
        cwd=paths.home,
        command=(
            "bash",
            str(paths.supervisor_script),
            "--pid-file",
            str(paths.pid_path),
            "--supervisor-pid-file",
            str(paths.supervisor_pid_path),
            "--process-group-file",
            str(paths.pgid_path),
            "--log-file",
            str(paths.log_path),
            "--",
            *server.command,
        ),
        pgid=pid,
        state="S",
    )


def _managed_metadata(paths: Any, *, server_pid: int = 201, supervisor_pid: int = 200) -> None:
    write_private_text(paths.pid_path, f"{server_pid}\n")
    write_private_text(paths.supervisor_pid_path, f"{supervisor_pid}\n")
    write_private_text(paths.pgid_path, f"{server_pid}\n")


def test_home_resolution_uses_the_documented_precedence(tmp_path: Path) -> None:
    explicit = _installation(tmp_path / "explicit")
    configured = _installation(tmp_path / "configured")
    embedded = _installation(tmp_path / "embedded")
    checkout = _installation(tmp_path / "checkout", with_venv=False)
    fallback = _installation(tmp_path / "user" / ".kestrel-agent")
    environ = {
        "KESTREL_HOME": str(configured),
        "HOME": str(tmp_path / "user"),
    }

    assert (
        resolve_kestrel_home(
            explicit_home=explicit,
            environ=environ,
            embedded_home=embedded,
            cwd=checkout,
        )
        == explicit.resolve()
    )
    assert (
        resolve_kestrel_home(
            explicit_home=None,
            environ=environ,
            embedded_home=embedded,
            cwd=checkout,
        )
        == configured.resolve()
    )
    assert (
        resolve_kestrel_home(
            explicit_home=None,
            environ={"HOME": str(tmp_path / "user")},
            embedded_home=embedded,
            cwd=checkout,
        )
        == embedded.resolve()
    )
    assert (
        resolve_kestrel_home(
            explicit_home=None,
            environ={"HOME": str(tmp_path / "user")},
            embedded_home=None,
            cwd=checkout,
        )
        == checkout.resolve()
    )
    assert (
        resolve_kestrel_home(
            explicit_home=None,
            environ={"HOME": str(tmp_path / "user")},
            embedded_home=None,
            cwd=tmp_path,
        )
        == fallback.resolve()
    )


def test_home_resolution_rejects_missing_or_incomplete_installations(
    tmp_path: Path,
) -> None:
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()

    with pytest.raises(ValueError, match="Kestrel installation"):
        resolve_kestrel_home(
            explicit_home=incomplete,
            environ={"HOME": str(tmp_path)},
            embedded_home=None,
            cwd=tmp_path,
        )


@pytest.mark.parametrize("configured", ["0", "-1", "65536", "not-a-port", "1.5"])
def test_service_paths_reject_invalid_ports(
    tmp_path: Path,
    configured: str,
) -> None:
    home = _installation(tmp_path / "home")

    with pytest.raises(ValueError, match="port"):
        resolve_service_paths(home, environ={"KESTREL_PORT": configured})


def test_service_paths_are_canonical_loopback_paths(tmp_path: Path) -> None:
    home = _installation(tmp_path / "home")

    paths = resolve_service_paths(
        home,
        port=18765,
        environ={"KESTREL_PORT": "28765"},
    )

    assert paths.home == home.resolve()
    assert paths.host == "127.0.0.1"
    assert paths.port == 18765
    assert paths.url == "http://127.0.0.1:18765/"
    assert paths.state_path == home.resolve() / ".nest" / "state" / "agent.db"
    assert paths.memory_dir == home.resolve() / ".nest" / "memory"
    assert paths.lifecycle_lock_path == home.resolve() / ".nest" / "server.lifecycle.lock"


def test_status_recognizes_a_verified_managed_service(tmp_path: Path) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    supervisor = _supervisor_snapshot(paths, server)
    _managed_metadata(paths)
    client = FakeClient(ServerProbe(True, True, False))
    controller = ServiceController(
        paths,
        inspector=FakeInspector(
            processes={server.pid: server, supervisor.pid: supervisor},
            listeners=(server.pid,),
            live_groups=frozenset({server.pgid}),
            bindable=False,
        ),
        client=client,
    )

    status = controller.status()

    assert status.state == ServiceState.RUNNING
    assert status.management == ServiceManagement.MANAGED
    assert status.pid == server.pid
    assert status.supervisor_pid == supervisor.pid
    assert status.pgid == server.pgid
    assert client.probe_count == 1


def test_status_recognizes_a_verified_external_service_without_adopting_it(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    controller = ServiceController(
        paths,
        inspector=FakeInspector(
            processes={server.pid: server},
            listeners=(server.pid,),
            live_groups=frozenset({server.pgid}),
            bindable=False,
        ),
        client=FakeClient(ServerProbe(True, True, False)),
    )

    status = controller.status()

    assert status.state == ServiceState.RUNNING
    assert status.management == ServiceManagement.EXTERNAL
    assert status.pid == server.pid
    assert not paths.pid_path.exists()
    assert not paths.supervisor_pid_path.exists()
    assert not paths.pgid_path.exists()


def test_status_treats_authenticated_service_as_running_but_locked(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    _managed_metadata(paths)
    supervisor = _supervisor_snapshot(paths, server)
    controller = ServiceController(
        paths,
        inspector=FakeInspector(
            processes={server.pid: server, supervisor.pid: supervisor},
            listeners=(server.pid,),
            live_groups=frozenset({server.pgid}),
            bindable=False,
        ),
        client=FakeClient(
            ServerProbe(True, False, True, "API token is required")
        ),
    )

    status = controller.status()

    assert status.state == ServiceState.RUNNING
    assert status.management == ServiceManagement.MANAGED
    assert "locked" in status.detail.lower()


def test_status_reports_verified_supervisor_startup_in_progress(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    supervisor = _supervisor_snapshot(paths, server)
    write_private_text(paths.supervisor_pid_path, f"{supervisor.pid}\n")
    controller = ServiceController(
        paths,
        inspector=FakeInspector(
            processes={supervisor.pid: supervisor},
            listeners=(),
            bindable=True,
        ),
        client=FakeClient(ServerProbe(False, False, False, "offline")),
    )

    status = controller.status()

    assert status.state == ServiceState.STARTING
    assert status.management == ServiceManagement.MANAGED
    assert status.supervisor_pid == supervisor.pid


def test_status_treats_safe_absent_metadata_as_stale_but_stopped(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    _managed_metadata(paths)
    controller = ServiceController(
        paths,
        inspector=FakeInspector(),
        client=FakeClient(ServerProbe(False, False, False, "offline")),
    )
    before = {
        path: path.read_text(encoding="utf-8")
        for path in (paths.pid_path, paths.supervisor_pid_path, paths.pgid_path)
    }

    status = controller.status()

    assert status.state == ServiceState.STOPPED
    assert status.management == ServiceManagement.NONE
    assert "stale" in status.detail.lower()
    assert {
        path: path.read_text(encoding="utf-8") for path in before
    } == before


@pytest.mark.parametrize(
    "unsafe_kind",
    ["symlink", "permissive", "nonnumeric"],
)
def test_status_refuses_unsafe_lifecycle_metadata(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    paths.pid_path.parent.mkdir(parents=True)
    if unsafe_kind == "symlink":
        target = paths.pid_path.with_name("target.pid")
        target.write_text("201\n", encoding="utf-8")
        paths.pid_path.symlink_to(target)
    elif unsafe_kind == "permissive":
        paths.pid_path.write_text("201\n", encoding="utf-8")
        paths.pid_path.chmod(0o644)
    else:
        write_private_text(paths.pid_path, "not-a-pid\n")
    controller = ServiceController(
        paths,
        inspector=FakeInspector(),
        client=FakeClient(ServerProbe(False, False, False, "offline")),
    )

    status = controller.status()

    assert status.state == ServiceState.CONFLICT
    assert status.management == ServiceManagement.NONE
    assert "metadata" in status.detail.lower()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("uid", os.getuid() + 1),
        ("cwd", Path("/tmp/not-kestrel-home")),
        ("command", ("python", "-m", "http.server", "18765")),
    ],
)
def test_status_refuses_reused_or_mismatched_server_pids(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths, **{field: replacement})
    _managed_metadata(paths)
    controller = ServiceController(
        paths,
        inspector=FakeInspector(
            processes={server.pid: server},
            listeners=(server.pid,),
            bindable=False,
        ),
        client=FakeClient(ServerProbe(True, True, False)),
    )

    status = controller.status()

    assert status.state == ServiceState.CONFLICT
    assert "identity" in status.detail.lower()


def test_status_refuses_a_healthy_api_with_unverified_listener_identity(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    unknown = ProcessSnapshot(
        pid=909,
        uid=os.getuid(),
        cwd=tmp_path,
        command=("python", "-m", "http.server", "18765"),
        pgid=909,
        state="S",
    )
    controller = ServiceController(
        paths,
        inspector=FakeInspector(
            processes={unknown.pid: unknown},
            listeners=(unknown.pid,),
            bindable=False,
        ),
        client=FakeClient(ServerProbe(True, True, False)),
    )

    status = controller.status()

    assert status.state == ServiceState.CONFLICT
    assert status.management == ServiceManagement.NONE
    assert status.pid == unknown.pid
    assert "listener" in status.detail.lower()


def test_status_reports_stopped_without_metadata_or_listener(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    client = FakeClient(ServerProbe(False, False, False, "offline"))
    status = ServiceController(
        paths,
        inspector=FakeInspector(bindable=True),
        client=client,
    ).status()

    assert status.state == ServiceState.STOPPED
    assert status.management == ServiceManagement.NONE
    assert client.probe_count == 0


def test_status_reports_lifecycle_lock_contention_without_waiting(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    descriptor = open_private_file_descriptor(paths.lifecycle_lock_path)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    lock_exclusive(handle, blocking=False)
    try:
        status = ServiceController(
            paths,
            inspector=FakeInspector(bindable=True),
            client=FakeClient(ServerProbe(False, False, False, "offline")),
        ).status()
    finally:
        unlock(handle)
        handle.close()

    assert status.state == ServiceState.STOPPED
    assert status.lifecycle_busy is True
    assert "lifecycle" in status.detail.lower()
