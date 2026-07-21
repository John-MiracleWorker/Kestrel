from __future__ import annotations

import os
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import numpy as np
import pytest

from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.layers import LayeredMemorySystem, load_layer_specs
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MEMVID_INTEGRATION") != "1",
    reason="Set RUN_MEMVID_INTEGRATION=1 and install memvid-sdk to run Memvid integration tests.",
)


@pytest.fixture(autouse=True)
def _require_memvid_sdk() -> None:
    pytest.importorskip("memvid_sdk")


def test_memvid_layered_memory_creates_one_mv2_per_layer_and_reopens_existing_files(tmp_path: Path) -> None:
    # This test seeds the storage adapter below the promotion-policy boundary.
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        MemvidBackend,
        enforce_stable_write_integrity=False,
    )
    try:
        assert {path.name for path in (tmp_path / "memory").glob("*.mv2")} == {
            "working.mv2",
            "episodic.mv2",
            "semantic.mv2",
            "procedural.mv2",
            "self.mv2",
            "policy.mv2",
        }
        memory.put(
            MemoryRecord(
                id="memvid-system-fact",
                title="Memvid system fact",
                content="sentinel_memvid_system_81aa survives seal and reopen.",
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.FACT,
                confidence=0.92,
                metadata={"frame_id": "memvid-system-frame", "frame_type": "section_summary"},
            )
        )
        memory.seal_all()
        assert memory.verify_all()[MemoryLayer.SEMANTIC] is True
    finally:
        memory.close_all()

    reopened = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", MemvidBackend)
    try:
        hits = reopened.retrieve(
            RetrievalQuery(query="sentinel_memvid_system_81aa", layers=(MemoryLayer.SEMANTIC,), k_per_layer=5)
        )
        assert hits
        assert hits[0].record.metadata["frame_id"] == "memvid-system-frame"
        assert reopened.tombstone(MemoryLayer.SEMANTIC, "memvid-system-fact", reason="integration", superseded_by="next")
        reopened.seal_all()
    finally:
        reopened.close_all()

    final = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", MemvidBackend)
    try:
        assert final.get_record(MemoryLayer.SEMANTIC, "memvid-system-fact", include_inactive=False) is None
        inactive = final.get_record(MemoryLayer.SEMANTIC, "memvid-system-fact", include_inactive=True)
        assert inactive is not None
        assert inactive.metadata["active"] is False
        inactive_hits = final.retrieve(
            RetrievalQuery(
                query="sentinel_memvid_system_81aa",
                layers=(MemoryLayer.SEMANTIC,),
                include_inactive=True,
            )
        )
        assert inactive_hits
    finally:
        final.close_all()


def test_memvid_run_manager_serializes_two_runs_and_reopens_each_layer(tmp_path: Path) -> None:
    config = AgentConfig(
        backend="memvid",
        provider="mock",
        model="mock",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        max_concurrent_runs=4,
        max_queued_runs=2,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
    )
    first_started = Event()
    release_first = Event()
    started: list[str] = []

    def controlled_turn(
        run_id: str,
        run_config: AgentConfig,
        _message: str,
        _session_id: str,
    ) -> None:
        agent = manager._build_agent(run_config)
        try:
            state.transition_run(run_id, "running")
            started.append(run_id)
            if len(started) == 1:
                first_started.set()
                assert release_first.wait(timeout=10)
            state.transition_run(run_id, "completed", stop_reason="done")
        finally:
            agent.close()

    manager._run_agent_turn = controlled_turn  # type: ignore[method-assign]
    try:
        first = manager.create_run(message="first", autonomy_mode="manual")
        assert first_started.wait(timeout=10)
        second = manager.create_run(message="second", autonomy_mode="manual")
        assert state.get_run(second.run_id).status == "queued"
        assert manager.capacity_snapshot()["max_active"] == 1
        release_first.set()
        assert _wait_for_run_status(state, first.run_id, "completed")
        assert _wait_for_run_status(state, second.run_id, "completed")
        assert started == [first.run_id, second.run_id]
    finally:
        release_first.set()
        assert manager.shutdown(timeout_seconds=10.0)

    assert {path.name for path in config.memory_dir.glob("*.mv2")} == {
        "working.mv2",
        "episodic.mv2",
        "semantic.mv2",
        "procedural.mv2",
        "self.mv2",
        "policy.mv2",
    }

    reopened = LayeredMemorySystem.from_backend_factory(config.memory_dir, MemvidBackend)
    reopened.close_all()


def test_memvid_autonomous_scheduler_releases_primary_agent_between_workers(
    tmp_path: Path,
) -> None:
    config = AgentConfig(
        backend="memvid",
        provider="mock",
        model="mock",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        enable_autonomous_scheduler=True,
        max_concurrent_runs=4,
        max_scheduler_tasks=1,
        max_scheduler_cycles=5,
    )
    state = AgentStateStore(config.state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
    )
    try:
        run = manager.create_run(
            message="Complete the autonomous low-risk chain",
            session_id="session",
        )
        terminal = _wait_for_terminal_run(state, run.run_id)
        assert terminal == "completed"
        child_statuses = [
            task.status
            for task in state.list_task_nodes(run.run_id)
            if task.parent_id is not None
        ]
        assert child_statuses == ["completed", "completed", "completed"]
    finally:
        assert manager.shutdown(timeout_seconds=10.0)


def test_memvid_layered_memory_uses_rebuildable_vector_sidecar(tmp_path: Path) -> None:
    layer_config = tmp_path / "layers.json"
    layer_config.write_text(
        """
        {
          "semantic": {
            "search_mode": "hybrid",
            "vector": {
              "enabled": true,
              "embedding_provider": "local",
              "embedding_model": "concept-test",
              "index_path": "semantic.mv2.vector.sqlite"
            }
          }
        }
        """,
        encoding="utf-8",
    )
    specs = load_layer_specs(layer_config)
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        MemvidBackend,
        specs=specs,
        vector_embedder=_ConceptEmbedder(),
        # This test seeds the storage adapter below the promotion-policy boundary.
        enforce_stable_write_integrity=False,
    )
    try:
        memory.put(
            MemoryRecord(
                id="memvid-vector-fact",
                title="Python path import fix",
                content="Set PYTHONPATH before pytest invocations.",
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.FACT,
                confidence=0.92,
            )
        )
        memory.seal_all()
        status = memory.vector_index_status()[MemoryLayer.SEMANTIC]
        assert status.enabled is True
        assert status.indexed_count == 1
    finally:
        memory.close_all()

    sidecar_path = tmp_path / "memory" / "semantic.mv2.vector.sqlite"
    assert sidecar_path.exists()
    assert b"Set PYTHONPATH before pytest invocations" not in sidecar_path.read_bytes()

    reopened = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        MemvidBackend,
        specs=specs,
        vector_embedder=_ConceptEmbedder(),
    )
    try:
        hits = reopened.retrieve(
            RetrievalQuery(
                query="module discovery needs sys route",
                layers=(MemoryLayer.SEMANTIC,),
                mode="hybrid",
            )
        )
        assert hits
        assert hits[0].record.id == "memvid-vector-fact"
        assert hits[0].source_backend == "vector_sidecar"
    finally:
        reopened.close_all()


class _ConceptEmbedder:
    model_name = "concept-test"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(1, dtype=np.float32)
        synonyms = {"pythonpath": 0, "module": 0, "sys": 0}
        for raw in text.lower().replace(".", " ").split():
            idx = synonyms.get(raw.strip())
            if idx is not None:
                vector[idx] = 1.0
        return vector


def _wait_for_run_status(
    state: AgentStateStore,
    run_id: str,
    status: str,
    *,
    timeout_seconds: float = 10.0,
) -> bool:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if state.get_run(run_id).status == status:
            return True
        sleep(0.01)
    return state.get_run(run_id).status == status


def _wait_for_terminal_run(
    state: AgentStateStore,
    run_id: str,
    *,
    timeout_seconds: float = 20.0,
) -> str:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        status = state.get_run(run_id).status
        if status in {"completed", "failed", "blocked", "cancelled"}:
            return status
        sleep(0.01)
    return state.get_run(run_id).status
