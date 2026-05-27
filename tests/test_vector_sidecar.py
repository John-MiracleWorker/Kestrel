from __future__ import annotations

from pathlib import Path

import numpy as np

from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.vector_sidecar import VectorSidecar


class ConceptEmbedder:
    model_name = "concept-test"

    def embed(self, text: str) -> np.ndarray:
        concepts = {"import_path": 0, "auth": 1, "network": 2, "retry": 3}
        synonyms = {
            "pythonpath": "import_path",
            "module": "import_path",
            "sys": "import_path",
            "credential": "auth",
            "credentials": "auth",
            "token": "auth",
            "fetch": "network",
            "http": "network",
            "rerun": "retry",
            "retry": "retry",
        }
        vector = np.zeros(len(concepts), dtype=np.float32)
        for raw in text.lower().replace(".", " ").replace(":", " ").split():
            concept = synonyms.get(raw.strip())
            if concept in concepts:
                vector[concepts[concept]] = 1.0
        return vector


def test_vector_sidecar_indexes_records_without_storing_raw_memory_text(tmp_path: Path) -> None:
    index_path = tmp_path / "semantic.mv2.vector.sqlite"
    sidecar = VectorSidecar(
        path=index_path,
        layer=MemoryLayer.SEMANTIC,
        embedder=ConceptEmbedder(),
        mv2_path=tmp_path / "semantic.mv2",
    )
    record = MemoryRecord(
        id="pythonpath-fix",
        title="Python path import fix",
        content="Set PYTHONPATH before pytest invocations.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.9,
    )

    sidecar.open()
    sidecar.upsert(record)

    raw_database = index_path.read_bytes()
    assert b"Set PYTHONPATH before pytest invocations" not in raw_database

    hits = sidecar.search("module discovery needs sys route", k=3)

    assert hits[0].record_id == "pythonpath-fix"
    assert hits[0].score > 0.5
    status = sidecar.status(records=(record,))
    assert status.enabled is True
    assert status.indexed_count == 1
    assert status.stale_count == 0


def test_vector_sidecar_reports_stale_records_and_hides_tombstones(tmp_path: Path) -> None:
    sidecar = VectorSidecar(
        path=tmp_path / "procedural.mv2.vector.sqlite",
        layer=MemoryLayer.PROCEDURAL,
        embedder=ConceptEmbedder(),
        mv2_path=tmp_path / "procedural.mv2",
    )
    original = MemoryRecord(
        id="auth-refresh",
        title="Auth retry",
        content="Refresh token credentials before retry.",
        layer=MemoryLayer.PROCEDURAL,
        kind=MemoryKind.PROCEDURE,
        confidence=0.9,
    )
    changed = MemoryRecord(
        id="auth-refresh",
        title="Auth retry",
        content="Renew credential material before rerun.",
        layer=MemoryLayer.PROCEDURAL,
        kind=MemoryKind.PROCEDURE,
        confidence=0.9,
    )

    sidecar.open()
    sidecar.upsert(original)

    assert sidecar.status(records=(changed,)).stale_count == 1

    sidecar.rebuild((changed,))
    assert sidecar.status(records=(changed,)).stale_count == 0
    assert sidecar.search("token credentials", k=3)[0].record_id == "auth-refresh"

    sidecar.tombstone("auth-refresh")

    assert sidecar.search("token credentials", k=3) == []
    assert sidecar.search("token credentials", k=3, include_inactive=True)[0].record_id == "auth-refresh"
