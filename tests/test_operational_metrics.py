from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
    assert snapshot["process"]["pid"] > 0
    assert readiness_snapshot(config=config, state=state, runs=_Runs())["ok"] is True


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
