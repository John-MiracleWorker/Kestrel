from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.retention import RetentionCompactor


def test_retention_compactor_dry_run_skips_stable_layers(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)
    memory.put(
        MemoryRecord(
            id="stable-fact",
            title="Stable fact",
            content="Stable semantic memory should not be TTL-compacted.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.86,
            created_at=datetime.now(UTC) - timedelta(days=800),
        )
    )

    report = RetentionCompactor(memory).compact_layer(MemoryLayer.SEMANTIC)

    assert report["skipped"] is True
    assert report["reason"] == "stable_layer"
    assert memory.retrieve(RetrievalQuery(query="TTL-compacted", layers=(MemoryLayer.SEMANTIC,)))


def test_retention_compactor_apply_summarizes_and_tombstones_working_records(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)
    memory.put(
        MemoryRecord(
            id="old-working-1",
            title="Old working item",
            content="Old working scratch detail about the provider switch.",
            layer=MemoryLayer.WORKING,
            kind=MemoryKind.OBSERVATION,
            confidence=0.5,
            created_at=datetime.now(UTC) - timedelta(days=30),
        )
    )

    report = RetentionCompactor(memory).compact_layer(MemoryLayer.WORKING, dry_run=False)

    assert report["dry_run"] is False
    assert report["tombstoned_ids"] == ["old-working-1"]
    assert not memory.retrieve(RetrievalQuery(query="provider switch", layers=(MemoryLayer.WORKING,)))
    audit = memory.retrieve(RetrievalQuery(query="provider switch", layers=(MemoryLayer.WORKING,), include_inactive=True))
    assert audit
    summaries = memory.retrieve(RetrievalQuery(query="provider switch", layers=(MemoryLayer.EPISODIC,)))
    assert summaries
    assert summaries[0].record.metadata["frame_type"] == "session_summary"
