from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryLayer, MemoryRecord, RetrievalQuery


def test_layer_write_threshold_blocks_low_confidence_semantic(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)
    with pytest.raises(ValueError):
        memory.put(
            MemoryRecord(
                title="Weak fact",
                content="Maybe the repo uses Kimi.",
                layer=MemoryLayer.SEMANTIC,
                confidence=0.2,
            )
        )


def test_retrieve_across_layers(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)
    memory.put(
        MemoryRecord(
            title="Working auth note",
            content="Kimi auth failed during startup.",
            layer=MemoryLayer.WORKING,
            confidence=0.3,
        )
    )
    memory.put(
        MemoryRecord(
            title="Semantic auth note",
            content="Provider-specific auth profiles should be checked before global variables.",
            layer=MemoryLayer.SEMANTIC,
            confidence=0.8,
        )
    )
    hits = memory.retrieve(RetrievalQuery(query="auth profiles"))
    assert {hit.record.layer for hit in hits} == {MemoryLayer.WORKING, MemoryLayer.SEMANTIC}
