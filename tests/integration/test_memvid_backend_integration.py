from __future__ import annotations

import json
import os
import shutil
from hashlib import sha256
from pathlib import Path

import pytest

from nested_memvid_agent.backends.base import MemoryBackend
from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.backends.memvid_backend import MemvidBackend, _record_to_index_payload
from nested_memvid_agent.cognition import FailureEpisode, LessonCard
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.memory_backup import MemoryBackupManager
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.runtime_models import StrategyProposal, ToolCall, ToolExecution
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import MemoryExportTool

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MEMVID_INTEGRATION") != "1",
    reason="Set RUN_MEMVID_INTEGRATION=1 and install memvid-sdk to run Memvid integration tests.",
)


@pytest.mark.parametrize(
    "backend_factory",
    [InMemoryBackend, MemvidBackend],
    ids=["memory", "memvid"],
)
def test_memvid_full_memory_export_is_paginated_and_never_silently_empty(
    tmp_path: Path,
    backend_factory: type[MemoryBackend],
) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "export-memory",
        backend_factory,
    )
    try:
        for index in range(3):
            memory.put(
                MemoryRecord(
                    id=f"portable-record-{index}",
                    layer=MemoryLayer.EPISODIC,
                    kind=MemoryKind.EVENT,
                    title=f"Portable record {index}",
                    content=f"Memvid export payload {index}.",
                )
            )
        result = MemoryExportTool().run(
            {"layers": ["episodic"], "limit": 2},
            ToolContext(
                memory=memory,
                config=AgentConfig(),
                workspace=tmp_path,
            ),
        )

        assert result.success
        assert result.data["count"] == 2
        assert result.data["total"] == 3
        assert result.data["next_offset"] == 2
        assert result.data["truncated"] is True
        assert [row["id"] for row in result.data["records"]] == [
            "portable-record-0",
            "portable-record-1",
        ]
    finally:
        memory.close_all()


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
    # This test seeds the storage adapter below the promotion-policy boundary.
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path,
        MemvidBackend,
        enforce_stable_write_integrity=False,
    )
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


def test_memvid_rebuilds_more_than_one_timeline_page_without_resurrecting_tombstones(
    tmp_path: Path,
) -> None:
    path = tmp_path / "semantic.mv2"
    sidecar = path.with_suffix(f"{path.suffix}.records.json")
    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    try:
        for index in range(105):
            backend.put(
                MemoryRecord(
                    id=f"fact-{index:03d}",
                    layer=MemoryLayer.SEMANTIC,
                    kind=MemoryKind.FACT,
                    title=f"Canonical fact {index:03d}",
                    content=f"Exact canonical payload for record {index:03d}.",
                    confidence=0.9,
                )
            )
        backend.tombstone("fact-042", reason="integration_superseded", superseded_by="fact-104")
        backend.seal()
    finally:
        backend.close()

    sidecar.unlink()
    rebuilt = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    rebuilt.open()
    try:
        active_ids = {record.id for record in rebuilt.iter_records()}
        all_ids = {record.id for record in rebuilt.iter_records(include_inactive=True)}
        assert len(active_ids) == 105  # 104 facts plus the authoritative tombstone audit record.
        assert {f"fact-{index:03d}" for index in range(105)} <= all_ids
        assert "fact-042" not in active_ids
        assert "tombstone_fact-042" in active_ids
        assert rebuilt.get_record("fact-042", include_inactive=False) is None
        inactive = rebuilt.get_record("fact-042")
        assert inactive is not None
        assert inactive.metadata["tombstone_reason"] == "integration_superseded"
        assert sidecar.exists()
    finally:
        rebuilt.close()


def test_memvid_rebuilds_exact_256kib_record_from_one_chunked_logical_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "working.mv2"
    sidecar = path.with_suffix(f"{path.suffix}.records.json")
    content = "0123456789abcdef" * 16_384
    expected_digest = sha256(content.encode("utf-8")).hexdigest()
    backend = MemvidBackend(path=path, layer=MemoryLayer.WORKING)
    backend.open()
    try:
        backend.put(
            MemoryRecord(
                id="exact-256kib-record",
                layer=MemoryLayer.WORKING,
                kind=MemoryKind.OBSERVATION,
                title="Exact 256 KiB record",
                content=content,
                confidence=0.87,
                metadata={"validation_status": "live_chunk_replay"},
            )
        )
        assert backend.stats()["active_frame_count"] > 1
        assert len(backend.mem.timeline(limit=256, reverse=True)) == 1
        backend.seal()
    finally:
        backend.close()

    sidecar.unlink()
    rebuilt = MemvidBackend(path=path, layer=MemoryLayer.WORKING)
    rebuilt.open()
    try:
        restored = rebuilt.get_record("exact-256kib-record")
        assert restored is not None
        assert len(restored.content) == 262_144
        assert sha256(restored.content.encode("utf-8")).hexdigest() == expected_digest
        assert restored.metadata["validation_status"] == "live_chunk_replay"
        assert sidecar.exists()
    finally:
        rebuilt.close()


def test_memvid_replay_repairs_a_self_consistent_but_tampered_exact_cache(tmp_path: Path) -> None:
    path = tmp_path / "semantic.mv2"
    sidecar = path.with_suffix(f"{path.suffix}.records.json")
    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    try:
        backend.put(
            MemoryRecord(
                id="authoritative-fact",
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.FACT,
                title="Authoritative fact",
                content="The .mv2 envelope is the only durable source of this exact content.",
                confidence=0.95,
            )
        )
    finally:
        backend.close()

    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["records"][0]["content"] = "Tampered JSON cache content."
    payload["records"][0]["metadata"]["active"] = False
    payload["inactive_ids"].append("authoritative-fact")
    cache_state = {
        "layer": payload["layer"],
        "records": payload["records"],
        "inactive_ids": payload["inactive_ids"],
    }
    canonical_cache_state = json.dumps(
        cache_state,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    payload["cache_sha256"] = sha256(canonical_cache_state.encode("utf-8")).hexdigest()
    sidecar.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    reopened = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    reopened.open()
    try:
        restored = reopened.get_record("authoritative-fact")
        assert restored is not None
        assert restored.content == "The .mv2 envelope is the only durable source of this exact content."
        assert reopened.get_record("authoritative-fact", include_inactive=False) is not None
        repaired_payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert repaired_payload["records"][0]["content"] == restored.content
        assert "authoritative-fact" not in repaired_payload["inactive_ids"]
    finally:
        reopened.close()


def test_memvid_migrates_legacy_cache_into_mv2_then_survives_cache_deletion(
    tmp_path: Path,
) -> None:
    import memvid_sdk

    path = tmp_path / "semantic.mv2"
    sidecar = path.with_suffix(f"{path.suffix}.records.json")
    active = MemoryRecord(
        id="legacy-active",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        title="Legacy active fact",
        content="Legacy exact content must become authoritative inside Memvid v2.",
        confidence=0.91,
    )
    inactive = MemoryRecord(
        id="legacy-inactive",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        title="Legacy inactive fact",
        content="Legacy tombstone state must not resurrect after cache deletion.",
        confidence=0.88,
        metadata={"active": False, "tombstone_reason": "legacy_superseded"},
    )
    raw = memvid_sdk.create(str(path), enable_vec=False, enable_lex=True)
    try:
        for record in (active, inactive):
            raw.put(
                record.title,
                record.layer.value,
                record.to_metadata(),
                text=record.content,
                uri=f"mv2://semantic/fact/{record.id}",
                tags=[],
                labels=[record.kind.value],
                track=record.layer.value,
                kind=record.kind.value,
                enable_embedding=False,
            )
    finally:
        raw.close()
    sidecar.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mv2_path": str(path),
                "layer": MemoryLayer.SEMANTIC.value,
                "inactive_ids": [inactive.id],
                "records": [_record_to_index_payload(active), _record_to_index_payload(inactive)],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    migrated = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    migrated.open()
    try:
        assert migrated.get_record(active.id) is not None
        assert migrated.get_record(inactive.id, include_inactive=False) is None
        migrated_cache = json.loads(sidecar.read_text(encoding="utf-8"))
        assert migrated_cache["schema_version"] == 2
        assert migrated.stats()["frame_count"] == 4
    finally:
        migrated.close()

    sidecar.unlink()
    rebuilt = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    rebuilt.open()
    try:
        rebuilt_active = rebuilt.get_record(active.id)
        assert rebuilt_active is not None
        assert rebuilt_active.content == active.content
        assert rebuilt.get_record(inactive.id, include_inactive=False) is None
        rebuilt_inactive = rebuilt.get_record(inactive.id)
        assert rebuilt_inactive is not None
        assert rebuilt_inactive.metadata["tombstone_reason"] == "legacy_superseded"
    finally:
        rebuilt.close()


def test_memvid_legacy_container_without_cache_fails_closed_with_migration_guidance(
    tmp_path: Path,
) -> None:
    import memvid_sdk

    path = tmp_path / "semantic.mv2"
    raw = memvid_sdk.create(str(path), enable_vec=False, enable_lex=True)
    try:
        raw.put(
            "Legacy frame",
            MemoryLayer.SEMANTIC.value,
            {"id": "legacy-only", "kind": MemoryKind.FACT.value},
            text="This frame predates Kestrel's canonical envelope.",
            uri="mv2://semantic/fact/legacy-only",
            tags=[],
            labels=[MemoryKind.FACT.value],
            track=MemoryLayer.SEMANTIC.value,
            kind=MemoryKind.FACT.value,
            enable_embedding=False,
        )
    finally:
        raw.close()

    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC, read_only=True)
    with pytest.raises(RuntimeError, match="legacy exact-record cache is required for one-time migration"):
        backend.open()


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
