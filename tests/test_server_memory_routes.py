from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.layers import load_layer_specs
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.server import create_app


class ConceptEmbedder:
    model_name = "concept-test"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(2, dtype=np.float32)
        synonyms = {
            "pythonpath": 0,
            "module": 0,
            "sys": 0,
            "credential": 1,
            "token": 1,
        }
        for raw in text.lower().replace(".", " ").split():
            idx = synonyms.get(raw.strip())
            if idx is not None:
                vector[idx] = 1.0
        return vector


def test_memory_search_route_accepts_mode_and_layers_report_vector_status(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    layer_config = tmp_path / "layers.json"
    layer_config.write_text(
        json.dumps(
            {
                "semantic": {
                    "search_mode": "hybrid",
                    "vector": {
                        "enabled": True,
                        "embedding_provider": "local",
                        "embedding_model": "concept-test",
                        "index_path": "semantic.mv2.vector.sqlite",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "nested_memvid_agent.layers.make_local_embedder",
        lambda model_name=None: ConceptEmbedder(),
    )
    memory_dir = tmp_path / "memory"
    memory = build_memory_system(
        "memory",
        memory_dir,
        specs=load_layer_specs(layer_config),
        enforce_stable_write_integrity=False,
    )
    memory.put(
        MemoryRecord(
            id="pythonpath-fix",
            title="Python path import fix",
            content="Set PYTHONPATH before pytest invocations.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )
    memory.close_all()
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=memory_dir,
        layer_config_path=layer_config,
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        workspace=tmp_path,
    )
    client = TestClient(create_app(config))

    lexical = client.get(
        "/api/memory/search",
        params={"query": "module discovery needs sys route", "layers": "semantic", "mode": "lex"},
    )
    hybrid = client.get(
        "/api/memory/search",
        params={"query": "module discovery needs sys route", "layers": "semantic", "mode": "hybrid"},
    )
    layers = client.get("/api/memory/layers")

    assert lexical.status_code == 200
    assert lexical.json() == []
    assert hybrid.status_code == 200
    assert hybrid.json()[0]["record_id"] == "pythonpath-fix"
    assert hybrid.json()[0]["source_backend"] == "vector_sidecar"
    semantic = next(row for row in layers.json() if row["layer"] == "semantic")
    assert semantic["vector"]["enabled"] is True
    assert semantic["vector"]["indexed_count"] == 1
