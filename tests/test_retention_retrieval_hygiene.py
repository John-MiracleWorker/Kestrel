from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent, _tool_memory_content
from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayeredMemorySystem
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.models import (
    MemoryHit,
    MemoryKind,
    MemoryLayer,
    MemoryRecord,
    RetrievalQuery,
)
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.retention import RetentionCompactor
from nested_memvid_agent.runtime_models import LLMResponse
from nested_memvid_agent.tools.builtin import build_default_tools


class RecordingInMemoryBackend(InMemoryBackend):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requested_ks: list[int] = []

    def find(
        self,
        query: str,
        k: int = 8,
        mode: str = "auto",
        min_relevancy: float = 0.0,
        *,
        include_inactive: bool = False,
    ) -> list[MemoryHit]:
        self.requested_ks.append(k)
        return super().find(
            query=query,
            k=k,
            mode=mode,
            min_relevancy=min_relevancy,
            include_inactive=include_inactive,
        )


def test_retrieval_tool_output_is_auditable_without_reentering_normal_retrieval(
    tmp_path: Path,
) -> None:
    memory = build_memory_system(
        "memory",
        tmp_path / "memory",
        enforce_stable_write_integrity=False,
    )
    memory.put(
        MemoryRecord(
            id="source_fact",
            title="Lookup key fact",
            content="RETRIEVED_PAYLOAD_7f3c is the source fact, not a runtime artifact.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider([LLMResponse(content="unused")]),
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
            ),
        )
    )

    result = agent.chat("/search lookup key", session_id="retrieval-loop")

    assert "RETRIEVED_PAYLOAD_7f3c" in result.assistant_message
    working_records = list(memory.iter_records(MemoryLayer.WORKING))
    tool_record = next(
        record for record in working_records if record.title == "Tool result: memory.search"
    )
    assistant_record = next(
        record for record in working_records if record.title == "Assistant message"
    )
    summary_record = next(
        record
        for record in memory.iter_records(MemoryLayer.EPISODIC)
        if record.title == "Conversation turn summary"
    )
    assert tool_record.metadata["frame_type"] == "trace_stub"
    assert tool_record.metadata["retrieval_artifact"] is True
    assert "RETRIEVED_PAYLOAD_7f3c" not in tool_record.content
    assert assistant_record.metadata["retrieval_artifact"] is True
    assert "RETRIEVED_PAYLOAD_7f3c" not in summary_record.content
    assert summary_record.metadata["retrieval_source_tools"] == ["memory.search"]

    normal_hits = memory.retrieve(
        RetrievalQuery(query="RETRIEVED_PAYLOAD_7f3c", k_per_layer=8)
    )
    assert {hit.record.id for hit in normal_hits} == {"source_fact"}
    audit_hits = memory.retrieve(
        RetrievalQuery(
            query="RETRIEVED_PAYLOAD_7f3c",
            k_per_layer=8,
            include_retrieval_artifacts=True,
        )
    )
    assert assistant_record.id in {hit.record.id for hit in audit_hits}
    packed = ContextPacker(memory).pack(
        ContextPackRequest(
            objective="Show exact RETRIEVED_PAYLOAD_7f3c evidence",
            query="RETRIEVED_PAYLOAD_7f3c",
            expand_raw=True,
        )
    )
    assert assistant_record.id not in packed.prompt
    assert tool_record.id not in packed.prompt


def test_normal_retrieval_pages_past_more_than_64_audit_artifacts(
    tmp_path: Path,
) -> None:
    specs = {MemoryLayer.SEMANTIC: DEFAULT_LAYER_SPECS[MemoryLayer.SEMANTIC]}
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        RecordingInMemoryBackend,
        specs=specs,
        enforce_stable_write_integrity=False,
    )
    sentinel = "ARTIFACT_STARVATION_915d"
    for index in range(96):
        memory.put(
            MemoryRecord(
                id=f"artifact-{index:03d}",
                title="Ranked retrieval candidate",
                content=f"{sentinel} equal ranked candidate {index:03d}",
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.EVENT,
                confidence=0.9,
                metadata={"retrieval_artifact": True},
            )
        )
    memory.put(
        MemoryRecord(
            id="eligible-fact",
            title="Ranked retrieval candidate",
            content=f"{sentinel} equal ranked candidate 096",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )

    hits = memory.retrieve(
        RetrievalQuery(
            query=sentinel,
            layers=(MemoryLayer.SEMANTIC,),
            k_per_layer=1,
        )
    )

    assert [hit.record.id for hit in hits] == ["eligible-fact"]
    backend = memory.backends[MemoryLayer.SEMANTIC]
    assert isinstance(backend, RecordingInMemoryBackend)
    assert backend.requested_ks == [65, 129]

    audit_hits = memory.retrieve(
        RetrievalQuery(
            query=sentinel,
            layers=(MemoryLayer.SEMANTIC,),
            k_per_layer=1,
            include_inactive=True,
            include_retrieval_artifacts=True,
        )
    )
    assert audit_hits[0].record.id == "artifact-000"


def test_memvid_retrieval_uses_native_cursor_to_page_past_audit_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMem:
        def __init__(self) -> None:
            self.items: list[dict[str, object]] = []
            self.find_calls: list[tuple[int, str | None]] = []

        def put(self, *args: object, **kwargs: object) -> str:
            uri = str(kwargs["uri"])
            self.items.append(
                {
                    "frame_id": len(self.items) + 1,
                    "uri": uri,
                    "title": str(args[0]),
                    "text": str(kwargs["text"]),
                    "score": 1.0,
                }
            )
            return uri

        def find(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args
            page_size = int(kwargs["k"])
            raw_cursor = kwargs.get("cursor")
            cursor = None if raw_cursor is None else str(raw_cursor)
            self.find_calls.append((page_size, cursor))
            start = int(cursor or "0")
            end = min(start + page_size, len(self.items))
            return {
                "hits": self.items[start:end],
                "next_cursor": str(end) if end < len(self.items) else None,
            }

        def close(self) -> None:
            return None

    fake_mem = FakeMem()

    def fake_create(filename: str, **kwargs: Any) -> FakeMem:
        del kwargs
        Path(filename).write_bytes(b"fake mv2")
        return fake_mem

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=fake_create,
            use=lambda *args, **kwargs: fake_mem,
        ),
    )
    backend = MemvidBackend(
        path=tmp_path / "semantic.mv2",
        layer=MemoryLayer.SEMANTIC,
    )
    backend.open()
    specs = {MemoryLayer.SEMANTIC: DEFAULT_LAYER_SPECS[MemoryLayer.SEMANTIC]}
    memory = LayeredMemorySystem(
        backends={MemoryLayer.SEMANTIC: backend},
        specs=specs,
        enforce_stable_write_integrity=False,
    )
    sentinel = "MEMVID_ARTIFACT_STARVATION_04b7"
    try:
        for index in range(96):
            memory.put(
                MemoryRecord(
                    id=f"memvid-artifact-{index:03d}",
                    title="Memvid ranked retrieval candidate",
                    content=f"{sentinel} equal ranked candidate {index:03d}",
                    layer=MemoryLayer.SEMANTIC,
                    kind=MemoryKind.EVENT,
                    confidence=0.9,
                    metadata={"retrieval_artifact": True},
                )
            )
        memory.put(
            MemoryRecord(
                id="memvid-eligible-fact",
                title="Memvid ranked retrieval candidate",
                content=f"{sentinel} equal ranked candidate 096",
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.FACT,
                confidence=0.9,
            )
        )

        hits = memory.retrieve(
            RetrievalQuery(
                query=sentinel,
                layers=(MemoryLayer.SEMANTIC,),
                k_per_layer=1,
            )
        )

        assert [hit.record.id for hit in hits] == ["memvid-eligible-fact"]
        assert fake_mem.find_calls == [(64, None), (64, "64")]
    finally:
        backend.close()


def test_volatile_layer_default_retention_is_enforced_by_normal_retrieval(
    tmp_path: Path,
) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory", InMemoryBackend
    )
    before_put = datetime.now(UTC)
    defaulted_record = MemoryRecord(
        id="defaulted_working",
        title="Defaulted working detail",
        content="DEFAULTED_WORKING_4d2a receives the configured TTL.",
        layer=MemoryLayer.WORKING,
        kind=MemoryKind.OBSERVATION,
        confidence=0.6,
    )
    expired_record = MemoryRecord(
        id="expired_working",
        title="Expired working detail",
        content="EXPIRED_WORKING_4d2a must not be recalled after its layer TTL.",
        layer=MemoryLayer.WORKING,
        kind=MemoryKind.OBSERVATION,
        confidence=0.6,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    memory.put(defaulted_record)
    memory.put(expired_record)

    stored = memory.get_record(MemoryLayer.WORKING, defaulted_record.id)
    assert stored is not None
    assert stored.expires_at is not None
    expected_ttl = timedelta(
        days=memory.specs[MemoryLayer.WORKING].retention_days
    )
    assert before_put + expected_ttl <= stored.expires_at <= datetime.now(UTC) + expected_ttl
    assert not memory.retrieve(
        RetrievalQuery(
            query="EXPIRED_WORKING_4d2a",
            layers=(MemoryLayer.WORKING,),
        )
    )
    audit_hits = memory.retrieve(
        RetrievalQuery(
            query="EXPIRED_WORKING_4d2a",
            layers=(MemoryLayer.WORKING,),
            include_inactive=True,
        )
    )
    assert [hit.record.id for hit in audit_hits] == [expired_record.id]


def test_retention_compaction_excludes_generated_artifacts_and_bounds_work(
    tmp_path: Path,
) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory", InMemoryBackend
    )
    old = datetime.now(UTC) - timedelta(days=30)
    records = [
        MemoryRecord(
            id="authored_record",
            title="User-authored working record",
            content="AUTHORED_EVIDENCE_932b must survive in the compaction summary.",
            layer=MemoryLayer.WORKING,
            kind=MemoryKind.OBSERVATION,
            confidence=0.5,
            created_at=old,
        ),
        MemoryRecord(
            id="retrieval_artifact",
            title="Tool result: memory.search",
            content="COPIED_RETRIEVAL_PAYLOAD_c81e must not enter the summary.",
            layer=MemoryLayer.WORKING,
            kind=MemoryKind.EVENT,
            confidence=0.7,
            created_at=old + timedelta(seconds=1),
            metadata={"retrieval_artifact": True, "frame_type": "trace_stub"},
        ),
        MemoryRecord(
            id="prior_compaction",
            title="Prior compaction",
            content="NESTED_COMPACTION_PAYLOAD_a712 must not be summarized again.",
            layer=MemoryLayer.WORKING,
            kind=MemoryKind.SUMMARY,
            confidence=0.7,
            created_at=old + timedelta(seconds=2),
            metadata={"retention_compaction": True},
        ),
    ]
    for record in records:
        memory.put(record)

    report = RetentionCompactor(
        memory,
        max_candidates_per_run=2,
        max_summary_chars=256,
    ).compact_layer(MemoryLayer.WORKING, dry_run=False)

    assert report["candidate_count"] == 3
    assert report["processed_count"] == 2
    assert report["deferred_count"] == 1
    assert report["summarized_count"] == 1
    assert report["artifact_count"] == 1
    assert report["tombstoned_ids"] == ["authored_record", "retrieval_artifact"]
    summary = memory.get_record(MemoryLayer.EPISODIC, str(report["summary_record_id"]))
    assert summary is not None
    assert len(summary.content) <= 256
    assert "AUTHORED_EVIDENCE_932b" in summary.content
    assert "COPIED_RETRIEVAL_PAYLOAD_c81e" not in summary.content
    assert "NESTED_COMPACTION_PAYLOAD_a712" not in summary.content


def test_large_tool_memory_record_has_a_deterministic_size_bound() -> None:
    content = "x" * 100_000

    stored = _tool_memory_content(content)

    assert len(stored) <= 64_000
    assert "TRUNCATED_TOOL_OUTPUT" in stored
    assert "total_chars=100000" in stored
    assert "sha256=" in stored


def test_memvid_page_hit_recovers_exact_artifact_metadata_from_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMem:
        def put(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            return "sdk_record"

        def find(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            return {
                "hits": [
                    {
                        "frame_id": 5,
                        "uri": "mv2://working/event/runtime-artifact#page-2",
                        "title": "Assistant message (page 2/3)",
                        "text": "COPIED_PAGE_PAYLOAD_92f1",
                        "score": 0.9,
                    }
                ]
            }

        def close(self) -> None:
            return None

    def fake_create(filename: str, **kwargs: Any) -> FakeMem:
        del kwargs
        Path(filename).write_bytes(b"fake mv2")
        return FakeMem()

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=fake_create,
            use=lambda *args, **kwargs: FakeMem(),
        ),
    )
    backend = MemvidBackend(
        path=tmp_path / "working.mv2",
        layer=MemoryLayer.WORKING,
    )
    backend.open()
    backend.put(
        MemoryRecord(
            id="5",
            title="Stale SDK page record",
            content="A prior retrieval write-back used the numeric SDK page identifier.",
            layer=MemoryLayer.WORKING,
            kind=MemoryKind.EVENT,
            confidence=0.6,
        )
    )
    backend.put(
        MemoryRecord(
            id="runtime-artifact",
            title="Assistant message",
            content="COPIED_PAGE_PAYLOAD_92f1 exact transcript content",
            layer=MemoryLayer.WORKING,
            kind=MemoryKind.EVENT,
            confidence=0.6,
            metadata={"retrieval_artifact": True},
        )
    )

    hit = backend.find("COPIED_PAGE_PAYLOAD_92f1")[0]

    assert hit.record.id == "runtime-artifact"
    assert hit.record.metadata["retrieval_artifact"] is True
    assert hit.record.content == "COPIED_PAGE_PAYLOAD_92f1 exact transcript content"
    backend.close()
