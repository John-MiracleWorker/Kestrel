from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from threading import Barrier, Thread

import numpy as np
import pytest

import nested_memvid_agent.layers as layers_module
from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system


def test_in_memory_backend_contract_round_trips_mutation_and_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "semantic.mv2"
    backend = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    first_id = backend.put(
        MemoryRecord(
            id="backend-record",
            title="Backend record",
            content="sentinel_backend_consistency_40de original content.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
            metadata={"frame_id": "backend-frame"},
        )
    )
    upsert_id = backend.upsert(
        MemoryRecord(
            id="backend-record",
            title="Backend record",
            content="sentinel_backend_consistency_40de updated content.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.91,
            metadata={"frame_id": "backend-frame"},
        )
    )

    assert first_id == upsert_id == "backend-record"
    assert backend.get_record("backend-record").content.endswith("updated content.")
    assert backend.get_record("backend-frame").id == "backend-record"  # type: ignore[union-attr]
    assert [record.id for record in backend.iter_records()] == ["backend-record"]
    assert backend.find("sentinel_backend_consistency_40de", k=3)
    assert backend.verify() is True

    backend.tombstone("backend-record", reason="superseded", superseded_by="backend-record-2")
    assert backend.find("sentinel_backend_consistency_40de", include_inactive=False) == []
    assert backend.find("sentinel_backend_consistency_40de", include_inactive=True)
    assert list(backend.iter_records()) == []
    assert [record.id for record in backend.iter_records(include_inactive=True)] == ["backend-record"]
    backend.seal()
    assert path.with_suffix(".memory.json").exists()
    backend.close()

    InMemoryBackend._global_records.pop(str(path), None)
    reopened = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)
    reopened.open()
    try:
        inactive = reopened.get_record("backend-record", include_inactive=True)
        assert inactive is not None
        assert inactive.metadata["active"] is False
        assert inactive.metadata["tombstone_reason"] == "superseded"
        assert reopened.get_record("backend-record", include_inactive=False) is None
    finally:
        reopened.close()


def test_in_memory_backend_rejects_cross_layer_put_and_upsert(tmp_path: Path) -> None:
    backend = InMemoryBackend(path=tmp_path / "working.mv2", layer=MemoryLayer.WORKING)
    backend.open()
    wrong_layer = MemoryRecord(
        title="Wrong layer",
        content="A semantic record cannot be written to working backend.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.9,
    )

    for operation in (backend.put, backend.upsert):
        try:
            operation(wrong_layer)
        except ValueError as exc:
            assert "Cannot write" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("cross-layer write was accepted")


def test_changed_upsert_keeps_lexical_results_bound_to_record_ids(tmp_path: Path) -> None:
    backend = InMemoryBackend(path=tmp_path / "semantic.mv2", layer=MemoryLayer.SEMANTIC)
    backend.open()
    backend.put(
        MemoryRecord(
            id="fact-alpha",
            title="Alpha",
            content="originalalpha",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )
    backend.put(
        MemoryRecord(
            id="fact-beta",
            title="Beta",
            content="uniquebeta",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )
    backend.upsert(
        MemoryRecord(
            id="fact-alpha",
            title="Alpha revised",
            content="newtoken",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.91,
        )
    )

    assert [hit.record.id for hit in backend.find("newtoken", k=8)] == ["fact-alpha"]
    assert [hit.record.id for hit in backend.find("uniquebeta", k=8)] == ["fact-beta"]


def test_vector_search_bounds_k_to_corpus_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = InMemoryBackend(
        path=tmp_path / "semantic.mv2",
        layer=MemoryLayer.SEMANTIC,
        enable_vec=True,
    )
    monkeypatch.setattr(
        backend,
        "_encode",
        lambda _text: np.asarray([1.0, 0.0], dtype=np.float64),
    )
    backend.open()
    backend.put(
        MemoryRecord(
            id="vector-fact",
            title="Vector fact",
            content="A one-record vector corpus.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )

    assert [hit.record.id for hit in backend.find("query", k=8, mode="vec")] == [
        "vector-fact"
    ]
    assert backend.find("query", k=0, mode="vec") == []
    assert backend.find("query", k=-1, mode="vec") == []


def test_same_path_backends_share_search_state_and_concurrent_seals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "semantic.mv2"
    backends = [
        InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC),
        InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC),
    ]
    opened = Barrier(2)
    written = Barrier(2)
    sealing = Barrier(2)
    errors: list[BaseException] = []
    observed: dict[str, tuple[bool, bool]] = {}

    def worker(backend: InMemoryBackend, record_id: str, token: str) -> None:
        try:
            backend.open()
            opened.wait(timeout=5)
            backend.put(
                MemoryRecord(
                    id=record_id,
                    title=f"Concurrent {token}",
                    content=f"The durable concurrency token is {token}.",
                    layer=MemoryLayer.SEMANTIC,
                    kind=MemoryKind.FACT,
                    confidence=0.9,
                )
            )
            written.wait(timeout=5)
            observed[record_id] = (
                bool(backend.find("aardvark", k=4)),
                bool(backend.find("platypus", k=4)),
            )
            sealing.wait(timeout=5)
            backend.seal()
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    threads = [
        Thread(target=worker, args=(backends[0], "fact-a", "aardvark")),
        Thread(target=worker, args=(backends[1], "fact-b", "platypus")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert observed == {"fact-a": (True, True), "fact-b": (True, True)}

    InMemoryBackend._global_records.pop(str(path), None)
    reopened = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)
    reopened.open()
    try:
        assert {record.id for record in reopened.iter_records()} == {"fact-a", "fact-b"}
        assert reopened.find("aardvark", k=4)
        assert reopened.find("platypus", k=4)
    finally:
        reopened.close()


def test_same_path_concurrent_open_and_seal_do_not_race_private_file_validation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "episodic.mv2"
    workers = 8
    iterations = 40
    start = Barrier(workers)
    errors: list[BaseException] = []

    def worker(worker_index: int) -> None:
        try:
            start.wait(timeout=5)
            for iteration in range(iterations):
                backend = InMemoryBackend(path=path, layer=MemoryLayer.EPISODIC)
                backend.open()
                backend.put(
                    MemoryRecord(
                        id=f"record-{worker_index}-{iteration}",
                        title="Concurrent lifecycle",
                        content=f"worker {worker_index} iteration {iteration}",
                        layer=MemoryLayer.EPISODIC,
                        kind=MemoryKind.OBSERVATION,
                        confidence=0.8,
                    )
                )
                backend.seal()
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    threads = [Thread(target=worker, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    reopened = InMemoryBackend(path=path, layer=MemoryLayer.EPISODIC)
    reopened.open()
    try:
        assert len(tuple(reopened.iter_records())) == workers * iterations
    finally:
        reopened.close()


def test_concurrent_runtime_backend_factory_defers_hardening_to_locked_backends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_unlocked_hardening(_path: Path) -> bool:
        raise AssertionError("runtime factory used unlocked artifact hardening")

    monkeypatch.setattr(
        layers_module,
        "harden_memory_artifact_files",
        reject_unlocked_hardening,
    )

    memory = build_memory_system("memory", tmp_path / "memory")
    memory.close_all()


def test_close_only_seals_layers_written_by_that_memory_system(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory = build_memory_system("memory", memory_dir)
    memory.put(
        MemoryRecord(
            id="working-only",
            title="Working only",
            content="Only the dirty working layer needs a durable snapshot.",
            layer=MemoryLayer.WORKING,
            kind=MemoryKind.OBSERVATION,
            confidence=0.8,
        )
    )

    memory.close_all()

    assert (memory_dir / "working.memory.json").is_file()
    assert not (memory_dir / "self.memory.json").exists()
    assert not (memory_dir / "policy.memory.json").exists()


def test_cross_process_seals_merge_distinct_records_without_last_writer_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "semantic.mv2"
    go = tmp_path / "go"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from nested_memvid_agent.backends.in_memory import InMemoryBackend\n"
        "from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord\n"
        "path, ready, go = map(Path, sys.argv[1:4])\n"
        "record_id, token = sys.argv[4:6]\n"
        "backend = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)\n"
        "backend.open()\n"
        "ready.write_text('ready', encoding='utf-8')\n"
        "deadline = time.monotonic() + 10\n"
        "while not go.exists():\n"
        "    if time.monotonic() >= deadline: raise TimeoutError('go barrier')\n"
        "    time.sleep(0.01)\n"
        "backend.put(MemoryRecord(id=record_id, title=token, content=f'cross process {token}', "
        "layer=MemoryLayer.SEMANTIC, kind=MemoryKind.FACT, confidence=0.9))\n"
        "backend.seal()\n"
    )
    processes: list[subprocess.Popen[str]] = []
    ready_paths: list[Path] = []
    for index, token in enumerate(("aardvark", "platypus")):
        ready = tmp_path / f"ready-{index}"
        ready_paths.append(ready)
        processes.append(
            subprocess.Popen(  # noqa: S603 - fixed interpreter and deterministic test script
                [sys.executable, "-c", script, str(path), str(ready), str(go), f"fact-{index}", token],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    deadline = time.monotonic() + 10
    while not all(ready.exists() for ready in ready_paths):
        if time.monotonic() >= deadline:
            raise TimeoutError("worker readiness barrier")
        time.sleep(0.01)
    go.write_text("go", encoding="utf-8")
    for process in processes:
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr

    backend = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    try:
        assert {record.id for record in backend.iter_records()} == {"fact-0", "fact-1"}
        assert backend.find("aardvark", k=4)
        assert backend.find("platypus", k=4)
    finally:
        backend.close()
