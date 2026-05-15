from __future__ import annotations

import pytest

from nested_memvid_agent.models import MemoryLayer, MemoryRecord


def test_memory_record_requires_content() -> None:
    with pytest.raises(ValueError):
        MemoryRecord(title="x", content="", layer=MemoryLayer.WORKING)


def test_memory_record_hash_is_stable_for_same_payload() -> None:
    a = MemoryRecord(title="T", content="hello", layer=MemoryLayer.SEMANTIC)
    b = MemoryRecord(title="T", content="hello", layer=MemoryLayer.SEMANTIC)
    assert a.content_hash == b.content_hash


def test_confidence_bounds() -> None:
    with pytest.raises(ValueError):
        MemoryRecord(title="x", content="y", layer=MemoryLayer.WORKING, confidence=1.1)
