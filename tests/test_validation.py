from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryLayer, MemoryRecord
from nested_memvid_agent.validation import GoldenQuestion, RetrievalValidator


def test_retrieval_validator_passes_expected_terms(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path,
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )
    memory.put(
        MemoryRecord(
            title="Memvid model",
            content="Memvid stores nested agent layers in portable .mv2 files.",
            layer=MemoryLayer.SEMANTIC,
            confidence=0.9,
        )
    )
    results = RetrievalValidator(memory).run(
        [GoldenQuestion(name="mv2", query="portable memory files", expected_terms=(".mv2",))]
    )
    assert results[0].passed
