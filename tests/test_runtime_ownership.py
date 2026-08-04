from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Any

import pytest
from fastapi.testclient import TestClient

import nested_memvid_agent.run_manager as run_manager_module
import nested_memvid_agent.runtime_ownership as runtime_ownership_module
import nested_memvid_agent.server as server_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.lan_discovery_models import LanScanLimits, NetworkInterface
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.routing.lan_ledger import LanDiscoveryLedger
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_ownership import (
    RUNTIME_OWNERSHIP_ERROR,
    PrimaryRuntimeOwnership,
    RuntimeOwnershipError,
    runtime_ownership_lock_path,
)
from nested_memvid_agent.runtime_profile_lease import (
    RuntimeProfileLease,
    current_runtime_lease_identity,
    resolve_runtime_profile_root,
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


def test_windows_lock_violation_is_recognized_as_runtime_contention() -> None:
    class _WindowsLockViolation(OSError):
        winerror = 33

    error = _WindowsLockViolation(33, "injected Windows lock violation")

    assert runtime_ownership_module._is_lock_contention(error) is True


def test_cli_server_exits_before_opening_writers_when_desktop_owns_profile(
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    state_path = profile_root / "state" / "agent.db"
    memory_dir = profile_root / "memory"
    lease_root = resolve_runtime_profile_root(
        state_path,
        memory_dir,
        profile_id="default",
    )
    desktop = RuntimeProfileLease.acquire(
        lease_root,
        current_runtime_lease_identity(
            profile_id="default",
            management="desktop",
            base_url="http://127.0.0.1:8765/",
            launch_nonce="desktop-test-nonce",
        ),
    )
    environment = dict(os.environ)
    source_root = Path(__file__).parents[1] / "src"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), environment.get("PYTHONPATH", "")]
    )
    try:
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "nested_memvid_agent.cli",
                "server",
                "--backend",
                "memory",
                "--provider",
                "mock",
                "--model",
                "mock",
                "--state-path",
                str(state_path),
                "--memory-dir",
                str(memory_dir),
                "--workspace",
                str(tmp_path),
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
            ],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        desktop.release()

    assert result.returncode == 1
    assert result.stderr.strip() == "profile_owned_by_desktop"
    assert not state_path.exists()
    assert not memory_dir.exists()


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
        assert [item["approval_id"] for item in approvals] == ["approval_observer_fixture"]
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


def test_task6_app_construction_validates_initialized_schema_without_rewriting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task6 = import_module("nested_memvid_agent.lan_scan_manager")
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "inert-task6-schema" / "state.db",
        memory_dir=tmp_path / "inert-task6-schema" / "memory",
        log_dir=tmp_path / "inert-task6-schema" / "logs",
        skills_dir=tmp_path / "inert-task6-schema" / "skills",
        plugins_dir=tmp_path / "inert-task6-schema" / "plugins",
        workspace=tmp_path,
    )
    schema_snapshots: list[tuple[tuple[int, str], tuple[int, str]]] = []
    lifecycle_calls: list[str] = []
    real_factory = LanDiscoveryLedger.from_initialized_state
    real_start_lifecycle = task6.LanScanManager.start_lifecycle

    def schema_row(state: AgentStateStore) -> tuple[int, str]:
        with state._connect() as connection:
            row = connection.execute(
                "SELECT version, updated_at FROM routing_schema_version WHERE id = 1"
            ).fetchone()
        assert row is not None
        return int(row["version"]), str(row["updated_at"])

    def capture_validation_only(
        state: AgentStateStore,
        **kwargs: Any,
    ) -> LanDiscoveryLedger:
        before = schema_row(state)
        ledger = real_factory(state, **kwargs)
        schema_snapshots.append((before, schema_row(state)))
        return ledger

    def track_lifecycle_start(manager: Any, executor: Any) -> Any:
        lifecycle_calls.append("start")
        return real_start_lifecycle(manager, executor)

    def fail_executor_construction(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Task 6 executor construction belongs to lifespan")

    def fail_network_inventory() -> Any:
        raise AssertionError("Task 6 network inventory belongs to an explicit preview")

    monkeypatch.setattr(
        LanDiscoveryLedger,
        "from_initialized_state",
        staticmethod(capture_validation_only),
    )
    monkeypatch.setattr(task6.LanScanManager, "start_lifecycle", track_lifecycle_start)
    monkeypatch.setattr(server_module, "ThreadPoolExecutor", fail_executor_construction)
    monkeypatch.setattr(task6, "enumerate_private_interfaces", fail_network_inventory)

    create_app(config)

    assert schema_snapshots and schema_snapshots[0][0] == schema_snapshots[0][1]
    assert lifecycle_calls == []


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


def test_lan_lifecycle_dependency_blocks_owner_release_until_idempotent_retry(
    tmp_path: Path,
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
    owner = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
        auto_start=False,
    )

    class LanLifecycle:
        def __init__(self) -> None:
            self.quiescent = False
            self.shutdown_calls: list[float] = []

        def shutdown(self, *, timeout_seconds: float) -> bool:
            self.shutdown_calls.append(timeout_seconds)
            return self.quiescent

    lifecycle = LanLifecycle()
    owner.register_lifecycle_dependency("lan_scans", lifecycle)
    owner.start()

    assert owner.shutdown(timeout_seconds=0.01) is False
    assert lifecycle.shutdown_calls
    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        _build_manager(tmp_path)

    lifecycle.quiescent = True
    assert owner.shutdown(timeout_seconds=1.0) is True
    assert len(lifecycle.shutdown_calls) == 2
    successor = _build_manager(tmp_path)
    assert successor.shutdown(timeout_seconds=1.0) is True


def test_startup_dependency_runs_after_ownership_before_queued_run_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-order" / "state.db",
        memory_dir=tmp_path / "startup-order" / "memory",
        log_dir=tmp_path / "startup-order" / "logs",
        skills_dir=tmp_path / "startup-order" / "skills",
        plugins_dir=tmp_path / "startup-order" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=True,
        enforce_single_owner=True,
        auto_start=False,
    )
    order: list[str] = []

    class Dependency:
        def shutdown(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            order.append("dependency_shutdown")
            return True

    dependency = Dependency()
    manager.register_lifecycle_dependency("lan_scans", dependency)

    def start_lan() -> None:
        contender = PrimaryRuntimeOwnership(config.state_path)
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            contender.acquire()
        order.append("lan_recovered")

    manager.register_startup_dependency("lan_scans", start_lan)
    monkeypatch.setattr(manager, "reconcile_capabilities", lambda: order.append("capabilities"))
    monkeypatch.setattr(
        manager,
        "_reconcile_startup",
        lambda: order.append("run_reconciliation") or {"failed": [], "preserved": []},
    )
    monkeypatch.setattr(
        manager,
        "_reconcile_startup_workers",
        lambda: order.append("worker_reconciliation") or {"failed": [], "preserved": []},
    )
    monkeypatch.setattr(
        manager,
        "_resume_startup_queued_runs",
        lambda: order.append("queued_run_resume"),
    )

    manager.start()

    assert order == [
        "lan_recovered",
        "capabilities",
        "run_reconciliation",
        "worker_reconciliation",
        "queued_run_resume",
    ]
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_shutdown_cannot_cache_success_while_startup_ownership_acquire_is_inflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-ownership-acquire-race" / "state.db",
        memory_dir=tmp_path / "startup-ownership-acquire-race" / "memory",
        log_dir=tmp_path / "startup-ownership-acquire-race" / "logs",
        skills_dir=tmp_path / "startup-ownership-acquire-race" / "skills",
        plugins_dir=tmp_path / "startup-ownership-acquire-race" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
        auto_start=False,
    )
    ownership = manager._runtime_ownership  # noqa: SLF001
    assert ownership is not None
    acquire_entered = Event()
    release_acquire = Event()
    start_errors: list[str] = []
    acquire = ownership.acquire

    def blocked_acquire() -> None:
        acquire_entered.set()
        assert release_acquire.wait(timeout=3.0)
        acquire()

    monkeypatch.setattr(ownership, "acquire", blocked_acquire)

    def run_start() -> None:
        try:
            manager.start()
        except RuntimeError as exc:
            start_errors.append(str(exc))

    start_thread = Thread(target=run_start, name="startup-ownership-acquire-race")
    start_thread.start()
    assert acquire_entered.wait(timeout=2.0)
    first_shutdown = manager.shutdown(timeout_seconds=1.0)
    release_acquire.set()
    start_thread.join(timeout=3.0)

    retry_completed = False
    try:
        assert start_thread.is_alive() is False
        assert start_errors == ["runtime_manager_shut_down"]
        assert first_shutdown is False
        contender = PrimaryRuntimeOwnership(config.state_path)
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            contender.acquire()

        retry_completed = manager.shutdown(timeout_seconds=1.0)
        assert retry_completed is True
        contender.acquire()
        contender.release()
    finally:
        if not retry_completed:
            with manager._shutdown_condition:  # noqa: SLF001 - repair old RED state
                manager._shutdown_result = False  # noqa: SLF001
            manager._release_runtime_ownership()  # noqa: SLF001


def test_concurrent_shutdown_fences_startup_and_retains_owner_until_callback_exits(
    tmp_path: Path,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-shutdown-race" / "state.db",
        memory_dir=tmp_path / "startup-shutdown-race" / "memory",
        log_dir=tmp_path / "startup-shutdown-race" / "logs",
        skills_dir=tmp_path / "startup-shutdown-race" / "skills",
        plugins_dir=tmp_path / "startup-shutdown-race" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
        auto_start=False,
    )
    entered = Event()
    release = Event()
    startup_errors: list[str] = []

    class Lifecycle:
        def __init__(self) -> None:
            self.closed = False
            self.shutdown_calls = 0

        def shutdown(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            self.closed = True
            self.shutdown_calls += 1
            return True

    lifecycle = Lifecycle()

    def startup() -> None:
        entered.set()
        assert release.wait(timeout=3.0)
        if lifecycle.closed:
            raise RuntimeError("LAN lifecycle is shut down")

    def run_start() -> None:
        try:
            manager.start()
        except RuntimeError as exc:
            startup_errors.append(str(exc))

    manager.register_lifecycle_dependency("lan_scans", lifecycle)
    manager.register_startup_dependency("lan_scans", startup)
    start_thread = Thread(target=run_start, name="runtime-start-shutdown-race")
    start_thread.start()
    assert entered.wait(timeout=2.0)

    assert manager.shutdown(timeout_seconds=0.01) is False
    assert lifecycle.closed is True
    contender = PrimaryRuntimeOwnership(config.state_path)
    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        contender.acquire()

    release.set()
    start_thread.join(timeout=3.0)
    assert start_thread.is_alive() is False
    assert startup_errors == ["LAN lifecycle is shut down"]
    assert manager.started is False
    assert lifecycle.shutdown_calls == 1

    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        contender.acquire()
    assert manager.shutdown(timeout_seconds=1.0) is True
    assert lifecycle.shutdown_calls == 2
    contender.acquire()
    contender.release()


def test_startup_shutdown_overlap_keeps_owner_until_resumed_worker_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-worker-race" / "state.db",
        memory_dir=tmp_path / "startup-worker-race" / "memory",
        log_dir=tmp_path / "startup-worker-race" / "logs",
        skills_dir=tmp_path / "startup-worker-race" / "skills",
        plugins_dir=tmp_path / "startup-worker-race" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=True,
        enforce_single_owner=True,
        auto_start=False,
    )
    worker_started = Event()
    release_worker = Event()
    resume_paused = Event()
    release_resume = Event()
    shutdown_entered = Event()
    startup_errors: list[str] = []
    shutdown_results: list[bool] = []

    class Lifecycle:
        def shutdown(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            shutdown_entered.set()
            return True

    manager.register_lifecycle_dependency("lan_scans", Lifecycle())
    monkeypatch.setattr(manager, "reconcile_capabilities", lambda: None)
    monkeypatch.setattr(
        manager,
        "_reconcile_startup",
        lambda: {"failed": [], "preserved": []},
    )
    monkeypatch.setattr(
        manager,
        "_reconcile_startup_workers",
        lambda: {"failed": [], "preserved": []},
    )

    def blocked_worker(_run_id: str) -> None:
        worker_started.set()
        release_worker.wait(timeout=5.0)

    def resume_queued() -> None:
        manager._schedule_primary_run("startup-resumed-worker", blocked_worker)  # noqa: SLF001
        assert worker_started.wait(timeout=2.0)
        resume_paused.set()
        assert release_resume.wait(timeout=3.0)

    monkeypatch.setattr(manager, "_resume_startup_queued_runs", resume_queued)

    def run_start() -> None:
        try:
            manager.start()
        except RuntimeError as exc:
            startup_errors.append(str(exc))

    start_thread = Thread(target=run_start, name="startup-resumed-worker-race")
    start_thread.start()
    assert resume_paused.wait(timeout=2.0)

    shutdown_thread = Thread(
        target=lambda: shutdown_results.append(manager.shutdown(timeout_seconds=3.0)),
        name="shutdown-resumed-worker-race",
    )
    shutdown_thread.start()
    assert shutdown_entered.wait(timeout=2.0)
    release_resume.set()
    start_thread.join(timeout=2.0)

    assert start_thread.is_alive() is False
    assert startup_errors == ["runtime_manager_shut_down"]
    assert shutdown_thread.is_alive() is True
    contender = PrimaryRuntimeOwnership(config.state_path)
    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        contender.acquire()

    release_worker.set()
    shutdown_thread.join(timeout=3.0)
    assert shutdown_thread.is_alive() is False
    assert shutdown_results == [True]
    contender.acquire()
    contender.release()


def test_shutdown_after_last_startup_helper_check_is_caught_by_final_atomic_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-final-fence" / "state.db",
        memory_dir=tmp_path / "startup-final-fence" / "memory",
        log_dir=tmp_path / "startup-final-fence" / "logs",
        skills_dir=tmp_path / "startup-final-fence" / "skills",
        plugins_dir=tmp_path / "startup-final-fence" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=True,
        enforce_single_owner=True,
        auto_start=False,
    )
    last_check_returned = Event()
    release_last_check = Event()
    startup_errors: list[str] = []
    check_calls = 0
    check = manager._raise_if_startup_shutdown_requested  # noqa: SLF001

    monkeypatch.setattr(manager, "reconcile_capabilities", lambda: None)
    monkeypatch.setattr(
        manager,
        "_reconcile_startup",
        lambda: {"failed": [], "preserved": []},
    )
    monkeypatch.setattr(
        manager,
        "_reconcile_startup_workers",
        lambda: {"failed": [], "preserved": []},
    )
    monkeypatch.setattr(manager, "_resume_startup_queued_runs", lambda: None)

    def pause_after_fourth_check() -> None:
        nonlocal check_calls
        check()
        check_calls += 1
        if check_calls == 4:
            last_check_returned.set()
            assert release_last_check.wait(timeout=3.0)

    monkeypatch.setattr(
        manager,
        "_raise_if_startup_shutdown_requested",
        pause_after_fourth_check,
    )

    def run_start() -> None:
        try:
            manager.start()
        except RuntimeError as exc:
            startup_errors.append(str(exc))

    start_thread = Thread(target=run_start, name="startup-final-fence")
    start_thread.start()
    assert last_check_returned.wait(timeout=2.0)

    assert manager.shutdown(timeout_seconds=0.01) is False
    contender = PrimaryRuntimeOwnership(config.state_path)
    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        contender.acquire()

    release_last_check.set()
    start_thread.join(timeout=3.0)
    assert start_thread.is_alive() is False
    assert startup_errors == ["runtime_manager_shut_down"]
    assert manager.started is False
    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        contender.acquire()

    assert manager.shutdown(timeout_seconds=1.0) is True
    contender.acquire()
    contender.release()


def test_startup_dependency_registration_is_closed_while_start_is_in_progress(
    tmp_path: Path,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-registration" / "state.db",
        memory_dir=tmp_path / "startup-registration" / "memory",
        log_dir=tmp_path / "startup-registration" / "logs",
        skills_dir=tmp_path / "startup-registration" / "skills",
        plugins_dir=tmp_path / "startup-registration" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
        auto_start=False,
    )
    entered = Event()
    release = Event()
    startup_calls: list[str] = []

    class Lifecycle:
        def shutdown(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            return True

    def first_startup() -> None:
        startup_calls.append("first")
        entered.set()
        assert release.wait(timeout=3.0)

    manager.register_lifecycle_dependency("first", Lifecycle())
    manager.register_lifecycle_dependency("late", Lifecycle())
    manager.register_startup_dependency("first", first_startup)
    start_thread = Thread(target=manager.start, name="startup-registration-fence")
    start_thread.start()
    assert entered.wait(timeout=2.0)

    with pytest.raises(RuntimeError, match="^runtime_manager_already_started$"):
        manager.register_startup_dependency(
            "late",
            lambda: startup_calls.append("late"),
        )

    release.set()
    start_thread.join(timeout=3.0)
    assert start_thread.is_alive() is False
    assert startup_calls == ["first"]
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_concurrent_shutdown_wins_owner_release_during_start_failure_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-cleanup-election" / "state.db",
        memory_dir=tmp_path / "startup-cleanup-election" / "memory",
        log_dir=tmp_path / "startup-cleanup-election" / "logs",
        skills_dir=tmp_path / "startup-cleanup-election" / "skills",
        plugins_dir=tmp_path / "startup-cleanup-election" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
        auto_start=False,
    )
    first_cleanup_entered = Event()
    release_first_cleanup = Event()
    shutdown_skills_entered = Event()
    release_shutdown_skills = Event()
    cleanup_lock = Lock()
    cleanup_calls = 0
    startup_errors: list[str] = []
    shutdown_results: list[bool] = []

    class Lifecycle:
        def shutdown(self, *, timeout_seconds: float) -> bool:
            nonlocal cleanup_calls
            assert timeout_seconds >= 0
            with cleanup_lock:
                cleanup_calls += 1
                call = cleanup_calls
            if call == 1:
                first_cleanup_entered.set()
                assert release_first_cleanup.wait(timeout=3.0)
            return True

    manager.register_lifecycle_dependency("lan_scans", Lifecycle())

    def fail_capabilities() -> None:
        raise RuntimeError("injected startup capability failure")

    def block_shutdown_skills(*, timeout_seconds: float) -> bool:
        assert timeout_seconds >= 0
        shutdown_skills_entered.set()
        assert release_shutdown_skills.wait(timeout=3.0)
        return True

    monkeypatch.setattr(manager, "reconcile_capabilities", fail_capabilities)
    monkeypatch.setattr(manager.skills, "shutdown", block_shutdown_skills)

    def run_start() -> None:
        try:
            manager.start()
        except RuntimeError as exc:
            startup_errors.append(str(exc))

    start_thread = Thread(target=run_start, name="startup-failure-cleanup-election")
    start_thread.start()
    assert first_cleanup_entered.wait(timeout=2.0)

    shutdown_thread = Thread(
        target=lambda: shutdown_results.append(manager.shutdown(timeout_seconds=3.0)),
        name="shutdown-cleanup-election",
    )
    shutdown_thread.start()
    assert shutdown_skills_entered.wait(timeout=2.0)
    release_first_cleanup.set()
    start_thread.join(timeout=2.0)

    assert start_thread.is_alive() is False
    assert startup_errors == ["injected startup capability failure"]
    assert shutdown_thread.is_alive() is True
    contender = PrimaryRuntimeOwnership(config.state_path)
    with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
        contender.acquire()

    release_shutdown_skills.set()
    shutdown_thread.join(timeout=3.0)
    assert shutdown_thread.is_alive() is False
    assert shutdown_results == [True]
    contender.acquire()
    contender.release()


def test_external_shutdown_waits_for_start_failure_precleanup_before_owner_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-precleanup-authority" / "state.db",
        memory_dir=tmp_path / "startup-precleanup-authority" / "memory",
        log_dir=tmp_path / "startup-precleanup-authority" / "logs",
        skills_dir=tmp_path / "startup-precleanup-authority" / "skills",
        plugins_dir=tmp_path / "startup-precleanup-authority" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
        auto_start=False,
    )
    first_cleanup_entered = Event()
    second_cleanup_returned = Event()
    release_first_cleanup = Event()
    cleanup_lock = Lock()
    cleanup_calls = 0
    startup_errors: list[str] = []
    shutdown_results: list[bool] = []

    class Lifecycle:
        def shutdown(self, *, timeout_seconds: float) -> bool:
            nonlocal cleanup_calls
            assert timeout_seconds >= 0
            with cleanup_lock:
                cleanup_calls += 1
                call = cleanup_calls
            if call == 1:
                first_cleanup_entered.set()
                assert release_first_cleanup.wait(timeout=3.0)
            else:
                second_cleanup_returned.set()
            return True

    manager.register_lifecycle_dependency("lan_scans", Lifecycle())

    def fail_capabilities() -> None:
        raise RuntimeError("injected startup capability failure")

    monkeypatch.setattr(manager, "reconcile_capabilities", fail_capabilities)

    def run_start() -> None:
        try:
            manager.start()
        except RuntimeError as exc:
            startup_errors.append(str(exc))

    start_thread = Thread(target=run_start, name="startup-precleanup-authority")
    shutdown_thread = Thread(
        target=lambda: shutdown_results.append(manager.shutdown(timeout_seconds=3.0)),
        name="external-precleanup-authority",
    )
    start_thread.start()
    assert first_cleanup_entered.wait(timeout=2.0)
    shutdown_thread.start()
    assert second_cleanup_returned.wait(timeout=2.0)

    try:
        assert shutdown_thread.is_alive() is True
        contender = PrimaryRuntimeOwnership(config.state_path)
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            contender.acquire()
    finally:
        release_first_cleanup.set()
        start_thread.join(timeout=3.0)
        shutdown_thread.join(timeout=3.0)

    assert start_thread.is_alive() is False
    assert shutdown_thread.is_alive() is False
    assert startup_errors == ["injected startup capability failure"]
    assert shutdown_results == [True]
    successor = PrimaryRuntimeOwnership(config.state_path)
    successor.acquire()
    successor.release()
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_start_failure_cleanup_fences_late_lifecycle_and_startup_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-cleanup-registration-fence" / "state.db",
        memory_dir=tmp_path / "startup-cleanup-registration-fence" / "memory",
        log_dir=tmp_path / "startup-cleanup-registration-fence" / "logs",
        skills_dir=tmp_path / "startup-cleanup-registration-fence" / "skills",
        plugins_dir=tmp_path / "startup-cleanup-registration-fence" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
        auto_start=False,
    )
    cleanup_entered = Event()
    release_cleanup = Event()
    startup_errors: list[str] = []
    registration_errors: list[tuple[str, str]] = []

    class FirstLifecycle:
        def shutdown(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            cleanup_entered.set()
            assert release_cleanup.wait(timeout=3.0)
            return True

    class RecordingLifecycle:
        def __init__(self) -> None:
            self.shutdown_calls = 0

        def shutdown(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            self.shutdown_calls += 1
            return True

    startup_slot = RecordingLifecycle()
    late_lifecycle = RecordingLifecycle()
    manager.register_lifecycle_dependency("first", FirstLifecycle())
    manager.register_lifecycle_dependency("startup_slot", startup_slot)

    def fail_capabilities() -> None:
        raise RuntimeError("injected startup capability failure")

    monkeypatch.setattr(manager, "reconcile_capabilities", fail_capabilities)

    def run_start() -> None:
        try:
            manager.start()
        except RuntimeError as exc:
            startup_errors.append(str(exc))

    start_thread = Thread(target=run_start, name="startup-cleanup-registration-fence")
    start_thread.start()
    assert cleanup_entered.wait(timeout=2.0)

    try:
        manager.register_lifecycle_dependency("late", late_lifecycle)
    except RuntimeError as exc:
        registration_errors.append(("lifecycle", str(exc)))
    try:
        manager.register_startup_dependency("startup_slot", lambda: None)
    except RuntimeError as exc:
        registration_errors.append(("startup", str(exc)))

    release_cleanup.set()
    start_thread.join(timeout=3.0)
    assert start_thread.is_alive() is False
    assert startup_errors == ["injected startup capability failure"]
    assert registration_errors == [
        ("lifecycle", "run_manager_shutting_down"),
        ("startup", "runtime_manager_already_started"),
    ]
    assert startup_slot.shutdown_calls == 1
    assert late_lifecycle.shutdown_calls == 0

    successor = PrimaryRuntimeOwnership(config.state_path)
    successor.acquire()
    successor.release()
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_shutdown_baseexception_after_election_allows_retry_before_owner_release(
    tmp_path: Path,
) -> None:
    manager = _build_manager(tmp_path)

    class InterruptOnce:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt("injected teardown interrupt")
            return True

    lifecycle = InterruptOnce()
    manager.register_lifecycle_dependency("interrupt_once", lifecycle)
    retry_completed = False
    try:
        with pytest.raises(KeyboardInterrupt, match="injected teardown interrupt"):
            manager.shutdown(timeout_seconds=1.0)
        retry_completed = manager.shutdown(timeout_seconds=1.0)
        assert retry_completed is True

        successor = PrimaryRuntimeOwnership(manager.state.path)
        successor.acquire()
        successor.release()
    finally:
        if not retry_completed:
            with manager._shutdown_condition:  # noqa: SLF001 - repair old RED state
                manager._shutdown_owner = None  # noqa: SLF001
                manager._shutdown_result = False  # noqa: SLF001
                manager._shutdown_condition.notify_all()  # noqa: SLF001
            manager.shutdown(timeout_seconds=1.0)


def test_start_failure_pre_election_baseexception_preserves_original_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-pre-election-interrupt" / "state.db",
        memory_dir=tmp_path / "startup-pre-election-interrupt" / "memory",
        log_dir=tmp_path / "startup-pre-election-interrupt" / "logs",
        skills_dir=tmp_path / "startup-pre-election-interrupt" / "skills",
        plugins_dir=tmp_path / "startup-pre-election-interrupt" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
        auto_start=False,
    )

    class InterruptOnce:
        def __init__(self) -> None:
            self.calls = 0

        def shutdown(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds >= 0
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt("injected pre-election interrupt")
            return True

    lifecycle = InterruptOnce()
    manager.register_lifecycle_dependency("interrupt_once", lifecycle)

    def fail_capabilities() -> None:
        raise RuntimeError("injected original startup failure")

    monkeypatch.setattr(manager, "reconcile_capabilities", fail_capabilities)
    retry_completed = False
    try:
        with pytest.raises(RuntimeError, match="injected original startup failure"):
            manager.start()

        contender = PrimaryRuntimeOwnership(config.state_path)
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            contender.acquire()
        retry_completed = manager.shutdown(timeout_seconds=1.0)
        assert retry_completed is True
        contender.acquire()
        contender.release()
    finally:
        if not retry_completed:
            manager.shutdown(timeout_seconds=1.0)


def test_start_failure_post_election_baseexception_preserves_original_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-post-election-interrupt" / "state.db",
        memory_dir=tmp_path / "startup-post-election-interrupt" / "memory",
        log_dir=tmp_path / "startup-post-election-interrupt" / "logs",
        skills_dir=tmp_path / "startup-post-election-interrupt" / "skills",
        plugins_dir=tmp_path / "startup-post-election-interrupt" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=False,
        enforce_single_owner=True,
        auto_start=False,
    )
    skill_shutdown_calls = 0

    def interrupt_skills_once(*, timeout_seconds: float) -> bool:
        nonlocal skill_shutdown_calls
        assert timeout_seconds >= 0
        skill_shutdown_calls += 1
        if skill_shutdown_calls == 1:
            raise KeyboardInterrupt("injected post-election interrupt")
        return True

    def fail_capabilities() -> None:
        raise RuntimeError("injected original startup failure")

    monkeypatch.setattr(manager, "reconcile_capabilities", fail_capabilities)
    monkeypatch.setattr(manager.skills, "shutdown", interrupt_skills_once)
    retry_completed = False
    try:
        with pytest.raises(RuntimeError, match="injected original startup failure"):
            manager.start()

        contender = PrimaryRuntimeOwnership(config.state_path)
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            contender.acquire()
        retry_completed = manager.shutdown(timeout_seconds=1.0)
        assert retry_completed is True
        contender.acquire()
        contender.release()
    finally:
        if not retry_completed:
            manager.shutdown(timeout_seconds=1.0)


def test_nonshutdown_start_failure_drains_resumed_worker_before_owner_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "startup-failure-worker-drain" / "state.db",
        memory_dir=tmp_path / "startup-failure-worker-drain" / "memory",
        log_dir=tmp_path / "startup-failure-worker-drain" / "logs",
        skills_dir=tmp_path / "startup-failure-worker-drain" / "skills",
        plugins_dir=tmp_path / "startup-failure-worker-drain" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        recover_startup_work=True,
        enforce_single_owner=True,
        auto_start=False,
    )
    worker_started = Event()

    monkeypatch.setattr(manager, "reconcile_capabilities", lambda: None)
    monkeypatch.setattr(
        manager,
        "_reconcile_startup",
        lambda: {"failed": [], "preserved": []},
    )
    monkeypatch.setattr(
        manager,
        "_reconcile_startup_workers",
        lambda: {"failed": [], "preserved": []},
    )

    def worker(_run_id: str) -> None:
        worker_started.set()
        manager._shutdown_event.wait(timeout=3.0)  # noqa: SLF001

    def resume_then_fail() -> None:
        manager._schedule_primary_run("startup-failure-worker", worker)  # noqa: SLF001
        assert worker_started.wait(timeout=2.0)
        raise RuntimeError("injected post-resume startup failure")

    monkeypatch.setattr(manager, "_resume_startup_queued_runs", resume_then_fail)

    try:
        with pytest.raises(RuntimeError, match="injected post-resume startup failure"):
            manager.start()

        assert all(not thread.is_alive() for thread in manager._threads.values())  # noqa: SLF001
        contender = PrimaryRuntimeOwnership(config.state_path)
        contender.acquire()
        contender.release()
    finally:
        manager._shutdown_event.set()  # noqa: SLF001
        assert manager.shutdown(timeout_seconds=1.0) is True


def test_auto_start_failure_timeout_retains_owner_until_resumed_worker_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "auto-start-failure-timeout" / "state.db",
        memory_dir=tmp_path / "auto-start-failure-timeout" / "memory",
        log_dir=tmp_path / "auto-start-failure-timeout" / "logs",
        skills_dir=tmp_path / "auto-start-failure-timeout" / "skills",
        plugins_dir=tmp_path / "auto-start-failure-timeout" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    worker_started = Event()
    release_worker = Event()
    retained: dict[str, RunManager] = {}

    class DeadlineClock:
        def __init__(self) -> None:
            self.first = True

        def __call__(self) -> float:
            if self.first:
                self.first = False
                return 0.0
            return 6.0

    monkeypatch.setattr(run_manager_module, "monotonic", DeadlineClock())

    class FailingAutoStartManager(RunManager):
        def reconcile_capabilities(self) -> None:
            return

        def _reconcile_startup(self) -> dict[str, list[str]]:
            return {"failed": [], "preserved": []}

        def _reconcile_startup_workers(self) -> dict[str, list[str]]:
            return {"failed": [], "preserved": []}

        def _resume_startup_queued_runs(self) -> None:
            retained["manager"] = self

            def worker(_run_id: str) -> None:
                worker_started.set()
                release_worker.wait(timeout=3.0)

            self._schedule_primary_run("auto-start-failure-worker", worker)
            assert worker_started.wait(timeout=2.0)
            raise RuntimeError("injected auto-start post-resume failure")

    try:
        with pytest.raises(RuntimeError, match="injected auto-start post-resume failure"):
            FailingAutoStartManager(
                config=config,
                state=state,
                events=RunEventBus(state),
                mcp=MCPManager(state),
                skills=SkillManager(config.skills_dir, state),
                plugins=_PluginProbe(),
                recover_startup_work=True,
                enforce_single_owner=True,
            )

        manager = retained["manager"]
        assert any(thread.is_alive() for thread in manager._threads.values())  # noqa: SLF001
        contender = PrimaryRuntimeOwnership(config.state_path)
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            contender.acquire()
    finally:
        release_worker.set()
        manager = retained.get("manager")
        if manager is not None:
            assert manager.shutdown(timeout_seconds=1.0) is True

    successor = PrimaryRuntimeOwnership(config.state_path)
    successor.acquire()
    successor.release()


def test_server_acquires_primary_ownership_before_lan_recovery(tmp_path: Path) -> None:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "server-lan" / "state.db",
        memory_dir=tmp_path / "server-lan" / "memory",
        log_dir=tmp_path / "server-lan" / "logs",
        skills_dir=tmp_path / "server-lan" / "skills",
        plugins_dir=tmp_path / "server-lan" / "plugins",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    ledger = LanDiscoveryLedger(state)
    interface = NetworkInterface.from_addresses(
        os_identity="darwin:en91",
        display_name="Recovery fixture",
        addresses=("192.168.91.1/30",),
    )
    draft = ledger.create_scan(
        scan_id="lan_server_recovery",
        owner_principal="owner:local-runtime:v1",
        confirmed_interface_id=interface.interface_id,
        network="192.168.91.0/30",
        limits=asdict(LanScanLimits()),
        preview_digest="sha256:" + "7" * 64,
        expected_revision=0,
    )
    running = ledger.transition_scan(
        draft.scan_id,
        "running",
        expected_revision=draft.revision,
    )
    app = create_app(config)
    contender = PrimaryRuntimeOwnership(config.state_path)
    contender.acquire()
    try:
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            with TestClient(app):
                pass
        assert ledger.get_scan(running.scan_id) == running
    finally:
        contender.release()

    with TestClient(create_app(config)) as client:
        assert client.get("/api/health/live").status_code == 200
        recovered = ledger.get_scan(running.scan_id)
        assert recovered is not None
        assert recovered.status == "interrupted"


def test_real_server_lan_manager_retains_owner_and_executor_until_quiescent_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task6 = import_module("nested_memvid_agent.lan_scan_manager")
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        state_path=tmp_path / "server-real-lan" / "state.db",
        memory_dir=tmp_path / "server-real-lan" / "memory",
        log_dir=tmp_path / "server-real-lan" / "logs",
        skills_dir=tmp_path / "server-real-lan" / "skills",
        plugins_dir=tmp_path / "server-real-lan" / "plugins",
        workspace=tmp_path,
        provider_startup_probe=True,
    )
    state = AgentStateStore(config.state_path)
    interface = NetworkInterface.from_addresses(
        os_identity="darwin:en-real-server",
        display_name="Real server LAN fixture",
        addresses=("192.168.93.1/30",),
    )
    seed = LanDiscoveryLedger(state)
    draft = seed.create_scan(
        scan_id="lan_server_prior_process",
        owner_principal="owner:local-runtime:v1",
        confirmed_interface_id=interface.interface_id,
        network="192.168.93.0/30",
        limits=asdict(LanScanLimits()),
        preview_digest="sha256:" + "8" * 64,
        expected_revision=0,
    )
    prior_running = seed.transition_scan(
        draft.scan_id,
        "running",
        expected_revision=draft.revision,
    )
    entered = Event()
    release = Event()
    provider_started = Event()
    order: list[str] = []
    managers: list[Any] = []
    runs: list[RunManager] = []
    executors: list[Any] = []

    def stuck_scanner(*_args: Any, **_kwargs: Any) -> tuple[()]:
        order.append("scan_admitted")
        entered.set()
        release.wait(timeout=10)
        return ()

    def lan_manager_factory(**kwargs: Any) -> Any:
        kwargs.update(
            interface_enumerator=lambda: (interface,),
            mdns_availability=lambda: task6.MdnsAvailability.UNAVAILABLE,
            scanner=stuck_scanner,
            scan_id_factory=lambda: "lan_server_stuck",
        )
        manager = task6.LanScanManager(**kwargs)
        original_start_lifecycle = manager.start_lifecycle

        def start_lifecycle(executor: Any) -> Any:
            contender = PrimaryRuntimeOwnership(config.state_path)
            with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
                contender.acquire()
            order.append("lan_recovery_begin")
            recovered = original_start_lifecycle(executor)
            order.append("lan_recovery_complete")
            return recovered

        manager.start_lifecycle = start_lifecycle
        managers.append(manager)
        return manager

    real_build_run_manager = server_module.build_run_manager

    def capture_run_manager(**kwargs: Any) -> Any:
        build = real_build_run_manager(**kwargs)
        original_start = build.runs.start

        def start() -> None:
            order.append("runs_start_begin")
            original_start()
            order.append("runs_start_complete")

        build.runs.start = start  # type: ignore[method-assign]
        runs.append(build.runs)
        return build

    class SpyExecutor(ThreadPoolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.configured_workers = kwargs.get("max_workers", args[0] if args else None)
            self.shutdown_quiescence: list[bool] = []
            executors.append(self)

        def shutdown(
            self,
            wait: bool = True,
            *,
            cancel_futures: bool = False,
        ) -> None:
            self.shutdown_quiescence.append(bool(managers) and managers[0].is_quiescent())
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def provider_probe(**_kwargs: Any) -> None:
        order.append("provider_startup")
        provider_started.set()

    monkeypatch.setattr(server_module, "LanScanManager", lan_manager_factory, raising=False)
    monkeypatch.setattr(server_module, "ThreadPoolExecutor", SpyExecutor, raising=False)
    monkeypatch.setattr(server_module, "build_run_manager", capture_run_manager)
    monkeypatch.setattr(server_module, "_probe_provider_health", provider_probe)

    app = create_app(config)
    assert len(managers) == 1
    assert executors == []
    assert seed.get_scan(prior_running.scan_id) == prior_running

    with TestClient(app) as client:
        assert client.get("/api/health/live").status_code == 200
        assert len(managers) == 1
        assert len(runs) == 1
        assert len(executors) == 1
        assert executors[0].configured_workers == 17
        assert provider_started.wait(timeout=2)
        recovered = seed.get_scan(prior_running.scan_id)
        assert recovered is not None and recovered.status == "interrupted"
        assert order.index("runs_start_begin") < order.index("lan_recovery_begin")
        assert order.index("lan_recovery_complete") < order.index("runs_start_complete")
        assert order.index("runs_start_complete") < order.index("provider_startup")

        manager = managers[0]
        authorization = manager.preview(interface.interface_id, "192.168.93.0/30")
        scan = manager.create_draft(authorization)
        manager.start(
            scan.scan_id,
            expected_revision=scan.revision,
            authorization=authorization,
            preview_digest=authorization.preview_digest,
        )
        assert entered.wait(timeout=2)
        assert order.index("lan_recovery_complete") < order.index("scan_admitted")

        assert runs[0].shutdown(timeout_seconds=0.01) is False
        current = manager.get(scan.scan_id)
        assert current.status == "cancelling"
        assert current.terminal_receipt is None
        assert executors[0].shutdown_quiescence == []
        contender = PrimaryRuntimeOwnership(config.state_path)
        with pytest.raises(RuntimeOwnershipError, match=f"^{RUNTIME_OWNERSHIP_ERROR}$"):
            contender.acquire()

        release.set()
        assert runs[0].shutdown(timeout_seconds=2.0) is True
        assert manager.get(scan.scan_id).status == "cancelled"
        assert executors[0].shutdown_quiescence == [True]
        contender.acquire()
        contender.release()


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
            [
                sys.executable,
                "-m",
                "nested_memvid_agent.cli",
                "run",
                *common,
                "--json",
                "blocked contender",
            ],
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
        [
            sys.executable,
            "-m",
            "nested_memvid_agent.cli",
            "run",
            *common,
            "--json",
            "successor run",
        ],
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
        assert {item["id"] for item in state.list_skills()} == {"plugin.readonly.stale"}

        mutating = run_cli("plugins", "disable", *common, "readonly", "--json")
        assert mutating.returncode == 1
        assert "Another Kestrel runtime already owns this state database" in mutating.stderr
        assert _extension_state_snapshot(state) == before
    finally:
        assert owner.shutdown(timeout_seconds=2.0) is True
