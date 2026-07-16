from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from threading import Thread
from time import sleep
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


def test_memvid_backend_verify_closes_live_handle_before_deep_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "semantic.mv2"
    path.write_bytes(b"existing")
    instances: list[FakeMemForVerify] = []

    class FakeMemForVerify:
        def __init__(self) -> None:
            self.closed = False
            instances.append(self)

        def verify(self, path_arg: str, *, deep: bool) -> dict[str, object]:
            assert path_arg == str(path)
            assert deep is True
            if not self.closed:
                raise RuntimeError("exclusive access unavailable")
            return {"overall_status": "passed"}

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=lambda *args, **kwargs: FakeMemForVerify(),
            use=lambda *args, **kwargs: FakeMemForVerify(),
        ),
    )

    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()

    assert backend.verify() is True
    assert len(instances) == 2
    assert instances[0].closed is True
    assert instances[1].closed is False

    backend.close()


def test_memvid_backend_releases_all_locks_when_verify_reopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "semantic.mv2"
    path.write_bytes(b"existing")
    use_calls = 0

    class FakeMem:
        def verify(self, *args: object, **kwargs: object) -> dict[str, str]:
            del args, kwargs
            return {"overall_status": "passed"}

        def close(self) -> None:
            return None

    def fake_use(*args: object, **kwargs: object) -> FakeMem:
        nonlocal use_calls
        del args, kwargs
        use_calls += 1
        if use_calls == 2:
            raise RuntimeError("reopen failed")
        return FakeMem()

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=lambda *args, **kwargs: FakeMem(), use=fake_use),
    )
    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()

    with pytest.raises(RuntimeError, match="reopen failed"):
        backend.verify()

    replacement = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    replacement.open()
    replacement.close()
    assert use_calls == 3


def test_memvid_backend_serializes_same_path_open_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "semantic.mv2"
    path.write_bytes(b"existing")
    opened: list[FakeMemForLock] = []

    class FakeMemForLock:
        def close(self) -> None:
            return None

    def fake_use(*args: object, **kwargs: object) -> FakeMemForLock:
        del args, kwargs
        mem = FakeMemForLock()
        opened.append(mem)
        return mem

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=lambda *args, **kwargs: FakeMemForLock(), use=fake_use),
    )

    first = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    second = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    first.open()

    thread = Thread(target=second.open)
    thread.start()
    sleep(0.05)

    assert thread.is_alive()
    assert len(opened) == 1

    first.close()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(opened) == 2

    second.close()


def test_memvid_backend_rejects_conflicting_cross_process_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "semantic.mv2"
    path.write_bytes(b"existing")
    lock_path = tmp_path / ".semantic.mv2.kestrel.lock"
    script = """
import sys
from pathlib import Path
from nested_memvid_agent.file_lock import lock_exclusive, unlock

path = Path(sys.argv[1])
path.touch(mode=0o600, exist_ok=True)
with path.open("a+") as handle:
    lock_exclusive(handle)
    print("ready", flush=True)
    sys.stdin.read(1)
    unlock(handle)
"""
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and inline test script
        [sys.executable, "-c", script, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=lambda *args, **kwargs: object(),
            use=lambda *args, **kwargs: SimpleNamespace(close=lambda: None),
        ),
    )
    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    try:
        with pytest.raises(RuntimeError, match="conflicting write access"):
            backend.open()
    finally:
        if process.stdin is not None:
            process.stdin.write("x")
            process.stdin.flush()
        process.wait(timeout=5)

    assert process.returncode == 0


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


def test_memvid_backend_falls_back_to_exact_index_when_lex_index_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LexIndexDisabledError(Exception):
        pass

    class FakeMem:
        def put(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            return "record_1"

        def find(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise LexIndexDisabledError("MV004: Lexical index is not enabled")

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            LexIndexDisabledError=LexIndexDisabledError,
            create=lambda *args, **kwargs: FakeMem(),
            use=lambda *args, **kwargs: FakeMem(),
        ),
    )
    backend = MemvidBackend(path=tmp_path / "semantic.mv2", layer=MemoryLayer.SEMANTIC)
    backend.open()
    backend.put(
        MemoryRecord(
            id="fact-1",
            title="Durable fact",
            content="Telegram turns should survive missing lexical index by reading exact records.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.91,
        )
    )

    hits = backend.find("Telegram lexical index", k=3)

    assert hits
    assert hits[0].record.id == "fact-1"
    assert hits[0].source_backend == "memvid_exact_fallback"


def test_memvid_backend_persists_exact_records_and_tombstones_across_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "semantic.mv2"

    class FakeMem:
        def put(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            return "sdk_record"

        def find(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            return {
                "hits": [
                    {
                        "id": "fact-1",
                        "title": "Durable fact",
                        "text": "Durable exact-record index survives process restart.",
                        "score": 0.9,
                        "metadata": {"id": "fact-1", "kind": MemoryKind.FACT.value},
                    }
                ]
            }

        def close(self) -> None:
            return None

    def fake_create(filename: str, **kwargs: object) -> FakeMem:
        del kwargs
        Path(filename).write_bytes(b"fake mv2")
        return FakeMem()

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=fake_create, use=lambda *args, **kwargs: FakeMem()),
    )

    first = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    first.open()
    first.put(
        MemoryRecord(
            id="fact-1",
            title="Durable fact",
            content="Durable exact-record index survives process restart.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.91,
            metadata={"frame_id": "frame-fact-1", "validation_status": "validated"},
        )
    )
    first.tombstone("fact-1", reason="superseded", superseded_by="fact-2")
    first.close()

    reopened = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    reopened.open()
    try:
        assert reopened.get_record("fact-1", include_inactive=False) is None
        inactive = reopened.get_record("fact-1")
        assert inactive is not None
        assert inactive.metadata["active"] is False
        assert inactive.metadata["superseded_by"] == "fact-2"
        assert reopened.get_record("frame-fact-1") == inactive
        assert [record.id for record in reopened.iter_records()] == ["tombstone_fact-1"]
        assert {record.id for record in reopened.iter_records(include_inactive=True)} == {
            "fact-1",
            "tombstone_fact-1",
        }
        assert reopened.find("Durable fact", include_inactive=False) == []
        inactive_hits = reopened.find("Durable fact", include_inactive=True)
        assert inactive_hits
        assert inactive_hits[0].record.metadata["tombstone_reason"] == "superseded"
    finally:
        reopened.close()


def test_memvid_backend_fails_closed_at_layer_capacity(tmp_path: Path) -> None:
    path = tmp_path / "working.mv2"
    path.write_bytes(b"1234567890")
    backend = MemvidBackend(path=path, layer=MemoryLayer.WORKING, max_file_bytes=10)
    backend.mem = object()

    with pytest.raises(RuntimeError, match="capacity exceeded"):
        backend.put(
            MemoryRecord(
                title="x",
                content="y",
                layer=MemoryLayer.WORKING,
                kind=MemoryKind.OBSERVATION,
            )
        )
