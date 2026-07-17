from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.cognition import FailureEpisode, LessonCard
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.memory_backup import MemoryBackupManager
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.runtime_models import StrategyProposal, ToolCall, ToolExecution

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MEMVID_INTEGRATION") != "1",
    reason="Set RUN_MEMVID_INTEGRATION=1 and install memvid-sdk to run Memvid integration tests.",
)


def test_memvid_backend_write_seal_verify_reopen_search(tmp_path: Path) -> None:
    path = tmp_path / "semantic.mv2"
    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    backend.put(
        MemoryRecord(
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            title="Integration fact",
            content="Memvid integration test fact about nested learning.",
            confidence=0.9,
        )
    )
    backend.seal()
    assert backend.verify()
    backend.close()

    reopened = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    reopened.open()
    try:
        hits = reopened.find("nested learning", k=5)
        assert hits
        assert any("Integration fact" in hit.record.title for hit in hits)
    finally:
        reopened.close()


def test_memvid_self_layer_write_verify_reopen_search(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, MemvidBackend)
    try:
        assert (tmp_path / "self.mv2").exists()
        memory.put(
            MemoryRecord(
                layer=MemoryLayer.SELF,
                kind=MemoryKind.FACT,
                title="Soul identity",
                content="Kestrel's Soul layer stores validated self-model records.",
                confidence=0.9,
                metadata={"self_schema": "identity_summary", "validation_status": "integration_test"},
            )
        )
        memory.seal_all()
        assert memory.verify_all()[MemoryLayer.SELF] is True
    finally:
        memory.close_all()

    reopened = LayeredMemorySystem.from_backend_factory(tmp_path, MemvidBackend)
    try:
        hits = reopened.retrieve(
            RetrievalQuery(query="Soul layer self-model", layers=(MemoryLayer.SELF,), k_per_layer=5)
        )
        assert any("Soul identity" in hit.record.title for hit in hits)
    finally:
        reopened.close_all()


def test_memvid_backend_persists_cognition_failure_and_lesson_records(tmp_path: Path) -> None:
    failure_execution = ToolExecution(
        call=ToolCall(name="test.run", arguments={"command": ["pytest", "-q"]}, id="failed_validation"),
        success=False,
        content="AssertionError: expected fixed",
        error="test_failed",
    )
    failure = FailureEpisode.from_tool_failure(
        run_id="run_cognition",
        execution=failure_execution,
        category="test_failure",
        diagnosis="Test failure playbook",
        attempted_strategy="Run the whole suite.",
    )
    validation = ToolExecution(
        call=ToolCall(name="test.run", arguments={"command": ["pytest", "tests/test_one.py", "-q"]}, id="focused_validation"),
        success=True,
        content="1 passed",
    )
    lesson = LessonCard.from_resolution(
        failure=failure,
        validation=validation,
        strategy=StrategyProposal(
            changed_strategy="Run the focused failing test before expanding validation.",
            why_different="The retry target is narrower.",
            expected_signal="Focused test passes.",
            fallback_if_fails="Inspect the assertion.",
        ),
    )

    episodic = MemvidBackend(path=tmp_path / "episodic.mv2", layer=MemoryLayer.EPISODIC)
    procedural = MemvidBackend(path=tmp_path / "procedural.mv2", layer=MemoryLayer.PROCEDURAL)
    episodic.open()
    procedural.open()
    try:
        episodic.put(failure.to_memory_record())
        procedural.put(lesson.to_memory_record())
        episodic.seal()
        procedural.seal()
        assert episodic.verify()
        assert procedural.verify()
    finally:
        episodic.close()
        procedural.close()

    reopened_episodic = MemvidBackend(path=tmp_path / "episodic.mv2", layer=MemoryLayer.EPISODIC)
    reopened_procedural = MemvidBackend(path=tmp_path / "procedural.mv2", layer=MemoryLayer.PROCEDURAL)
    reopened_episodic.open()
    reopened_procedural.open()
    try:
        failure_hits = reopened_episodic.find("FailureEpisode test_failure", k=5)
        lesson_hits = reopened_procedural.find("LessonCard test_failure focused", k=5)
        assert any("FailureEpisode" in hit.record.title for hit in failure_hits), [
            (hit.record.title, hit.record.content[:160], hit.record.metadata)
            for hit in failure_hits
        ]
        assert any("LessonCard" in hit.record.title for hit in lesson_hits), [
            (hit.record.title, hit.record.content[:160], hit.record.metadata)
            for hit in lesson_hits
        ]
    finally:
        reopened_episodic.close()
        reopened_procedural.close()


def test_memvid_backend_exact_record_index_survives_reopen_for_tombstones(tmp_path: Path) -> None:
    path = tmp_path / "semantic.mv2"
    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    try:
        backend.put(
            MemoryRecord(
                id="durable-fact",
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.FACT,
                title="Durable exact fact",
                content="Durable exact records survive Memvid backend reopen.",
                confidence=0.92,
                metadata={"frame_id": "durable-frame", "validation_status": "validated"},
            )
        )
        backend.seal()
    finally:
        backend.close()

    reopened = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    reopened.open()
    try:
        assert reopened.get_record("durable-fact") is not None
        assert reopened.get_record("durable-frame") is not None
        assert any(record.id == "durable-fact" for record in reopened.iter_records())
        reopened.tombstone("durable-fact", reason="integration_superseded", superseded_by="durable-fact-2")
        reopened.seal()
    finally:
        reopened.close()

    final = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    final.open()
    try:
        assert final.get_record("durable-fact", include_inactive=False) is None
        inactive = final.get_record("durable-fact")
        assert inactive is not None
        assert inactive.metadata["active"] is False
        assert inactive.metadata["tombstone_reason"] == "integration_superseded"
        assert {record.id for record in final.iter_records(include_inactive=True)} >= {
            "durable-fact",
            "tombstone_durable-fact",
        }
    finally:
        final.close()


def test_memvid_backup_restores_and_verifies_when_live_directory_is_missing(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    seed = LayeredMemorySystem.from_backend_factory(memory_dir, MemvidBackend)
    seed.close_all()
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=tmp_path / "backups")
    manifest = manager.create()
    shutil.rmtree(memory_dir)

    def verify_staging(path: Path) -> None:
        staged = LayeredMemorySystem.from_backend_factory(path, MemvidBackend)
        try:
            assert all(staged.verify_all().values())
        finally:
            staged.close_all()

    restored = manager.restore(manifest["backup_id"], verify_staging=verify_staging)

    assert restored["safety_backup_id"] is None
    reopened = LayeredMemorySystem.from_backend_factory(memory_dir, MemvidBackend)
    try:
        assert all(reopened.verify_all().values())
    finally:
        reopened.close_all()
