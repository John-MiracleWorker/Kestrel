from __future__ import annotations

import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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

        SystemProcessSignaler().signal_pid(process.pid, signal.SIGTERM)
        assert process.wait(timeout=3) == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
