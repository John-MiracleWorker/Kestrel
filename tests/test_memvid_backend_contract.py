from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from threading import Lock, Thread
from time import sleep
from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.backends.memvid_backend import MemvidBackend, MemvidLockError
from nested_memvid_agent.layers import LayeredMemorySystem, MemoryCleanupIncompleteError
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord


def test_in_memory_backend_put_and_upsert_return_logical_record_ids(tmp_path: Path) -> None:
    backend = InMemoryBackend(path=tmp_path / "working.mv2", layer=MemoryLayer.WORKING)
    backend.open()
    record = MemoryRecord(
        id="logical-working-record",
        title="Contract test",
        content="The backend must retrieve auth profile memories.",
        layer=MemoryLayer.WORKING,
        confidence=0.5,
    )
    record_id = backend.put(record)
    hits = backend.find("auth profile", k=3)
    assert record_id == record.id
    resolved = backend.get_record(record_id)
    assert resolved is not None and resolved.id == record.id
    assert hits
    assert backend.verify()

    replacement = MemoryRecord(
        id=record.id,
        title=record.title,
        content="The logical record ID remains stable after an upsert.",
        layer=MemoryLayer.WORKING,
        confidence=0.6,
    )
    assert backend.upsert(replacement) == record.id
    resolved = backend.get_record(record.id)
    assert resolved is not None and resolved.content == replacement.content


def test_memvid_backend_put_and_upsert_return_logical_record_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMem:
        def __init__(self) -> None:
            self.frame_count = 0

        def put(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            physical_frame_id = str(self.frame_count)
            self.frame_count += 1
            return physical_frame_id

        def stats(self) -> dict[str, int]:
            return {
                "frame_count": self.frame_count,
                "active_frame_count": self.frame_count,
                "seq_no": self.frame_count,
                "size_bytes": self.frame_count,
            }

        def close(self) -> None:
            return None

    fake_mem = FakeMem()

    def fake_create(filename: str, **kwargs: object) -> FakeMem:
        del kwargs
        Path(filename).write_bytes(b"fake mv2")
        return fake_mem

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=fake_create, use=lambda *args, **kwargs: fake_mem),
    )
    backend = MemvidBackend(path=tmp_path / "working.mv2", layer=MemoryLayer.WORKING)
    backend.open()
    try:
        record = MemoryRecord(
            id="logical-memvid-record",
            title="Logical Memvid ID",
            content="A physical frame ID is not a MemoryRecord ID.",
            layer=MemoryLayer.WORKING,
        )
        assert backend.put(record) == record.id
        resolved = backend.get_record(record.id)
        assert resolved is not None and resolved.id == record.id

        replacement = MemoryRecord(
            id=record.id,
            title=record.title,
            content="The logical ID remains resolvable immediately after upsert.",
            layer=MemoryLayer.WORKING,
        )
        assert backend.upsert(replacement) == record.id
        resolved = backend.get_record(record.id)
        assert resolved is not None and resolved.content == replacement.content
    finally:
        backend.close()


def test_memvid_backend_uses_existing_file_without_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    backend = MemvidBackend(
        path=tmp_path / "missing.mv2", layer=MemoryLayer.SEMANTIC, read_only=True
    )

    with pytest.raises(FileNotFoundError):
        backend.open()
    assert calls == []


def test_memvid_backend_preserves_native_loader_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_import(name: str) -> object:
        del name
        raise ImportError("cannot allocate memory in static TLS block")

    monkeypatch.setattr("nested_memvid_agent.backends.memvid_backend.import_module", broken_import)
    backend = MemvidBackend(path=tmp_path / "semantic.mv2", layer=MemoryLayer.SEMANTIC)

    with pytest.raises(RuntimeError, match="cannot allocate memory in static TLS block"):
        backend.open()


def test_memvid_backend_only_reports_sdk_absent_for_top_level_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_dependency(name: str) -> object:
        del name
        raise ModuleNotFoundError("No module named 'native_helper'", name="native_helper")

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module", missing_dependency
    )
    backend = MemvidBackend(path=tmp_path / "semantic.mv2", layer=MemoryLayer.SEMANTIC)

    with pytest.raises(
        RuntimeError, match="dependency is missing: No module named 'native_helper'"
    ):
        backend.open()


def test_memvid_backend_wraps_corrupt_existing_file_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_memvid_backend_close_failure_retains_handle_and_exclusive_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "semantic.mv2"
    path.write_bytes(b"existing")
    allow_close = False

    class FakeMem:
        def close(self) -> None:
            if not allow_close:
                raise RuntimeError("injected SDK close failure")

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=lambda *args, **kwargs: FakeMem(),
            use=lambda *args, **kwargs: FakeMem(),
        ),
    )
    first = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    first.open()
    handle = first.mem

    with pytest.raises(RuntimeError, match="injected SDK close failure"):
        first.close()

    assert first.mem is handle
    second = MemvidBackend(
        path=path,
        layer=MemoryLayer.SEMANTIC,
        path_lock_blocking=False,
    )
    with pytest.raises(MemvidLockError, match="already open in this process"):
        second.open()

    allow_close = True
    first.close()
    assert first.mem is None
    second.open()
    second.close()


def test_memvid_failed_open_keeps_locks_when_partial_handle_cannot_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "semantic.mv2"
    path.write_bytes(b"existing")
    allow_close = False

    class FakeMem:
        def close(self) -> None:
            if not allow_close:
                raise RuntimeError("injected partial-handle close failure")

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=lambda *args, **kwargs: FakeMem(),
            use=lambda *args, **kwargs: FakeMem(),
        ),
    )
    first = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    monkeypatch.setattr(
        first,
        "_load_exact_index",
        lambda: (_ for _ in ()).throw(RuntimeError("injected index load failure")),
    )

    with pytest.raises(RuntimeError, match="partial-handle close failure"):
        first.open()
    assert first.mem is not None

    second = MemvidBackend(
        path=path,
        layer=MemoryLayer.SEMANTIC,
        path_lock_blocking=False,
    )
    with pytest.raises(MemvidLockError, match="already open in this process"):
        second.open()

    allow_close = True
    first.close()
    second.open()
    second.close()


def test_layered_memvid_construction_retains_failed_cleanup_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = tmp_path / "memory"
    allow_close = False

    class FakeMem:
        def close(self) -> None:
            if not allow_close:
                raise RuntimeError("injected layered cleanup failure")

    def fake_create(filename: str, **kwargs: object) -> FakeMem:
        del kwargs
        path = Path(filename)
        if path.name == "episodic.mv2":
            raise RuntimeError("injected second-layer open failure")
        path.write_bytes(b"fake mv2")
        return FakeMem()

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=fake_create,
            use=lambda *args, **kwargs: FakeMem(),
        ),
    )

    with pytest.raises(MemoryCleanupIncompleteError) as raised:
        LayeredMemorySystem.from_backend_factory(memory_dir, MemvidBackend)
    assert raised.value.phase == "memory_construction"
    assert raised.value.pending_resource_count == 1

    replacement = MemvidBackend(
        path=memory_dir / "working.mv2",
        layer=MemoryLayer.WORKING,
        path_lock_blocking=False,
    )
    with pytest.raises(MemvidLockError, match="already open in this process"):
        replacement.open()

    allow_close = True
    assert raised.value.retry_cleanup() is True
    assert raised.value.pending_resource_count == 0
    replacement.open()
    replacement.close()


def test_layered_memvid_failed_seal_retains_handles_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = tmp_path / "memory"
    allow_seal = False
    close_calls = 0

    class FakeMem:
        def __init__(self, path: Path) -> None:
            self.path = path

        def put(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            return "frame"

        def seal(self) -> None:
            if self.path.name == "working.mv2" and not allow_seal:
                raise RuntimeError("injected SDK seal failure")

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    def fake_create(filename: str, **kwargs: object) -> FakeMem:
        del kwargs
        path = Path(filename)
        path.write_bytes(b"fake mv2")
        return FakeMem(path)

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=fake_create,
            use=lambda _kind, filename, **kwargs: FakeMem(Path(filename)),
        ),
    )
    memory = LayeredMemorySystem.from_backend_factory(memory_dir, MemvidBackend)
    memory.put(
        MemoryRecord(
            id="dirty-working",
            title="Dirty working frame",
            content="This frame must be sealed before ownership is released.",
            layer=MemoryLayer.WORKING,
        )
    )

    with pytest.raises(RuntimeError, match="injected SDK seal failure"):
        memory.close_all()
    assert close_calls == 0

    replacement = MemvidBackend(
        path=memory_dir / "working.mv2",
        layer=MemoryLayer.WORKING,
        path_lock_blocking=False,
    )
    with pytest.raises(MemvidLockError, match="already open in this process"):
        replacement.open()

    allow_seal = True
    memory.close_all()
    assert close_calls == len(MemoryLayer)
    replacement.open()
    replacement.close()


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


def test_memvid_backend_normalizes_find_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def fake_create(filename: str, **kwargs: object) -> FakeMem:
        del kwargs
        Path(filename).write_bytes(b"fake mv2")
        return FakeMem()

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=fake_create, use=lambda *args, **kwargs: FakeMem()),
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

    def fake_create(filename: str, **kwargs: object) -> FakeMem:
        del kwargs
        Path(filename).write_bytes(b"fake mv2")
        return FakeMem()

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            LexIndexDisabledError=LexIndexDisabledError,
            create=fake_create,
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


def test_memvid_backend_serializes_shared_handle_operations_across_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMem:
        def __init__(self) -> None:
            self.guard = Lock()
            self.active_calls = 0
            self.max_active_calls = 0
            self.frame_count = 0

        def put(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            with self.guard:
                self.active_calls += 1
                self.max_active_calls = max(self.max_active_calls, self.active_calls)
            sleep(0.005)
            with self.guard:
                self.frame_count += 1
                stored_id = str(self.frame_count)
                self.active_calls -= 1
            return stored_id

        def stats(self) -> dict[str, int]:
            with self.guard:
                return {
                    "frame_count": self.frame_count,
                    "active_frame_count": self.frame_count,
                    "seq_no": 1,
                    "size_bytes": self.frame_count,
                }

        def close(self) -> None:
            return None

    fake_mem = FakeMem()

    def fake_create(filename: str, **kwargs: object) -> FakeMem:
        del kwargs
        Path(filename).write_bytes(b"fake mv2")
        return fake_mem

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=fake_create, use=lambda *args, **kwargs: fake_mem),
    )
    backend = MemvidBackend(path=tmp_path / "semantic.mv2", layer=MemoryLayer.SEMANTIC)
    backend.open()
    failures: list[BaseException] = []
    stored_ids: list[str] = []

    def write(index: int) -> None:
        try:
            stored_ids.append(
                backend.put(
                    MemoryRecord(
                        id=f"concurrent-{index}",
                        title=f"Concurrent fact {index}",
                        content=f"Thread-safe shared Memvid operation {index}.",
                        layer=MemoryLayer.SEMANTIC,
                        kind=MemoryKind.FACT,
                    )
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted after join
            failures.append(exc)

    threads = [Thread(target=write, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert fake_mem.max_active_calls == 1
    assert set(stored_ids) == {f"concurrent-{index}" for index in range(12)}
    assert {record.id for record in backend.iter_records()} == {
        f"concurrent-{index}" for index in range(12)
    }
    backend.close()


def test_memvid_backend_replays_one_logical_commit_backed_by_many_physical_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "working.mv2"
    sidecar = path.with_suffix(f"{path.suffix}.records.json")

    class FakeChunkedMem:
        def __init__(self) -> None:
            self.frame_count = 0
            self.uri = ""
            self.metadata: dict[str, object] = {}

        def put(
            self,
            title: str,
            source: str,
            metadata: dict[str, object],
            **kwargs: object,
        ) -> str:
            del title, source
            self.frame_count = 220
            self.uri = str(kwargs["uri"])
            self.metadata = metadata
            return "0"

        def stats(self) -> dict[str, int]:
            return {
                "frame_count": self.frame_count,
                "active_frame_count": self.frame_count,
                "seq_no": 1,
                "size_bytes": self.frame_count,
            }

        def timeline(self, **kwargs: object) -> list[dict[str, object]]:
            del kwargs
            if self.frame_count == 0:
                return []
            return [{"frame_id": 0, "uri": self.uri}]

        def frame(self, uri: str) -> dict[str, object]:
            assert uri == self.uri
            return {"id": 0, "uri": uri, "extra_metadata": self.metadata}

        def close(self) -> None:
            return None

    fake_mem = FakeChunkedMem()

    def fake_create(filename: str, **kwargs: object) -> FakeChunkedMem:
        del kwargs
        Path(filename).write_bytes(b"fake mv2")
        return fake_mem

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=fake_create, use=lambda *args, **kwargs: fake_mem),
    )
    first = MemvidBackend(path=path, layer=MemoryLayer.WORKING)
    first.open()
    first.put(
        MemoryRecord(
            id="chunked-record",
            title="Chunked logical record",
            content="One exact logical record may span many physical Memvid frames.",
            layer=MemoryLayer.WORKING,
        )
    )
    first.close()
    sidecar.unlink()

    reopened = MemvidBackend(path=path, layer=MemoryLayer.WORKING)
    reopened.open()
    try:
        restored = reopened.get_record("chunked-record")
        assert restored is not None
        assert restored.content == "One exact logical record may span many physical Memvid frames."
        assert sidecar.exists()
    finally:
        reopened.close()


def test_memvid_backend_pages_logical_timeline_until_origin_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "working.mv2"
    sidecar = path.with_suffix(f"{path.suffix}.records.json")

    class FakePagedMem:
        def __init__(self) -> None:
            self.frame_count = 0
            self.uri = ""
            self.metadata: dict[str, object] = {}
            self.cursors: list[object] = []

        def put(
            self,
            title: str,
            source: str,
            metadata: dict[str, object],
            **kwargs: object,
        ) -> str:
            del title, source
            self.frame_count = 300
            self.uri = str(kwargs["uri"])
            self.metadata = metadata
            return "0"

        def stats(self) -> dict[str, int]:
            return {
                "frame_count": self.frame_count,
                "active_frame_count": self.frame_count,
                "seq_no": 1,
                "size_bytes": self.frame_count,
            }

        def timeline(self, **kwargs: object) -> list[dict[str, object]]:
            self.cursors.append(kwargs.get("as_of_frame"))
            if self.frame_count == 0:
                return []
            upper = int(kwargs.get("as_of_frame", self.frame_count - 1))
            limit = int(kwargs.get("limit", 100))
            return [
                {"frame_id": frame_id, "uri": f"{self.uri}&logical_frame={frame_id}"}
                for frame_id in range(upper, -1, -1)[:limit]
            ]

        def frame(self, uri: str) -> dict[str, object]:
            logical_frame = int(uri.rsplit("=", maxsplit=1)[-1])
            return {
                "uri": uri,
                "extra_metadata": self.metadata if logical_frame == 0 else {},
            }

        def close(self) -> None:
            return None

    fake_mem = FakePagedMem()

    def fake_create(filename: str, **kwargs: object) -> FakePagedMem:
        del kwargs
        Path(filename).write_bytes(b"fake mv2")
        return fake_mem

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(create=fake_create, use=lambda *args, **kwargs: fake_mem),
    )
    first = MemvidBackend(path=path, layer=MemoryLayer.WORKING)
    first.open()
    first.put(
        MemoryRecord(
            id="paged-record",
            title="Paged logical record",
            content="Canonical replay must page until logical origin.",
            layer=MemoryLayer.WORKING,
        )
    )
    first.close()
    sidecar.unlink()

    reopened = MemvidBackend(path=path, layer=MemoryLayer.WORKING)
    reopened.open()
    try:
        assert reopened.get_record("paged-record") is not None
        assert fake_mem.cursors == [None, 43]
    finally:
        reopened.close()


def test_memvid_backend_fails_closed_when_timeline_stops_before_logical_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "working.mv2"
    path.write_bytes(b"existing")

    class FakeTruncatedMem:
        def stats(self) -> dict[str, int]:
            return {
                "frame_count": 10,
                "active_frame_count": 10,
                "seq_no": 1,
                "size_bytes": 10,
            }

        def timeline(self, **kwargs: object) -> list[dict[str, object]]:
            if kwargs.get("as_of_frame") is None:
                return [{"frame_id": 9, "uri": "mv2://working/observation/newest"}]
            return []

        def frame(self, uri: str) -> dict[str, object]:
            raise AssertionError(f"truncated replay must fail before reading {uri}")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=lambda *args, **kwargs: FakeTruncatedMem(),
            use=lambda *args, **kwargs: FakeTruncatedMem(),
        ),
    )
    backend = MemvidBackend(path=path, layer=MemoryLayer.WORKING)

    with pytest.raises(RuntimeError, match="ended before origin frame 0"):
        backend.open()


def test_memvid_backend_fails_closed_on_canonical_event_sequence_or_hash_gap(
    tmp_path: Path,
) -> None:
    first = {
        "schema_version": 1,
        "event": "chain_anchor",
        "commit_sequence": 1,
        "previous_event_sha256": None,
    }
    first_digest = sha256(
        json.dumps(first, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()

    sequence_gap = MemvidBackend(path=tmp_path / "sequence.mv2", layer=MemoryLayer.WORKING)
    sequence_gap._apply_canonical_event(first)
    with pytest.raises(RuntimeError, match="sequence is truncated or out of order"):
        sequence_gap._apply_canonical_event(
            {
                "schema_version": 1,
                "event": "chain_anchor",
                "commit_sequence": 3,
                "previous_event_sha256": first_digest,
            }
        )

    hash_gap = MemvidBackend(path=tmp_path / "hash.mv2", layer=MemoryLayer.WORKING)
    hash_gap._apply_canonical_event(first)
    with pytest.raises(RuntimeError, match="hash chain is truncated or out of order"):
        hash_gap._apply_canonical_event(
            {
                "schema_version": 1,
                "event": "chain_anchor",
                "commit_sequence": 2,
                "previous_event_sha256": "0" * 64,
            }
        )


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
