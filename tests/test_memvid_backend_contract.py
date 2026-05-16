from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord


def test_backend_contract_put_find_verify(tmp_path: Path) -> None:
    backend = InMemoryBackend(path=tmp_path / "working.mv2", layer=MemoryLayer.WORKING)
    backend.open()
    record_id = backend.put(
        MemoryRecord(
            title="Contract test",
            content="The backend must retrieve auth profile memories.",
            layer=MemoryLayer.WORKING,
            confidence=0.5,
        )
    )
    hits = backend.find("auth profile", k=3)
    assert record_id
    assert hits
    assert backend.verify()


def test_memvid_backend_uses_existing_file_without_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "semantic.mv2"
    path.write_bytes(b"existing")
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_create(filename: str, **kwargs: Any) -> object:
        calls.append(("create", {"filename": filename, **kwargs}))
        return object()

    def fake_use(kind: str, filename: str, **kwargs: Any) -> object:
        calls.append(("use", {"kind": kind, "filename": filename, **kwargs}))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=fake_create, use=fake_use),
    )

    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC, read_only=True)
    backend.open()
    backend.close()

    assert [call[0] for call in calls] == ["use"]
    assert calls[0][1]["read_only"] is True


def test_memvid_backend_missing_read_only_file_fails_without_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=lambda *args, **kwargs: calls.append("create"),
            use=lambda *args, **kwargs: calls.append("use"),
        ),
    )

    backend = MemvidBackend(path=tmp_path / "missing.mv2", layer=MemoryLayer.SEMANTIC, read_only=True)

    with pytest.raises(FileNotFoundError):
        backend.open()
    assert calls == []


def test_memvid_backend_wraps_corrupt_existing_file_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "corrupt.mv2"
    path.write_text("not really mv2", encoding="utf-8")

    def fake_use(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ValueError("bad mv2")

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=lambda *args, **kwargs: object(), use=fake_use),
    )

    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)

    with pytest.raises(RuntimeError, match="Failed to open existing Memvid memory"):
        backend.open()


def test_memvid_backend_normalizes_find_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMem:
        def put(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            return "record_1"

        def find(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            return {
                "hits": [
                    {
                        "id": "hit_1",
                        "title": "Hit fact",
                        "text": "Normalized nested memory fact.",
                        "score": 0.8,
                        "metadata": {"kind": MemoryKind.FACT.value, "confidence": 0.9},
                    }
                ]
            }

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=lambda *args, **kwargs: FakeMem(), use=lambda *args, **kwargs: FakeMem()),
    )
    backend = MemvidBackend(path=tmp_path / "semantic.mv2", layer=MemoryLayer.SEMANTIC)
    backend.open()

    hits = backend.find("fact")

    assert len(hits) == 1
    assert hits[0].record.kind == MemoryKind.FACT
    assert hits[0].record.title == "Hit fact"
