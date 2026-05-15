from __future__ import annotations

import os
from pathlib import Path

import pytest

from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord

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
