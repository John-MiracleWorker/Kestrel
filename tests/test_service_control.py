from __future__ import annotations

import ctypes
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import nested_memvid_agent.service_control as service_control
from nested_memvid_agent.file_lock import lock_exclusive, unlock
from nested_memvid_agent.private_artifacts import (
    open_private_file_descriptor,
    write_private_text,
)
from nested_memvid_agent.server_client import ServerProbe
from nested_memvid_agent.service_control import (
    BoundListener,
    ProcessSnapshot,
    ServiceControlError,
    ServiceController,
    ServiceManagement,
    ServiceState,
    SystemProcessInspector,
    SystemProcessSignaler,
    resolve_kestrel_home,
    resolve_service_paths,
)


class FakeInspector:
    def __init__(
        self,
        *,
        processes: dict[int, ProcessSnapshot] | None = None,
        listeners: tuple[BoundListener, ...] = (),
        live_groups: frozenset[int] = frozenset(),
        bindable: bool = True,
    ) -> None:
        self.processes = processes or {}
        self.bound_listeners = listeners
        self.live_groups = live_groups
        self.bindable = bindable
        self.signals: list[tuple[str, int, int]] = []

    def process(self, pid: int) -> ProcessSnapshot | None:
        return self.processes.get(pid)

    def listeners(self, host: str, port: int) -> tuple[BoundListener, ...]:
        assert host == "127.0.0.1"
        assert port > 0
        return self.bound_listeners

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


class FakeSignaler:
    def __init__(
        self,
        callback: Any | None = None,
    ) -> None:
        self.callback = callback
        self.calls: list[tuple[str, int, int]] = []

    def signal_pid(self, pid: int, signal_number: int) -> None:
        self.calls.append(("pid", pid, signal_number))
        if self.callback is not None:
            self.callback("pid", pid, signal_number)

    def signal_group(self, pgid: int, signal_number: int) -> None:
        self.calls.append(("group", pgid, signal_number))
        if self.callback is not None:
            self.callback("group", pgid, signal_number)


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
        birth_marker=f"proc-start-ticks:{pid * 100}",
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
        birth_marker=f"proc-start-ticks:{pid * 100}",
    )


def _managed_metadata(paths: Any, *, server_pid: int = 201, supervisor_pid: int = 200) -> None:
    write_private_text(paths.pid_path, f"{server_pid}\n")
    write_private_text(paths.supervisor_pid_path, f"{supervisor_pid}\n")
    write_private_text(paths.pgid_path, f"{server_pid}\n")


def _listener(paths: Any, pid: int) -> BoundListener:
    return BoundListener(pid=pid, host=paths.host, port=paths.port)


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


def test_service_paths_allow_only_relative_state_and_memory_paths_contained_by_home(
    tmp_path: Path,
) -> None:
    home = _installation(tmp_path / "home")

    paths = resolve_service_paths(
        home,
        environ={
            "NEST_AGENT_STATE_PATH": ".nest/alternate/agent.db",
            "NEST_AGENT_MEMORY_DIR": ".nest/alternate/memory",
        },
    )

    assert paths.state_path == home.resolve() / ".nest" / "alternate" / "agent.db"
    assert paths.memory_dir == home.resolve() / ".nest" / "alternate" / "memory"


@pytest.mark.parametrize(
    ("setting", "configured"),
    [
        ("NEST_AGENT_STATE_PATH", "../outside/agent.db"),
        ("NEST_AGENT_MEMORY_DIR", "../../outside/memory"),
    ],
)
def test_service_paths_reject_relative_state_and_memory_escapes(
    tmp_path: Path,
    setting: str,
    configured: str,
) -> None:
    home = _installation(tmp_path / "home")

    with pytest.raises(ValueError, match="contained"):
        resolve_service_paths(home, environ={setting: configured})


def test_status_rejects_port_only_and_non_loopback_listener_records(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    for listener in (
        BoundListener(pid=server.pid, host="*", port=paths.port),
        BoundListener(pid=server.pid, host="0.0.0.0", port=paths.port),
        BoundListener(pid=server.pid, host="::1", port=paths.port),
        BoundListener(pid=server.pid, host=paths.host, port=paths.port + 1),
    ):
        status = ServiceController(
            paths,
            inspector=FakeInspector(
                processes={server.pid: server},
                listeners=(listener,),
                bindable=False,
            ),
            client=FakeClient(ServerProbe(True, True, False)),
        ).status()

        assert status.state == ServiceState.CONFLICT
        assert status.management == ServiceManagement.NONE


def test_status_rejects_port_only_listener_pids_without_an_endpoint(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    status = ServiceController(
        paths,
        inspector=FakeInspector(
            processes={server.pid: server},
            listeners=(server.pid,),  # type: ignore[arg-type]
            bindable=False,
        ),
        client=FakeClient(ServerProbe(True, True, False)),
    ).status()

    assert status.state == ServiceState.CONFLICT
    assert status.management == ServiceManagement.NONE


@pytest.mark.parametrize(
    "extra_option",
    [
        ("--backend", "not-memvid"),
        ("--memory-dir", ".nest/other-memory"),
        ("--state-path", ".nest/other-state.db"),
        ("--host", "0.0.0.0"),
        ("--port", "18766"),
    ],
)
def test_status_rejects_duplicate_server_identity_options(
    tmp_path: Path,
    extra_option: tuple[str, str],
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    command = (*server.command, *extra_option)
    conflicting = replace(server, command=command)

    status = ServiceController(
        paths,
        inspector=FakeInspector(
            processes={conflicting.pid: conflicting},
            listeners=(_listener(paths, conflicting.pid),),
            bindable=False,
        ),
        client=FakeClient(ServerProbe(True, True, False)),
    ).status()

    assert status.state == ServiceState.CONFLICT


@pytest.mark.parametrize(
    "extra_option",
    [
        ("--pid-file", ".nest/other-server.pid"),
        ("--supervisor-pid-file", ".nest/other-supervisor.pid"),
        ("--process-group-file", ".nest/other-server.pgid"),
        ("--log-file", ".nest/other-server.log"),
    ],
)
def test_status_rejects_duplicate_supervisor_identity_options(
    tmp_path: Path,
    extra_option: tuple[str, str],
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    supervisor = _supervisor_snapshot(paths, server)
    _managed_metadata(paths)
    separator = supervisor.command.index("--")
    command = (
        *supervisor.command[:separator],
        *extra_option,
        *supervisor.command[separator:],
    )
    conflicting = replace(supervisor, command=command)

    status = ServiceController(
        paths,
        inspector=FakeInspector(
            processes={server.pid: server, conflicting.pid: conflicting},
            listeners=(_listener(paths, server.pid),),
            live_groups=frozenset({server.pgid}),
            bindable=False,
        ),
        client=FakeClient(ServerProbe(True, True, False)),
    ).status()

    assert status.state == ServiceState.CONFLICT


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
            listeners=(_listener(paths, server.pid),),
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
            listeners=(_listener(paths, server.pid),),
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
            listeners=(_listener(paths, server.pid),),
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
            listeners=(_listener(paths, server.pid),),
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
            listeners=(_listener(paths, unknown.pid),),
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


def test_start_reuses_running_managed_and_external_services(
    tmp_path: Path,
) -> None:
    for name, managed in (("managed", True), ("external", False)):
        paths = resolve_service_paths(
            _installation(tmp_path / name),
            port=18765 if managed else 18766,
        )
        server = _server_snapshot(paths)
        processes = {server.pid: server}
        if managed:
            supervisor = _supervisor_snapshot(paths, server)
            processes[supervisor.pid] = supervisor
            _managed_metadata(paths)
        inspector = FakeInspector(
            processes=processes,
            listeners=(_listener(paths, server.pid),),
            live_groups=frozenset({server.pgid}),
            bindable=False,
        )

        status = ServiceController(
            paths,
            inspector=inspector,
            client=FakeClient(ServerProbe(True, True, False)),
            popen=lambda *_args, **_kwargs: pytest.fail(
                "healthy service must not relaunch"
            ),
        ).start()

        assert status.state == ServiceState.RUNNING
        assert status.management == (
            ServiceManagement.MANAGED
            if managed
            else ServiceManagement.EXTERNAL
        )


def test_start_launches_the_existing_supervisor_with_safe_absolute_arguments(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    inspector = FakeInspector()
    launches: list[tuple[list[str], dict[str, object]]] = []

    def launch(command: list[str], **kwargs: object) -> object:
        launches.append((command, kwargs))
        server = _server_snapshot(paths)
        supervisor = _supervisor_snapshot(paths, server)
        _managed_metadata(paths)
        inspector.processes = {
            server.pid: server,
            supervisor.pid: supervisor,
        }
        inspector.bound_listeners = (_listener(paths, server.pid),)
        inspector.live_groups = frozenset({server.pgid})
        inspector.bindable = False
        return SimpleNamespace(pid=supervisor.pid, poll=lambda: None)

    status = ServiceController(
        paths,
        inspector=inspector,
        client=FakeClient(ServerProbe(True, True, False)),
        popen=launch,
    ).start()

    assert status.state == ServiceState.RUNNING
    assert status.management == ServiceManagement.MANAGED
    assert len(launches) == 1
    command, kwargs = launches[0]
    assert command[:2] == ["bash", str(paths.supervisor_script)]
    assert command[command.index("--") + 1 :] == list(
        _server_snapshot(paths).command
    )
    assert kwargs["cwd"] == paths.home
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["shell"] is False


def test_artifact_preparation_repairs_existing_sensitive_directories_to_owner_only(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    for directory in {
        paths.lifecycle_lock_path.parent,
        paths.memory_dir,
        paths.state_path.parent,
        paths.log_path.parent,
        paths.pid_path.parent,
        paths.supervisor_pid_path.parent,
        paths.pgid_path.parent,
    }:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)

    ServiceController(paths)._prepare_service_artifacts()

    for directory in {
        paths.lifecycle_lock_path.parent,
        paths.memory_dir,
        paths.state_path.parent,
        paths.log_path.parent,
        paths.pid_path.parent,
        paths.supervisor_pid_path.parent,
        paths.pgid_path.parent,
    }:
        assert directory.stat().st_mode & 0o777 == 0o700


def test_concurrent_start_calls_serialize_to_one_supervisor(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    inspector = FakeInspector()
    entered = threading.Event()
    release = threading.Event()
    launch_count = 0
    launch_guard = threading.Lock()

    def launch(_command: list[str], **_kwargs: object) -> object:
        nonlocal launch_count
        with launch_guard:
            launch_count += 1
        entered.set()
        assert release.wait(timeout=2)
        server = _server_snapshot(paths)
        supervisor = _supervisor_snapshot(paths, server)
        _managed_metadata(paths)
        inspector.processes = {
            server.pid: server,
            supervisor.pid: supervisor,
        }
        inspector.bound_listeners = (_listener(paths, server.pid),)
        inspector.live_groups = frozenset({server.pgid})
        inspector.bindable = False
        return SimpleNamespace(pid=supervisor.pid, poll=lambda: None)

    controller = ServiceController(
        paths,
        inspector=inspector,
        client=FakeClient(ServerProbe(True, True, False)),
        popen=launch,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(controller.start)
        assert entered.wait(timeout=2)
        second = pool.submit(controller.start)
        release.set()
        statuses = (first.result(timeout=3), second.result(timeout=3))

    assert launch_count == 1
    assert all(status.state == ServiceState.RUNNING for status in statuses)
    assert statuses[0].pid == statuses[1].pid


def test_start_removes_only_proven_stale_metadata_before_launch(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    _managed_metadata(paths, server_pid=701, supervisor_pid=700)
    inspector = FakeInspector()

    def launch(_command: list[str], **_kwargs: object) -> object:
        assert not paths.pid_path.exists()
        assert not paths.supervisor_pid_path.exists()
        assert not paths.pgid_path.exists()
        server = _server_snapshot(paths)
        supervisor = _supervisor_snapshot(paths, server)
        _managed_metadata(paths)
        inspector.processes = {
            server.pid: server,
            supervisor.pid: supervisor,
        }
        inspector.bound_listeners = (_listener(paths, server.pid),)
        inspector.live_groups = frozenset({server.pgid})
        inspector.bindable = False
        return SimpleNamespace(pid=supervisor.pid, poll=lambda: None)

    status = ServiceController(
        paths,
        inspector=inspector,
        client=FakeClient(ServerProbe(True, True, False)),
        popen=launch,
    ).start()

    assert status.state == ServiceState.RUNNING
    assert paths.pid_path.read_text(encoding="utf-8").strip() == "201"


def test_stale_metadata_cleanup_refuses_inode_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    write_private_text(paths.pid_path, "701\n")
    real_stat = os.stat
    stat_calls = 0

    def stat_with_replacement(
        target: object,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal stat_calls
        if target == paths.pid_path.name and kwargs.get("dir_fd") is not None:
            stat_calls += 1
            if stat_calls == 2:
                paths.pid_path.unlink()
                paths.pid_path.write_text("999\n", encoding="utf-8")
                paths.pid_path.chmod(0o600)
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(service_control.os, "stat", stat_with_replacement)

    with pytest.raises(ServiceControlError) as exc_info:
        service_control._unlink_private_identifier(paths.pid_path, 701)

    assert exc_info.value.code == "identity_changed"
    assert paths.pid_path.read_text(encoding="utf-8") == "999\n"


def test_stale_metadata_cleanup_preserves_a_replacement_at_rename_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    write_private_text(paths.pid_path, "701\n")
    real_rename = os.rename

    def rename_after_replacement(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        if source == paths.pid_path.name:
            paths.pid_path.unlink()
            paths.pid_path.write_text("999\n", encoding="utf-8")
            paths.pid_path.chmod(0o600)
        real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(service_control.os, "rename", rename_after_replacement)

    with pytest.raises(ServiceControlError) as exc_info:
        service_control._unlink_private_identifier(paths.pid_path, 701)

    assert exc_info.value.code == "identity_changed"
    quarantined = tuple(paths.pid_path.parent.glob(".server.pid.*.cleanup"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "999\n"


def test_start_refuses_unknown_listener_without_launching(
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
            listeners=(_listener(paths, unknown.pid),),
            bindable=False,
        ),
        client=FakeClient(ServerProbe(True, True, False)),
        popen=lambda *_args, **_kwargs: pytest.fail(
            "conflict must not launch"
        ),
    )

    with pytest.raises(ServiceControlError) as exc_info:
        controller.start()

    assert exc_info.value.code == "service_conflict"


def test_start_rechecks_listener_ownership_after_artifact_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    unknown = ProcessSnapshot(
        pid=909,
        uid=os.getuid(),
        cwd=tmp_path,
        command=("python", "-m", "http.server", "18765"),
        pgid=909,
        state="S",
        birth_marker="other-service",
    )
    inspector = FakeInspector()
    controller = ServiceController(
        paths,
        inspector=inspector,
        client=FakeClient(ServerProbe(False, False, False, "offline")),
        popen=lambda *_args, **_kwargs: pytest.fail(
            "a listener appearing during preparation must prevent launch"
        ),
    )
    prepare = controller._prepare_service_artifacts

    def prepare_then_claim_port() -> None:
        prepare()
        inspector.processes[unknown.pid] = unknown
        inspector.bound_listeners = (_listener(paths, unknown.pid),)
        inspector.bindable = False

    monkeypatch.setattr(controller, "_prepare_service_artifacts", prepare_then_claim_port)

    with pytest.raises(ServiceControlError) as exc_info:
        controller.start()

    assert exc_info.value.code == "service_conflict"


def test_startup_timeout_cleans_up_only_its_verified_supervisor(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    inspector = FakeInspector()
    now = [0.0]

    def launch(_command: list[str], **_kwargs: object) -> object:
        server = _server_snapshot(paths)
        supervisor = _supervisor_snapshot(paths, server)
        write_private_text(paths.supervisor_pid_path, f"{supervisor.pid}\n")
        inspector.processes = {supervisor.pid: supervisor}
        return SimpleNamespace(pid=supervisor.pid, poll=lambda: None)

    def on_signal(kind: str, identifier: int, signal_number: int) -> None:
        assert kind == "pid"
        assert identifier == 200
        assert signal_number == signal.SIGTERM
        inspector.processes.clear()

    signaler = FakeSignaler(on_signal)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    controller = ServiceController(
        paths,
        inspector=inspector,
        client=FakeClient(ServerProbe(False, False, False, "offline")),
        popen=launch,
        signaler=signaler,
    )
    with pytest.raises(ServiceControlError) as exc_info:
        controller.start(
            readiness_timeout=0.5,
            poll_interval=0.25,
            clock=lambda: now[0],
            sleep=sleep,
        )

    assert exc_info.value.code == "startup_failed"
    assert signaler.calls == [("pid", 200, signal.SIGTERM)]
    assert not paths.supervisor_pid_path.exists()


def test_startup_timeout_hard_kills_only_an_unchanged_verified_supervisor(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    inspector = FakeInspector()
    now = [0.0]

    def launch(_command: list[str], **_kwargs: object) -> object:
        server = _server_snapshot(paths, birth_marker="proc-start-ticks:20100")
        supervisor = _supervisor_snapshot(paths, server)
        supervisor = replace(supervisor, birth_marker="proc-start-ticks:20000")
        write_private_text(paths.supervisor_pid_path, f"{supervisor.pid}\n")
        inspector.processes = {supervisor.pid: supervisor}
        return SimpleNamespace(pid=supervisor.pid, poll=lambda: None)

    def on_signal(_kind: str, _identifier: int, signal_number: int) -> None:
        if signal_number == signal.SIGKILL:
            inspector.processes.clear()

    signaler = FakeSignaler(on_signal)
    with pytest.raises(ServiceControlError) as exc_info:
        ServiceController(
            paths,
            inspector=inspector,
            client=FakeClient(ServerProbe(False, False, False, "offline")),
            popen=launch,
            signaler=signaler,
        ).start(
            readiness_timeout=0.5,
            poll_interval=0.25,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )

    assert exc_info.value.code == "startup_failed"
    assert signaler.calls == [
        ("pid", 200, signal.SIGTERM),
        ("pid", 200, signal.SIGKILL),
    ]
    assert not paths.supervisor_pid_path.exists()


def test_stop_is_idempotent_when_already_stopped(tmp_path: Path) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    signaler = FakeSignaler()
    status = ServiceController(
        paths,
        inspector=FakeInspector(),
        client=FakeClient(ServerProbe(False, False, False, "offline")),
        signaler=signaler,
    ).stop()

    assert status.state == ServiceState.STOPPED
    assert signaler.calls == []


def test_stop_terminates_only_a_verified_managed_process_group(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    supervisor = _supervisor_snapshot(paths, server)
    _managed_metadata(paths)
    inspector = FakeInspector(
        processes={server.pid: server, supervisor.pid: supervisor},
        listeners=(_listener(paths, server.pid),),
        live_groups=frozenset({server.pgid}),
        bindable=False,
    )

    def on_signal(kind: str, identifier: int, signal_number: int) -> None:
        assert (kind, identifier, signal_number) == (
            "group",
            server.pgid,
            signal.SIGTERM,
        )
        inspector.processes.clear()
        inspector.bound_listeners = ()
        inspector.live_groups = frozenset()
        inspector.bindable = True

    signaler = FakeSignaler(on_signal)
    status = ServiceController(
        paths,
        inspector=inspector,
        client=FakeClient(ServerProbe(True, True, False)),
        signaler=signaler,
    ).stop()

    assert status.state == ServiceState.STOPPED
    assert signaler.calls == [("group", server.pgid, signal.SIGTERM)]
    assert not paths.pid_path.exists()
    assert not paths.supervisor_pid_path.exists()
    assert not paths.pgid_path.exists()


def test_stop_terminates_only_the_exact_verified_external_pid(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    inspector = FakeInspector(
        processes={server.pid: server},
        listeners=(_listener(paths, server.pid),),
        live_groups=frozenset({server.pgid}),
        bindable=False,
    )

    def on_signal(kind: str, identifier: int, signal_number: int) -> None:
        assert (kind, identifier, signal_number) == (
            "pid",
            server.pid,
            signal.SIGTERM,
        )
        inspector.processes.clear()
        inspector.bound_listeners = ()
        inspector.live_groups = frozenset()
        inspector.bindable = True

    signaler = FakeSignaler(on_signal)
    status = ServiceController(
        paths,
        inspector=inspector,
        client=FakeClient(ServerProbe(True, True, False)),
        signaler=signaler,
    ).stop()

    assert status.state == ServiceState.STOPPED
    assert signaler.calls == [("pid", server.pid, signal.SIGTERM)]


def test_stop_reverifies_identity_before_hard_kill(tmp_path: Path) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    inspector = FakeInspector(
        processes={server.pid: server},
        listeners=(_listener(paths, server.pid),),
        live_groups=frozenset({server.pgid}),
        bindable=False,
    )
    now = [0.0]

    def on_signal(kind: str, identifier: int, signal_number: int) -> None:
        if signal_number == signal.SIGTERM:
            inspector.processes[server.pid] = replace(
                server,
                command=("python", "-m", "http.server", "18765"),
            )

    signaler = FakeSignaler(on_signal)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(ServiceControlError) as exc_info:
        ServiceController(
            paths,
            inspector=inspector,
            client=FakeClient(ServerProbe(True, True, False)),
            signaler=signaler,
        ).stop(
            grace_timeout=0.5,
            kill_timeout=0.5,
            poll_interval=0.25,
            clock=lambda: now[0],
            sleep=sleep,
        )

    assert exc_info.value.code == "identity_changed"
    assert signaler.calls == [("pid", server.pid, signal.SIGTERM)]


def test_stop_refuses_hard_kill_when_same_shaped_pid_was_reused(
    tmp_path: Path,
) -> None:
    """A PID's command and group can be reused, but its birth marker cannot."""
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths, birth_marker="proc-start-ticks:1001")
    inspector = FakeInspector(
        processes={server.pid: server},
        listeners=(_listener(paths, server.pid),),
        live_groups=frozenset({server.pgid}),
        bindable=False,
    )
    now = [0.0]

    def on_signal(kind: str, identifier: int, signal_number: int) -> None:
        if signal_number == signal.SIGTERM:
            assert (kind, identifier) == ("pid", server.pid)
            inspector.processes[server.pid] = replace(
                server,
                birth_marker="proc-start-ticks:1002",
            )

    signaler = FakeSignaler(on_signal)

    with pytest.raises(ServiceControlError) as exc_info:
        ServiceController(
            paths,
            inspector=inspector,
            client=FakeClient(ServerProbe(True, True, False)),
            signaler=signaler,
        ).stop(
            grace_timeout=0.5,
            kill_timeout=0.5,
            poll_interval=0.25,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )

    assert exc_info.value.code == "identity_changed"
    assert signaler.calls == [("pid", server.pid, signal.SIGTERM)]


def test_stop_refuses_hard_kill_without_a_strong_birth_marker(
    tmp_path: Path,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths, birth_marker="")
    inspector = FakeInspector(
        processes={server.pid: server},
        listeners=(_listener(paths, server.pid),),
        live_groups=frozenset({server.pgid}),
        bindable=False,
    )
    now = [0.0]
    signaler = FakeSignaler()

    with pytest.raises(ServiceControlError) as exc_info:
        ServiceController(
            paths,
            inspector=inspector,
            client=FakeClient(ServerProbe(True, True, False)),
            signaler=signaler,
        ).stop(
            grace_timeout=0.5,
            kill_timeout=0.5,
            poll_interval=0.25,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )

    assert exc_info.value.code == "identity_changed"
    assert signaler.calls == [("pid", server.pid, signal.SIGTERM)]


@pytest.mark.parametrize("managed", [False, True])
def test_stop_escalates_only_a_still_verified_signal_target(
    tmp_path: Path,
    managed: bool,
) -> None:
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=18765)
    server = _server_snapshot(paths)
    processes = {server.pid: server}
    if managed:
        supervisor = _supervisor_snapshot(paths, server)
        processes[supervisor.pid] = supervisor
        _managed_metadata(paths)
    inspector = FakeInspector(
        processes=processes,
        listeners=(_listener(paths, server.pid),),
        live_groups=frozenset({server.pgid}),
        bindable=False,
    )
    now = [0.0]

    def on_signal(kind: str, identifier: int, signal_number: int) -> None:
        if signal_number != signal.SIGKILL:
            return
        inspector.processes.clear()
        inspector.bound_listeners = ()
        inspector.live_groups = frozenset()
        inspector.bindable = True

    signaler = FakeSignaler(on_signal)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    status = ServiceController(
        paths,
        inspector=inspector,
        client=FakeClient(ServerProbe(True, True, False)),
        signaler=signaler,
    ).stop(
        grace_timeout=0.5,
        kill_timeout=0.5,
        poll_interval=0.25,
        clock=lambda: now[0],
        sleep=sleep,
    )

    target_kind = "group" if managed else "pid"
    assert status.state == ServiceState.STOPPED
    assert signaler.calls == [
        (target_kind, server.pgid if managed else server.pid, signal.SIGTERM),
        (target_kind, server.pgid if managed else server.pid, signal.SIGKILL),
    ]


def test_system_process_inspector_and_signaler_touch_only_test_owned_process(
    tmp_path: Path,
) -> None:
    process = subprocess.Popen(
        ["/bin/sleep", "30"],
        cwd=tmp_path,
        start_new_session=True,
    )
    try:
        snapshot = SystemProcessInspector().process(process.pid)
        assert snapshot is not None
        assert snapshot.pid == process.pid
        assert snapshot.uid == os.getuid()
        assert snapshot.cwd == tmp_path.resolve()
        assert snapshot.birth_marker

        SystemProcessSignaler().signal_pid(process.pid, signal.SIGTERM)
        assert process.wait(timeout=3) == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


def test_system_process_inspector_treats_a_process_that_vanishes_during_cwd_lookup_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = SystemProcessInspector()
    results = iter(
        (
            SimpleNamespace(
                returncode=0,
                stdout=f"{os.getuid()} 200 S Mon Jul 28 10:10:10 2026 /bin/sleep 30\n",
            ),
            SimpleNamespace(returncode=1, stdout=""),
        )
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: next(results))
    monkeypatch.setattr(
        inspector,
        "_process_cwd",
        lambda _pid: (_ for _ in ()).throw(
            ServiceControlError("vanished", code="process_inspection_failed", recovery="retry")
        ),
    )

    assert inspector.process(200) is None


def test_darwin_birth_marker_uses_microseconds_to_reject_same_second_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = SystemProcessInspector()
    markers = iter(((1_753_718_200, 101), (1_753_718_200, 102)))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        service_control,
        "_darwin_process_birth_marker",
        lambda _pid: next(markers),
        raising=False,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{os.getuid()} 200 S Mon Jul 28 10:10:10 2026 /bin/sleep 30\n",
        ),
    )
    monkeypatch.setattr(inspector, "_process_cwd", lambda _pid: Path("/tmp"))

    expected = inspector.process(200)
    current = inspector.process(200)

    assert expected is not None
    assert current is not None
    assert expected.birth_marker == "darwin-proc-start:1753718200:000101"
    assert current.birth_marker == "darwin-proc-start:1753718200:000102"
    assert not service_control._same_process_identity(expected, current)


class _ShortProcPidInfo:
    argtypes: object | None = None
    restype: object | None = None

    def __call__(self, *_args: object) -> int:
        return 1


@pytest.mark.parametrize("failure", ["unavailable", "short_result"])
def test_darwin_birth_marker_fails_closed_when_libproc_cannot_prove_identity(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    class FakeLibproc:
        proc_pidinfo = _ShortProcPidInfo()

    if failure == "unavailable":
        def load_libproc(*_args: object, **_kwargs: object) -> object:
            raise OSError("libproc unavailable")
    else:
        def load_libproc(*_args: object, **_kwargs: object) -> object:
            return FakeLibproc()

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", load_libproc)

    with pytest.raises(ServiceControlError) as exc_info:
        SystemProcessInspector()._birth_marker(200, "Mon Jul 28 10:10:10 2026")

    assert exc_info.value.code == "process_inspection_failed"


def test_linux_birth_marker_uses_proc_start_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        service_control.Path,
        "read_text",
        lambda _self, **_kwargs: (
            "200 (sleep) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 987654\n"
        ),
    )

    assert (
        SystemProcessInspector()._birth_marker(200, "unused")
        == "proc-start-ticks:987654"
    )


def test_unsupported_platform_birth_marker_fails_closed_without_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd13")

    with pytest.raises(ServiceControlError) as exc_info:
        SystemProcessInspector()._birth_marker(200, "Mon Jul 28 10:10:10 2026")

    assert exc_info.value.code == "process_inspection_failed"


def test_service_controller_real_process_lifecycle_smoke(tmp_path: Path) -> None:
    if os.name == "nt" or shutil.which("lsof") is None:
        pytest.skip("requires POSIX process inspection with lsof")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    paths = resolve_service_paths(_installation(tmp_path / "home"), port=port)
    paths.server_executable.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, socket, sys\n"
        "host = sys.argv[sys.argv.index('--host') + 1]\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "listener = socket.socket()\n"
        "listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "listener.bind((host, port))\n"
        "listener.listen()\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n",
        encoding="utf-8",
    )
    paths.server_executable.chmod(0o755)
    paths.supervisor_script.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "umask 077\n"
        "pid_file=$2; supervisor_file=$4; group_file=$6; log_file=$8\n"
        "shift 8; shift\n"
        "\"$@\" >>\"$log_file\" 2>&1 &\n"
        "child=$!\n"
        "printf '%s\\n' \"$child\" >\"$pid_file\"\n"
        "printf '%s\\n' \"$$\" >\"$supervisor_file\"\n"
        "printf '%s\\n' \"$$\" >\"$group_file\"\n"
        "wait \"$child\"\n",
        encoding="utf-8",
    )
    paths.supervisor_script.chmod(0o755)

    controller = ServiceController(
        paths,
        client=FakeClient(ServerProbe(True, True, False)),
    )
    try:
        started = controller.start(readiness_timeout=3, poll_interval=0.05)
        assert started.state == ServiceState.RUNNING
        assert started.management == ServiceManagement.MANAGED
        assert paths.pid_path.exists()
        assert SystemProcessInspector().process(started.pid or -1) is not None

        stopped = controller.stop(grace_timeout=3, kill_timeout=1, poll_interval=0.05)
        assert stopped.state == ServiceState.STOPPED
        assert not paths.pid_path.exists()
        assert not paths.supervisor_pid_path.exists()
        assert not paths.pgid_path.exists()
        assert SystemProcessInspector().listeners(paths.host, paths.port) == ()
    finally:
        try:
            controller.stop(grace_timeout=1, kill_timeout=1, poll_interval=0.05)
        except ServiceControlError:
            pass
        if paths.pgid_path.exists():
            pgid_text = paths.pgid_path.read_text(encoding="ascii").strip()
            if pgid_text.isdigit():
                group_leader = SystemProcessInspector().process(int(pgid_text))
                if (
                    group_leader is not None
                    and group_leader.cwd == paths.home
                    and str(paths.supervisor_script) in group_leader.command
                ):
                    os.killpg(int(pgid_text), signal.SIGKILL)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and SystemProcessInspector().process(
                        group_leader.pid
                    ) is not None:
                        time.sleep(0.05)


@pytest.mark.parametrize(
    ("lsof_output", "expected"),
    [
        ("p101\nn127.0.0.1:18765\n", (BoundListener(101, "127.0.0.1", 18765),)),
        ("p102\nn*:18765\n", (BoundListener(102, "*", 18765),)),
        ("p103\nn[::1]:18765\n", (BoundListener(103, "::1", 18765),)),
    ],
)
def test_system_listener_inspection_parses_complete_lsof_records(
    monkeypatch: pytest.MonkeyPatch,
    lsof_output: str,
    expected: tuple[BoundListener, ...],
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=lsof_output),
    )

    assert SystemProcessInspector().listeners("127.0.0.1", 18765) == expected


@pytest.mark.parametrize(
    "lsof_output",
    [
        "p104\nnnot-an-endpoint\n",
        "p105\n",
        "p106\nn127.0.0.1:18765\np107\n",
    ],
)
def test_system_listener_inspection_refuses_incomplete_lsof_records(
    monkeypatch: pytest.MonkeyPatch,
    lsof_output: str,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=lsof_output),
    )

    with pytest.raises(ServiceControlError) as exc_info:
        SystemProcessInspector().listeners("127.0.0.1", 18765)

    assert exc_info.value.code == "port_inspection_failed"
