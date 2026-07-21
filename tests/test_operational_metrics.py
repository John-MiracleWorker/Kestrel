from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest

from nested_memvid_agent import operational_metrics as operational_metrics_module
from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS
from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.operational_metrics import (
    operational_snapshot,
    prometheus_snapshot,
    readiness_snapshot,
)
from nested_memvid_agent.state_store import SCHEMA_VERSION, AgentStateStore


class _Runs:
    def __init__(self, *, queued: int = 0, max_queued: int = 4) -> None:
        self.queued = queued
        self.max_queued = max_queued

    def capacity_snapshot(self) -> dict[str, int]:
        return {
            "active": 0,
            "queued": self.queued,
            "reserved": 0,
            "max_active": 1,
            "max_queued": self.max_queued,
        }

    def operational_counters(self) -> dict[str, int]:
        return {"started": 3, "completed": 2}


class _RoutineLoop:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def status(self) -> object:
        payload = self.payload

        class _Status:
            def to_dict(self) -> dict[str, object]:
                return dict(payload)

        return _Status()


def test_operational_snapshot_reports_state_integrity_and_writable_storage(tmp_path) -> None:
    config = AgentConfig(
        backend="memory",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        channel_config_path=tmp_path / "channels.json",
    )
    config.memory_dir.mkdir()
    state = AgentStateStore(config.state_path)

    snapshot = operational_snapshot(config=config, state=state, runs=_Runs())

    assert snapshot["state"] == {
        "ok": True,
        "integrity": "ok",
        "schema_version": SCHEMA_VERSION,
        "writable": True,
        "error_type": None,
    }
    assert snapshot["memory"]["available"] is True
    assert snapshot["memory"]["writable"] is True
    assert snapshot["workers"]["by_status"] == {}
    assert snapshot["proactive_routines"] == {"status": "disabled", "enabled": False}
    assert snapshot["process"]["pid"] > 0
    assert readiness_snapshot(config=config, state=state, runs=_Runs())["ok"] is True


def test_telegram_poller_health_defaults_to_the_instance_state_directory(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("KESTREL_TELEGRAM_HEALTH_PATH", raising=False)
    first = AgentConfig(state_path=tmp_path / "first" / "agent.db")
    second = AgentConfig(state_path=tmp_path / "second" / "agent.db")
    first_health_path = first.state_path.parent / "telegram-poller-health.json"
    first_health_path.parent.mkdir(parents=True)
    first_health_path.write_text(
        json.dumps(
            {
                "status": "healthy",
                "updated_at_epoch": datetime.now(UTC).timestamp(),
                "pid": 101,
            }
        ),
        encoding="utf-8",
    )

    assert operational_metrics_module._telegram_poller_health(first)["pid"] == 101
    assert operational_metrics_module._telegram_poller_health(second) == {
        "status": "not_configured"
    }


def test_telegram_poller_health_preserves_explicit_path_override(
    tmp_path, monkeypatch
) -> None:
    override = tmp_path / "operator" / "poller.json"
    override.parent.mkdir()
    override.write_text(
        json.dumps(
            {
                "status": "healthy",
                "updated_at_epoch": datetime.now(UTC).timestamp(),
                "pid": 202,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KESTREL_TELEGRAM_HEALTH_PATH", str(override))
    config = AgentConfig(state_path=tmp_path / "otherwise" / "agent.db")

    assert operational_metrics_module._telegram_poller_health(config)["pid"] == 202


def test_state_health_snapshot_coalesces_only_concurrent_checks(
    tmp_path, monkeypatch
) -> None:
    class _BlockingState:
        path = tmp_path / "state.db"

        def __init__(self) -> None:
            self.calls = 0
            self.call_lock = Lock()
            self.entered = Event()
            self.release = Event()

        def health_snapshot(self) -> dict[str, object]:
            with self.call_lock:
                self.calls += 1
            self.entered.set()
            assert self.release.wait(timeout=2)
            return {"ok": True, "integrity": "ok", "writable": True}

    state = _BlockingState()
    workers = 8
    barrier = Barrier(workers)
    follower_lock = Lock()
    followers_ready = Event()
    follower_count = 0

    class _TrackingFuture(Future[dict[str, object]]):
        def result(self, timeout: float | None = None) -> dict[str, object]:
            nonlocal follower_count
            with follower_lock:
                follower_count += 1
                if follower_count == workers - 1:
                    followers_ready.set()
            return super().result(timeout=timeout)

    monkeypatch.setattr(operational_metrics_module, "Future", _TrackingFuture)

    def check() -> dict[str, object]:
        barrier.wait(timeout=2)
        return operational_metrics_module._state_health_snapshot(state)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(check) for _ in range(workers)]
        assert state.entered.wait(timeout=2)
        assert followers_ready.wait(timeout=2)
        state.release.set()
        results = [future.result(timeout=2) for future in futures]

    assert state.calls == 1
    assert all(result["ok"] is True for result in results)

    state.release.clear()
    state.release.set()
    operational_metrics_module._state_health_snapshot(state)
    assert state.calls == 2


def test_readiness_requires_healthy_enabled_proactive_routine_loop(tmp_path) -> None:
    config = AgentConfig(
        backend="memory",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        channel_config_path=tmp_path / "channels.json",
        enable_proactive_routines=True,
    )
    config.memory_dir.mkdir()
    state = AgentStateStore(config.state_path)
    healthy = _RoutineLoop(
        {
            "running": True,
            "tick_count": 2,
            "last_error": None,
            "tick_in_progress": False,
            "current_tick_age_seconds": None,
        }
    )

    readiness = readiness_snapshot(
        config=config,
        state=state,
        runs=_Runs(),
        routine_loop=healthy,
    )

    assert readiness["ok"] is True
    assert readiness["proactive_routines"]["status"] == "healthy"


@pytest.mark.parametrize(
    ("loop", "expected_status"),
    [
        (None, "unavailable"),
        (
            _RoutineLoop(
                {
                    "running": False,
                    "tick_count": 1,
                    "last_error": None,
                    "tick_in_progress": False,
                }
            ),
            "stopped",
        ),
        (
            _RoutineLoop(
                {
                    "running": True,
                    "tick_count": 1,
                    "last_error": "redacted failure",
                    "tick_in_progress": False,
                }
            ),
            "error",
        ),
        (
            _RoutineLoop(
                {
                    "running": True,
                    "tick_count": 1,
                    "last_error": None,
                    "tick_in_progress": True,
                    "current_tick_age_seconds": 121.0,
                }
            ),
            "stale",
        ),
    ],
)
def test_readiness_fails_for_unhealthy_proactive_routine_loop(
    tmp_path,
    loop,
    expected_status,
) -> None:
    config = AgentConfig(
        backend="memory",
        state_path=tmp_path / f"{expected_status}.db",
        memory_dir=tmp_path / f"memory-{expected_status}",
        channel_config_path=tmp_path / "channels.json",
        enable_proactive_routines=True,
    )
    config.memory_dir.mkdir()
    state = AgentStateStore(config.state_path)

    readiness = readiness_snapshot(
        config=config,
        state=state,
        runs=_Runs(),
        routine_loop=loop,
    )

    assert readiness["ok"] is False
    assert readiness["proactive_routines"]["status"] == expected_status
    assert "proactive_routine_loop_unhealthy" in readiness["reasons"]


def test_prometheus_snapshot_uses_operational_snapshot_field_contract(
    tmp_path, monkeypatch
) -> None:
    config = AgentConfig(
        backend="memory",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        channel_config_path=tmp_path / "channels.json",
    )
    config.memory_dir.mkdir()
    (config.memory_dir / "working.mv2").write_bytes(b"memory")
    state = AgentStateStore(config.state_path)
    monkeypatch.setattr(
        operational_metrics_module,
        "_process_resource_snapshot",
        lambda: {
            "pid": 123,
            "cpu_seconds": 1.5,
            "thread_count": 4,
            "max_rss_bytes": 4096,
        },
    )

    snapshot = operational_snapshot(config=config, state=state, runs=_Runs())
    rendered = prometheus_snapshot(snapshot)

    assert snapshot["runs"]["counters"] == {"started": 3, "completed": 2}
    assert snapshot["memory"]["total_bytes"] == 6
    assert "kestrel_process_rss_bytes 4096" in rendered
    assert 'kestrel_run_operations{operation="started"} 3' in rendered
    assert 'kestrel_run_operations{operation="completed"} 2' in rendered
    assert "kestrel_memory_total_bytes 6" in rendered


def test_readiness_fails_for_saturated_queue_and_missing_memvid_layers(tmp_path) -> None:
    config = AgentConfig(
        backend="memvid",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        channel_config_path=tmp_path / "channels.json",
    )
    config.memory_dir.mkdir()
    state = AgentStateStore(config.state_path)

    readiness = readiness_snapshot(
        config=config,
        state=state,
        runs=_Runs(queued=1, max_queued=1),
    )

    assert readiness["ok"] is False
    assert "run_queue_saturated" in readiness["reasons"]
    assert "memory_store_unhealthy" in readiness["reasons"]


def test_readiness_fails_closed_when_memvid_integrity_check_fails(tmp_path, monkeypatch) -> None:
    config = AgentConfig(
        backend="memvid",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        channel_config_path=tmp_path / "channels.json",
    )
    config.memory_dir.mkdir()
    for spec in DEFAULT_LAYER_SPECS.values():
        (config.memory_dir / spec.mv2_file).write_bytes(b"corrupt")
    monkeypatch.setattr(
        operational_metrics_module,
        "_check_memvid_layer",
        lambda _path, _layer: (False, "integrity_failed"),
    )
    state = AgentStateStore(config.state_path)

    readiness = readiness_snapshot(config=config, state=state, runs=_Runs())

    assert readiness["ok"] is False
    assert "memory_store_unhealthy" in readiness["reasons"]
    assert set(readiness["memory"]["invalid_layers"]) == {
        layer.value for layer in DEFAULT_LAYER_SPECS
    }
    assert all(
        layer["integrity_error"] == "integrity_failed"
        for layer in readiness["memory"]["layers"].values()
    )


def test_memvid_readiness_probe_does_not_block_an_active_writer(tmp_path, monkeypatch) -> None:
    path = tmp_path / "working.mv2"
    path.write_bytes(b"existing")
    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=lambda *args, **kwargs: object(),
            use=lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
        ),
    )
    writer = MemvidBackend(path, MemoryLayer.WORKING)
    writer.open()
    try:
        assert operational_metrics_module._check_memvid_layer(path, MemoryLayer.WORKING) == (
            True,
            "busy",
        )
    finally:
        writer.close()


def test_readiness_requires_observed_operational_health_for_non_mock_provider(tmp_path) -> None:
    config = AgentConfig(
        provider="openai-compatible",
        model="unprobed-test-model",
        backend="memory",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        channel_config_path=tmp_path / "channels.json",
    )
    config.memory_dir.mkdir()
    state = AgentStateStore(config.state_path)

    readiness = readiness_snapshot(config=config, state=state, runs=_Runs())

    assert readiness["ok"] is False
    assert "provider_not_verified" in readiness["reasons"]
    assert readiness["provider"]["state"] == "unknown"


def test_readiness_rejects_a_running_run_with_an_expired_lease(tmp_path) -> None:
    config = AgentConfig(
        backend="memory",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        channel_config_path=tmp_path / "channels.json",
    )
    config.memory_dir.mkdir()
    state = AgentStateStore(config.state_path)
    state.create_run(
        run_id="expired",
        message="active",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    state.transition_run("expired", "running")
    assert state.acquire_run_lease(
        "expired",
        owner="dead-worker",
        ttl_seconds=1,
        now=datetime.now(UTC) - timedelta(seconds=60),
    ) is not None

    readiness = readiness_snapshot(config=config, state=state, runs=_Runs())

    assert readiness["ok"] is False
    assert readiness["orphaned_run_ids"] == ["expired"]
    assert "orphaned_running_runs" in readiness["reasons"]
