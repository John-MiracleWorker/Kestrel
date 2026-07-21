from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import pytest
from fastapi.testclient import TestClient

import nested_memvid_agent.server as server_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_ownership import (
    RUNTIME_OWNERSHIP_ERROR,
    PrimaryRuntimeOwnership,
    RuntimeOwnershipError,
    runtime_ownership_lock_path,
)
from nested_memvid_agent.server import create_app
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


class _PluginProbe:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sync_calls = 0

    def sync_all(self) -> None:
        self.sync_calls += 1
        if self.fail:
            raise RuntimeError("injected plugin reconciliation failure")


def _build_manager(
    root: Path,
    *,
    plugins: Any | None = None,
) -> RunManager:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=root / "state.db",
        memory_dir=root / "memory",
        log_dir=root / "logs",
        skills_dir=root / "skills",
        plugins_dir=root / "plugins",
        workspace=root,
    )
    state = AgentStateStore(config.state_path)
    return RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        plugins=plugins,
        recover_startup_work=False,
        enforce_single_owner=True,
    )


def _seed_observer_catalog(root: Path, state: AgentStateStore) -> None:
    disk_skill = root / "skills" / "disk-observer"
    disk_skill.mkdir(parents=True, exist_ok=True)
    (disk_skill / "skill.json").write_text(
        json.dumps(
            {
                "id": "disk-observer",
                "name": "Disk Observer",
                "description": "Must not be discovered by a read-only CLI observer.",
                "risk": "low",
            }
        ),
        encoding="utf-8",
    )
    (disk_skill / "SKILL.md").write_text("Observe without mutation.", encoding="utf-8")

    plugin_path = root / "plugins" / "readonly"
    plugin_path.mkdir(parents=True, exist_ok=True)
    state.upsert_plugin(
        {
            "id": "readonly",
            "name": "Readonly",
            "description": "Observer reconciliation fixture.",
            "source_url": "https://github.com/owner/readonly",
            "commit_sha": "f" * 40,
            "install_path": str(plugin_path),
            "manifest": {"id": "readonly", "skills": [], "mcp_servers": []},
            "capabilities": ["skill"],
            "enabled": True,
            "risk_report": {"risk": "medium"},
            "install_status": "installed",
            "format": "kestrel",
        }
    )
    stale_skill_id = "plugin.readonly.stale"
    state.upsert_skill(
        {
            "id": stale_skill_id,
            "name": "Stale Plugin Skill",
            "description": "A primary reconciliation would remove this row.",
            "path": str(plugin_path / "generated" / "skills" / "stale"),
            "manifest": {"id": stale_skill_id},
            "enabled": True,
        }
    )
    state.set_capability_override(
        "skill",
        stale_skill_id,
        True,
        expected_revision=0,
        default_enabled=False,
        updated_by="observer-test",
    )
    state.create_run(
        run_id="run_observer_fixture",
        message="observer fixture",
        session_id="observer-session",
        workspace=str(root),
        model="mock",
    )
    state.create_approval(
        approval_id="approval_observer_fixture",
        run_id="run_observer_fixture",
        tool_call_id="tool_observer_fixture",
        tool_name="shell.run",
        arguments={"command": ["true"]},
        risk="high",
        expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )


def _extension_state_snapshot(state: AgentStateStore) -> dict[str, list[dict[str, Any]]]:
    return {
        "plugins": state.list_plugins(),
        "skills": state.list_skills(),
        "capabilities": state.list_capability_overrides(),
        "mcp_servers": state.list_mcp_servers(),
    }


def test_run_manager_ownership_precedes_reconciliation_and_transfers_after_shutdown(
    tmp_path: Path,
) -> None:
    first = _build_manager(tmp_path)
    blocked_probe = _PluginProbe()

    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        _build_manager(tmp_path, plugins=blocked_probe)

    assert blocked_probe.sync_calls == 0
    assert first.shutdown(timeout_seconds=1.0) is True
    assert first.shutdown(timeout_seconds=1.0) is True

    successor_probe = _PluginProbe()
    successor = _build_manager(tmp_path, plugins=successor_probe)
    assert successor_probe.sync_calls == 1
    assert successor.shutdown(timeout_seconds=1.0) is True


def test_run_manager_releases_ownership_when_initialization_fails(tmp_path: Path) -> None:
    failing_probe = _PluginProbe(fail=True)
    with pytest.raises(RuntimeError, match="injected plugin reconciliation failure"):
        _build_manager(tmp_path, plugins=failing_probe)
    assert failing_probe.sync_calls == 1

    successor = _build_manager(tmp_path, plugins=_PluginProbe())
    assert successor.shutdown(timeout_seconds=1.0) is True


def test_read_only_observer_skips_reconciliation_and_approval_expiry(
    tmp_path: Path,
) -> None:
    owner = _build_manager(tmp_path)
    state = owner.state
    _seed_observer_catalog(tmp_path, state)
    before = _extension_state_snapshot(state)
    probe = _PluginProbe()
    observer_mcp = MCPManager(state)
    observer = RunManager(
        config=owner.config,
        state=state,
        events=RunEventBus(state),
        mcp=observer_mcp,
        skills=SkillManager(owner.config.skills_dir, state),
        plugins=probe,
        recover_startup_work=False,
        read_only_observer=True,
    )

    try:
        assert probe.sync_calls == 0
        assert observer.get_run("run_observer_fixture")["run_id"] == "run_observer_fixture"
        approvals = observer.list_approvals(status="pending")
        assert [item["approval_id"] for item in approvals] == [
            "approval_observer_fixture"
        ]
        assert state.get_approval("approval_observer_fixture", expire=False)["status"] == "pending"
        assert _extension_state_snapshot(state) == before
        with pytest.raises(
            RuntimeError,
            match="^read_only_runtime_observer:reconcile_capabilities$",
        ):
            observer.reconcile_capabilities()
        with pytest.raises(RuntimeError, match="^read_only_runtime_observer:create_run$"):
            observer.create_run(message="must fail")
    finally:
        assert observer.shutdown(timeout_seconds=1.0) is True
        observer_mcp.shutdown()
        assert owner.shutdown(timeout_seconds=1.0) is True


@pytest.mark.parametrize(
    ("recover_startup_work", "enforce_single_owner", "message"),
    [
        (True, False, "read-only observers cannot recover startup work"),
        (False, True, "read-only observers cannot own the primary runtime"),
    ],
)
def test_read_only_observer_rejects_active_runtime_modes(
    tmp_path: Path,
    recover_startup_work: bool,
    enforce_single_owner: bool,
    message: str,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)

    with pytest.raises(ValueError, match=f"^{message}$"):
        RunManager(
            config=config,
            state=state,
            events=RunEventBus(state),
            mcp=MCPManager(state),
            skills=SkillManager(config.skills_dir, state),
            recover_startup_work=recover_startup_work,
            enforce_single_owner=enforce_single_owner,
            read_only_observer=True,
        )


def test_server_enforces_one_runtime_owner_and_releases_on_clean_lifespan_shutdown(
    tmp_path: Path,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "server" / "state.db",
        memory_dir=tmp_path / "server" / "memory",
        log_dir=tmp_path / "server" / "logs",
        skills_dir=tmp_path / "server" / "skills",
        plugins_dir=tmp_path / "server" / "plugins",
        workspace=tmp_path,
    )

    contender_app = create_app(config)
    with TestClient(create_app(config)) as client:
        assert client.get("/api/health/live").status_code == 200
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            with TestClient(contender_app):
                pass

    with TestClient(create_app(config)) as restarted:
        assert restarted.get("/api/health/live").status_code == 200


def test_unverified_mcp_shutdown_retains_primary_runtime_ownership(
    tmp_path: Path,
) -> None:
    owner = _build_manager(tmp_path)
    allow_close = False

    class _Worker:
        def close(self, *, timeout: float) -> bool:
            del timeout
            return allow_close

    worker = _Worker()
    owner.mcp._sessions["stuck-stdio"] = worker  # type: ignore[assignment]

    assert owner.shutdown(timeout_seconds=1.0) is False
    assert owner.mcp._sessions["stuck-stdio"] is worker
    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        _build_manager(tmp_path)

    allow_close = True
    assert owner.shutdown(timeout_seconds=1.0) is True
    successor = _build_manager(tmp_path)
    assert successor.shutdown(timeout_seconds=1.0) is True


def test_unverified_oci_cleanup_retains_primary_runtime_ownership(
    tmp_path: Path,
) -> None:
    owner = _build_manager(tmp_path)

    class _CleanupRunner:
        def __init__(self) -> None:
            self.allow_cleanup = False
            self.shutdown_calls = 0

        @property
        def pending_cleanup_count(self) -> int:
            return 0 if self.allow_cleanup else 1

        def shutdown(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            self.shutdown_calls += 1
            return self.allow_cleanup

    runner = _CleanupRunner()
    owner.skills.container_runner = runner  # type: ignore[assignment]

    assert owner.operational_counters()["oci_container_cleanups_pending"] == 1
    assert owner.shutdown(timeout_seconds=1.0) is False
    assert runner.shutdown_calls == 1
    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        _build_manager(tmp_path)

    runner.allow_cleanup = True
    assert owner.shutdown(timeout_seconds=1.0) is True
    assert runner.shutdown_calls == 2
    assert owner.operational_counters()["oci_container_cleanups_pending"] == 0
    successor = _build_manager(tmp_path)
    assert successor.shutdown(timeout_seconds=1.0) is True


def test_server_app_construction_is_inert_until_lifespan_start(tmp_path: Path) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "inert-server" / "state.db",
        memory_dir=tmp_path / "inert-server" / "memory",
        log_dir=tmp_path / "inert-server" / "logs",
        skills_dir=tmp_path / "inert-server" / "skills",
        plugins_dir=tmp_path / "inert-server" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    state.create_run(
        run_id="queued_before_lifespan",
        message="say hello",
        session_id="inert-server",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )

    app = create_app(config)
    sleep(0.1)

    assert state.get_run("queued_before_lifespan").status == "queued"
    ownership_probe = PrimaryRuntimeOwnership(config.state_path)
    ownership_probe.acquire()
    ownership_probe.release()

    with TestClient(app) as client:
        assert client.get("/api/health/live").status_code == 200
        deadline = monotonic() + 10
        while state.get_run("queued_before_lifespan").status not in {
            "completed",
            "failed",
            "cancelled",
        }:
            assert monotonic() < deadline
            sleep(0.01)
        assert state.get_run("queued_before_lifespan").status == "completed"

    assert state.get_run("queued_before_lifespan").status == "completed"


def test_server_factory_releases_runtime_owner_when_post_manager_assembly_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "factory-failure" / "state.db",
        memory_dir=tmp_path / "factory-failure" / "memory",
        log_dir=tmp_path / "factory-failure" / "logs",
        skills_dir=tmp_path / "factory-failure" / "skills",
        plugins_dir=tmp_path / "factory-failure" / "plugins",
        workspace=tmp_path,
    )
    real_channel_manager = server_module.ChannelManager

    def fail_channel_manager(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("injected channel assembly failure")

    monkeypatch.setattr(server_module, "ChannelManager", fail_channel_manager)
    with pytest.raises(RuntimeError, match="injected channel assembly failure"):
        create_app(config)

    monkeypatch.setattr(server_module, "ChannelManager", real_channel_manager)
    with TestClient(create_app(config)) as recovered:
        assert recovered.get("/api/health/live").status_code == 200


def test_timed_out_shutdown_retains_ownership_until_workers_exit(tmp_path: Path) -> None:
    manager = _build_manager(tmp_path)
    release_worker = Event()
    worker = Thread(target=release_worker.wait, daemon=True)
    worker.start()
    manager._threads["ownership-test-worker"] = worker

    try:
        assert manager.shutdown(timeout_seconds=0.0) is False
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            _build_manager(tmp_path)
    finally:
        release_worker.set()
        worker.join(timeout=2)

    assert manager.shutdown(timeout_seconds=1.0) is True
    successor = _build_manager(tmp_path)
    assert successor.shutdown(timeout_seconds=1.0) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX link semantics differ on Windows")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_runtime_ownership_rejects_aliased_lock(
    tmp_path: Path,
    link_kind: str,
) -> None:
    state_path = tmp_path / "state" / "agent.db"
    AgentStateStore(state_path)
    lock_path = runtime_ownership_lock_path(state_path)
    outside = tmp_path / "outside.lock"
    outside.write_text("do-not-touch", encoding="utf-8")
    os.chmod(outside, 0o644)
    if link_kind == "symlink":
        lock_path.symlink_to(outside)
    else:
        lock_path.hardlink_to(outside)

    with pytest.raises(ValueError, match="Sensitive artifacts must not be"):
        PrimaryRuntimeOwnership(state_path).acquire()

    assert outside.read_text(encoding="utf-8") == "do-not-touch"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_runtime_ownership_lock_is_owner_only(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "agent.db"
    AgentStateStore(state_path)
    ownership = PrimaryRuntimeOwnership(state_path)

    ownership.acquire()
    try:
        assert ownership.acquired is True
        assert stat.S_IMODE(ownership.lock_path.stat().st_mode) == 0o600
    finally:
        ownership.release()
    assert ownership.acquired is False


_SUBPROCESS_OWNER_SCRIPT = """
import sys
from pathlib import Path
from time import sleep

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_ownership import RuntimeOwnershipError
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore

root = Path(sys.argv[1])
mode = sys.argv[2]
ready_path = Path(sys.argv[3])
release_path = Path(sys.argv[4])
config = AgentConfig(
    backend="memory",
    provider="mock",
    model="mock",
    state_path=root / "state.db",
    memory_dir=root / "memory",
    log_dir=root / "logs",
    skills_dir=root / "skills",
    plugins_dir=root / "plugins",
    workspace=root,
)
state = AgentStateStore(config.state_path)
try:
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
    )
except RuntimeOwnershipError as exc:
    print(str(exc), flush=True)
    raise SystemExit(23) from exc

print("acquired", flush=True)
if mode == "hold":
    ready_path.write_text("ready", encoding="utf-8")
    while not release_path.exists():
        sleep(0.01)
if not manager.shutdown(timeout_seconds=2.0):
    raise SystemExit(24)
"""


def test_subprocess_runtime_ownership_is_exclusive_and_reusable(tmp_path: Path) -> None:
    root = tmp_path / "subprocess-runtime"
    root.mkdir()
    ready_path = tmp_path / "holder.ready"
    release_path = tmp_path / "holder.release"
    command = [
        sys.executable,
        "-c",
        _SUBPROCESS_OWNER_SCRIPT,
        str(root),
        "hold",
        str(ready_path),
        str(release_path),
    ]
    environment = dict(os.environ)
    source_root = Path(__file__).parents[1] / "src"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), environment.get("PYTHONPATH", "")]
    )
    holder = subprocess.Popen(  # noqa: S603
        command,
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        deadline = monotonic() + 20.0
        while not ready_path.exists() and holder.poll() is None and monotonic() < deadline:
            sleep(0.01)
        assert ready_path.exists(), holder.communicate(timeout=2)

        contender_command = [
            sys.executable,
            "-c",
            _SUBPROCESS_OWNER_SCRIPT,
            str(root),
            "once",
            str(ready_path),
            str(release_path),
        ]
        blocked = subprocess.run(  # noqa: S603
            contender_command,
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert blocked.returncode == 23
        assert blocked.stdout.strip() == RUNTIME_OWNERSHIP_ERROR

        release_path.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=30)
        assert holder.returncode == 0, holder_stderr
        assert holder_stdout.strip() == "acquired"

        successor = subprocess.run(  # noqa: S603
            contender_command,
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert successor.returncode == 0, successor.stderr
        assert successor.stdout.strip() == "acquired"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=5)


def test_real_cli_fails_primary_admission_but_keeps_status_available_to_observers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cli-runtime"
    owner = _build_manager(root)
    environment = dict(os.environ)
    source_root = Path(__file__).parents[1] / "src"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), environment.get("PYTHONPATH", "")]
    )
    common = [
        "--backend",
        "memory",
        "--memory-dir",
        str(root / "memory"),
        "--state-path",
        str(root / "state.db"),
        "--log-dir",
        str(root / "logs"),
        "--skills-dir",
        str(root / "skills"),
        "--plugins-dir",
        str(root / "plugins"),
        "--workspace",
        str(root),
        "--provider",
        "mock",
        "--model",
        "mock",
    ]

    try:
        blocked = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "nested_memvid_agent.cli", "run", *common, "--json", "blocked contender"],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert blocked.returncode == 1
        assert "Another Kestrel runtime already owns this state database" in blocked.stderr
        assert AgentStateStore(root / "state.db").list_runs() == []

        observed = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "nested_memvid_agent.cli", "status", *common, "--json"],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert observed.returncode == 0, observed.stderr
        assert json.loads(observed.stdout) == {"runs": [], "sessions": []}
    finally:
        assert owner.shutdown(timeout_seconds=2.0) is True

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "nested_memvid_agent.cli", "run", *common, "--json", "successor run"],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "completed"


def test_real_cli_observers_preserve_extension_control_plane_under_live_owner(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cli-observers"
    owner = _build_manager(root)
    state = owner.state
    _seed_observer_catalog(root, state)
    before = _extension_state_snapshot(state)
    environment = dict(os.environ)
    source_root = Path(__file__).parents[1] / "src"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), environment.get("PYTHONPATH", "")]
    )
    common = [
        "--backend",
        "memory",
        "--memory-dir",
        str(root / "memory"),
        "--state-path",
        str(root / "state.db"),
        "--log-dir",
        str(root / "logs"),
        "--skills-dir",
        str(root / "skills"),
        "--plugins-dir",
        str(root / "plugins"),
        "--workspace",
        str(root),
        "--provider",
        "mock",
        "--model",
        "mock",
    ]

    def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "nested_memvid_agent.cli", *arguments],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    try:
        status = run_cli(
            "status",
            *common,
            "--json",
            "run_observer_fixture",
        )
        approvals = run_cli("approvals", *common, "--status", "pending", "--json")
        plugins = run_cli("plugins", "list", *common, "--json")
        inspected = run_cli("plugins", "inspect", *common, "readonly", "--json")

        assert status.returncode == 0, status.stderr
        assert json.loads(status.stdout)["run_id"] == "run_observer_fixture"
        assert approvals.returncode == 0, approvals.stderr
        assert json.loads(approvals.stdout)["approvals"][0]["status"] == "pending"
        assert plugins.returncode == 0, plugins.stderr
        assert json.loads(plugins.stdout)["plugins"][0]["id"] == "readonly"
        assert inspected.returncode == 0, inspected.stderr
        assert json.loads(inspected.stdout)["id"] == "readonly"
        assert state.get_approval("approval_observer_fixture", expire=False)["status"] == "pending"
        assert _extension_state_snapshot(state) == before
        assert {item["id"] for item in state.list_skills()} == {
            "plugin.readonly.stale"
        }

        mutating = run_cli("plugins", "disable", *common, "readonly", "--json")
        assert mutating.returncode == 1
        assert "Another Kestrel runtime already owns this state database" in mutating.stderr
        assert _extension_state_snapshot(state) == before
    finally:
        assert owner.shutdown(timeout_seconds=2.0) is True
