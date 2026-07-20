from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import nested_memvid_agent.private_artifacts as private_artifacts
from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.backends.memvid_backend import MemvidBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.layers import (
    DEFAULT_LAYER_SPECS,
    prepare_private_memory_artifacts,
    prepare_private_runs_root,
)
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.server import create_app
from nested_memvid_agent.task_capsule import (
    TaskCapsuleWriter,
    summarize_run_capsule,
    write_run_capsule,
)
from nested_memvid_agent.vector_sidecar import VectorSidecar

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")


class _FakeMemvid:
    def put(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        return "sdk_record"

    def seal(self) -> None:
        return None

    def close(self) -> None:
        return None


class _StubEmbedder:
    model_name = "stub"

    def embed(self, text: str) -> np.ndarray:
        del text
        return np.asarray([1.0, 0.5], dtype=np.float32)


def test_in_memory_snapshot_and_created_leaf_are_owner_only(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    path = memory_dir / "semantic.mv2"
    backend = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    backend.put(_record("memory-permissions"))
    backend.seal()
    snapshot = path.with_suffix(".memory.json")

    assert _mode(memory_dir) == 0o700
    assert _mode(snapshot) == 0o600

    os.chmod(snapshot, 0o644)
    reopened = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)
    reopened.open()
    assert _mode(snapshot) == 0o600


def test_layered_memory_system_creates_owner_only_memory_leaf(tmp_path: Path) -> None:
    memory_dir = tmp_path / "nested" / "memory"
    memory = build_memory_system("memory", memory_dir)
    try:
        assert _mode(memory_dir) == 0o700
    finally:
        memory.close_all()
    assert all(_mode(path) == 0o600 for path in memory_dir.glob("*.memory.json"))


def test_memory_backend_switch_hardens_every_existing_layer_variant(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    artifacts: list[Path] = []
    for spec in DEFAULT_LAYER_SPECS.values():
        canonical = memory_dir / spec.mv2_file
        variants = (
            (canonical, b"legacy canonical bytes"),
            (canonical.with_suffix(".memory.json"), b"[]"),
            (canonical.with_suffix(".mv2.records.json"), b'{"records":[]}'),
        )
        for artifact, content in variants:
            artifact.write_bytes(content)
            os.chmod(artifact, 0o644)
            artifacts.append(artifact)

    memory = build_memory_system("memory", memory_dir)
    memory.close_all()

    assert all(_mode(artifact) == 0o600 for artifact in artifacts)


def test_existing_custom_memory_directory_keeps_its_mode(tmp_path: Path) -> None:
    memory_dir = tmp_path / "custom-memory"
    memory_dir.mkdir(mode=0o755)
    os.chmod(memory_dir, 0o755)
    backend = InMemoryBackend(
        path=memory_dir / "semantic.mv2",
        layer=MemoryLayer.SEMANTIC,
    )

    backend.open()
    backend.put(_record("custom-directory"))
    backend.seal()

    assert _mode(memory_dir) == 0o755
    assert _mode(memory_dir / "semantic.memory.json") == 0o600


@pytest.mark.parametrize(
    "artifact_name",
    ["semantic.mv2", "semantic.memory.json", "semantic.mv2.records.json"],
)
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_in_memory_rejects_aliased_layer_variant_without_mutating_target(
    tmp_path: Path,
    artifact_name: str,
    link_kind: str,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('[{"content":"private"}]', encoding="utf-8")
    os.chmod(outside, 0o644)
    artifact = memory_dir / artifact_name
    _link(artifact, outside, link_kind)
    before = outside.read_bytes()

    backend = InMemoryBackend(
        path=memory_dir / "semantic.mv2",
        layer=MemoryLayer.SEMANTIC,
    )
    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        backend.open()

    assert outside.read_bytes() == before
    assert _mode(outside) == 0o644


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_in_memory_rejects_aliased_snapshot_lock_without_mutating_target(
    tmp_path: Path,
    link_kind: str,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    outside = tmp_path / "outside-memory-lock"
    outside.write_text("outside lock", encoding="utf-8")
    os.chmod(outside, 0o644)
    lock_path = memory_dir / ".semantic.mv2.kestrel.lock"
    _link(lock_path, outside, link_kind)

    backend = InMemoryBackend(
        path=memory_dir / "semantic.mv2",
        layer=MemoryLayer.SEMANTIC,
    )
    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        backend.open()

    assert outside.read_text(encoding="utf-8") == "outside lock"
    assert _mode(outside) == 0o644


def test_private_file_rejects_foreign_owner_before_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "foreign.memory.json"
    artifact.write_text("private", encoding="utf-8")
    os.chmod(artifact, 0o644)
    actual_uid = artifact.stat().st_uid
    monkeypatch.setattr(private_artifacts.os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(PermissionError, match="owned by the current user"):
        private_artifacts.harden_private_file(artifact)

    assert _mode(artifact) == 0o644
    assert artifact.read_text(encoding="utf-8") == "private"


def test_private_file_rejects_nonregular_path(tmp_path: Path) -> None:
    artifact = tmp_path / "not-a-file.memory.json"
    artifact.mkdir()

    with pytest.raises(ValueError, match="regular files"):
        private_artifacts.harden_private_file(artifact)

    assert artifact.is_dir()


def test_memvid_creation_is_not_precreated_and_artifacts_are_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "memory" / "semantic.mv2"
    create_saw_missing: list[bool] = []

    def fake_create(filename: str, **kwargs: object) -> _FakeMemvid:
        del kwargs
        target = Path(filename)
        create_saw_missing.append(not target.exists())
        target.write_bytes(b"fake mv2")
        os.chmod(target, 0o644)
        return _FakeMemvid()

    _install_fake_memvid(monkeypatch, create=fake_create)
    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    backend.put(_record("memvid-permissions"))
    backend.close()

    assert create_saw_missing == [True]
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    assert _mode(path.with_suffix(".mv2.records.json")) == 0o600


def test_memvid_creation_fails_when_sdk_does_not_materialize_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "memory" / "semantic.mv2"
    created_handle = _FakeMemvid()
    _install_fake_memvid(monkeypatch, create=lambda *args, **kwargs: created_handle)

    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    with pytest.raises(FileNotFoundError):
        backend.open()

    assert not path.exists()
    assert backend.mem is None


def test_memvid_open_hardens_stale_in_memory_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    path = memory_dir / "semantic.mv2"
    variants = (
        (path, b"fake mv2"),
        (path.with_suffix(".memory.json"), b"[]"),
        (path.with_suffix(".mv2.records.json"), b'{"records":[]}'),
    )
    for artifact, content in variants:
        artifact.write_bytes(content)
        os.chmod(artifact, 0o644)
    _install_fake_memvid(monkeypatch)

    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    backend.close()

    assert all(_mode(artifact) == 0o600 for artifact, _content in variants)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_memvid_rejects_aliased_container_without_mutating_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    outside = tmp_path / "outside.mv2"
    outside.write_bytes(b"outside memvid")
    os.chmod(outside, 0o644)
    path = memory_dir / "semantic.mv2"
    _link(path, outside, link_kind)
    calls: list[str] = []
    _install_fake_memvid(
        monkeypatch,
        create=lambda *args, **kwargs: calls.append("create"),
        use=lambda *args, **kwargs: calls.append("use"),
    )

    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        backend.open()

    assert calls == []
    assert outside.read_bytes() == b"outside memvid"
    assert _mode(outside) == 0o644


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_memvid_rejects_aliased_exact_index_without_mutating_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    path = memory_dir / "semantic.mv2"
    path.write_bytes(b"fake mv2")
    outside = tmp_path / "outside-index.json"
    outside.write_text('{"records":[]}', encoding="utf-8")
    os.chmod(outside, 0o644)
    _link(path.with_suffix(".mv2.records.json"), outside, link_kind)
    _install_fake_memvid(monkeypatch)

    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        backend.open()

    assert outside.read_text(encoding="utf-8") == '{"records":[]}'
    assert _mode(outside) == 0o644


@pytest.mark.parametrize("lock_scope", ["memory", "layer"])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_memvid_rejects_aliased_lock_without_mutating_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lock_scope: str,
    link_kind: str,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    path = memory_dir / "semantic.mv2"
    path.write_bytes(b"fake mv2")
    outside = tmp_path / f"outside-{lock_scope}.lock"
    outside.write_text("outside lock", encoding="utf-8")
    os.chmod(outside, 0o644)
    lock_path = (
        tmp_path / ".memory.kestrel-memory.lock"
        if lock_scope == "memory"
        else memory_dir / ".semantic.mv2.kestrel.lock"
    )
    _link(lock_path, outside, link_kind)
    _install_fake_memvid(monkeypatch)

    backend = MemvidBackend(path=path, layer=MemoryLayer.SEMANTIC)
    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        backend.open()

    assert outside.read_text(encoding="utf-8") == "outside lock"
    assert _mode(outside) == 0o644


def test_memory_task_capsule_artifacts_are_owner_only(tmp_path: Path) -> None:
    path = write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="private-memory-capsule",
        objective="Keep this prompt private",
    )

    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    assert _mode(path.with_suffix(".memory.json")) == 0o600


def test_startup_protects_legacy_capsule_root_without_scanning_children(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "legacy-run"
    run_dir.mkdir(parents=True, mode=0o755)
    os.chmod(run_dir, 0o755)
    known = tuple(run_dir / name for name in (
        "complete.mv2",
        "complete.memory.json",
        "complete.mv2.records.json",
    ))
    for artifact in known:
        artifact.write_text("legacy private capsule", encoding="utf-8")
        os.chmod(artifact, 0o644)
    unknown = run_dir / "unrelated.txt"
    unknown.write_text("not a capsule artifact", encoding="utf-8")
    os.chmod(unknown, 0o644)

    prepare_private_runs_root(runs_dir)

    assert _mode(runs_dir) == 0o700
    assert _mode(run_dir) == 0o755
    assert all(_mode(artifact) == 0o644 for artifact in known)
    assert _mode(unknown) == 0o644


def test_memory_only_build_does_not_touch_unrelated_sibling_runs_directory(
    tmp_path: Path,
) -> None:
    unrelated_runs = tmp_path / "runs"
    unrelated_runs.mkdir(mode=0o755)
    marker = unrelated_runs / "unrelated.txt"
    marker.write_text("not this memory runtime", encoding="utf-8")
    os.chmod(marker, 0o644)

    memory = build_memory_system("memory", tmp_path / "flat-memory")
    memory.close_all()

    assert _mode(unrelated_runs) == 0o755
    assert marker.read_text(encoding="utf-8") == "not this memory runtime"
    assert _mode(marker) == 0o644


def test_memory_artifact_preparation_rejects_escaping_config_before_target_mutation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.mv2"
    outside.write_text("outside memory", encoding="utf-8")
    os.chmod(outside, 0o644)
    specs = dict(DEFAULT_LAYER_SPECS)
    specs[MemoryLayer.SEMANTIC] = replace(
        specs[MemoryLayer.SEMANTIC],
        mv2_file=str(outside.resolve()),
    )
    memory_dir = tmp_path / "runtime" / "memory"

    with pytest.raises(ValueError, match="single filename"):
        prepare_private_memory_artifacts(memory_dir, specs=specs)

    assert outside.read_text(encoding="utf-8") == "outside memory"
    assert _mode(outside) == 0o644
    assert not memory_dir.exists()
    assert not (memory_dir.parent / "runs").exists()


def test_server_bootstrap_repairs_finite_private_artifacts_before_first_request(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    memory_dir = runtime_root / "memory"
    memory_dir.mkdir(parents=True, mode=0o755)
    os.chmod(memory_dir, 0o755)
    memory_artifacts = (
        memory_dir / "semantic.mv2",
        memory_dir / "semantic.memory.json",
        memory_dir / "semantic.mv2.records.json",
    )
    for artifact in memory_artifacts:
        artifact.write_text("legacy private memory", encoding="utf-8")
        os.chmod(artifact, 0o644)

    runs_dir = runtime_root / "runs"
    legacy_run = runs_dir / "legacy-run"
    legacy_run.mkdir(parents=True, mode=0o755)
    os.chmod(runs_dir, 0o755)
    os.chmod(legacy_run, 0o755)
    legacy_capsule = legacy_run / "complete.memory.json"
    legacy_capsule.write_text("legacy private capsule", encoding="utf-8")
    os.chmod(legacy_capsule, 0o644)

    secret_path = runtime_root / "secrets" / "local_vault.json"
    secret_path.parent.mkdir()
    secret_path.write_text('{"secrets": {}}', encoding="utf-8")
    os.chmod(secret_path, 0o644)

    config = AgentConfig(
        memory_dir=memory_dir,
        state_path=runtime_root / "state" / "agent.db",
        log_dir=runtime_root / "logs",
        secret_store_path=secret_path,
        skills_dir=runtime_root / "skills",
        plugins_dir=runtime_root / "plugins",
        mcp_config_path=runtime_root / "config" / "mcp_servers.json",
        channel_config_path=runtime_root / "config" / "channels.json",
        worker_worktree_dir=runtime_root / "worktrees",
        workspace=tmp_path / "workspace",
    )

    app = create_app(config)

    assert app is not None
    assert _mode(memory_dir) == 0o755
    assert all(_mode(artifact) == 0o600 for artifact in memory_artifacts)
    assert _mode(runs_dir) == 0o700
    assert _mode(legacy_run) == 0o755
    assert _mode(legacy_capsule) == 0o644
    assert _mode(secret_path) == 0o600


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_lazy_capsule_access_rejects_alias_without_mutating_target(
    tmp_path: Path,
    link_kind: str,
) -> None:
    run_dir = tmp_path / "runs" / "legacy-run"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside-legacy-capsule"
    outside.write_text("outside capsule", encoding="utf-8")
    os.chmod(outside, 0o644)
    _link(run_dir / "complete.mv2.records.json", outside, link_kind)

    prepare_private_runs_root(tmp_path / "runs")
    assert _mode(tmp_path / "runs") == 0o700

    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        summarize_run_capsule(
            runs_dir=tmp_path / "runs",
            run_id="legacy-run",
        )

    assert outside.read_text(encoding="utf-8") == "outside capsule"
    assert _mode(outside) == 0o644


def test_capsule_read_hardens_legacy_variants(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    path = write_run_capsule(
        runs_dir=runs_dir,
        run_id="legacy-read",
        objective="Read a legacy capsule privately",
    )
    variants = (path, path.with_suffix(".memory.json"))
    for artifact in variants:
        os.chmod(artifact, 0o644)
    os.chmod(runs_dir, 0o755)

    summary = summarize_run_capsule(runs_dir=runs_dir, run_id="legacy-read")

    assert summary.objective == "Read a legacy capsule privately"
    assert _mode(runs_dir) == 0o700
    assert all(_mode(artifact) == 0o600 for artifact in variants)


@pytest.mark.parametrize(
    "unsafe_run_id",
    ["../outside-run", "nested/outside-run", r"..\outside-run"],
)
def test_capsule_summary_rejects_traversal_before_target_mutation(
    tmp_path: Path,
    unsafe_run_id: str,
) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(mode=0o755)
    outside_run = tmp_path / "outside-run"
    outside_run.mkdir(mode=0o755)
    artifact = outside_run / "complete.memory.json"
    artifact.write_text("outside capsule", encoding="utf-8")
    os.chmod(artifact, 0o644)

    with pytest.raises(ValueError, match="single safe path component"):
        summarize_run_capsule(runs_dir=runs_dir, run_id=unsafe_run_id)

    assert outside_run.is_dir()
    assert _mode(outside_run) == 0o755
    assert artifact.read_text(encoding="utf-8") == "outside capsule"
    assert _mode(artifact) == 0o644


def test_capsule_summary_rejects_absolute_run_id_before_target_mutation(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(mode=0o755)
    outside_run = tmp_path / "outside-absolute-run"
    outside_run.mkdir(mode=0o755)
    artifact = outside_run / "complete.memory.json"
    artifact.write_text("absolute outside capsule", encoding="utf-8")
    os.chmod(artifact, 0o644)

    with pytest.raises(ValueError, match="single safe path component"):
        summarize_run_capsule(runs_dir=runs_dir, run_id=str(outside_run.resolve()))

    assert _mode(outside_run) == 0o755
    assert artifact.read_text(encoding="utf-8") == "absolute outside capsule"
    assert _mode(artifact) == 0o644


@pytest.mark.parametrize(
    "unsafe_run_id",
    ["../outside-run", "nested/outside-run", r"..\outside-run"],
)
def test_capsule_writer_rejects_traversal_before_filesystem_mutation(
    tmp_path: Path,
    unsafe_run_id: str,
) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(mode=0o755)
    outside_run = tmp_path / "outside-run"
    outside_run.mkdir(mode=0o755)

    with pytest.raises(ValueError, match="single safe path component"):
        TaskCapsuleWriter(runs_dir=runs_dir, run_id=unsafe_run_id)

    assert _mode(runs_dir) == 0o755
    assert _mode(outside_run) == 0o755


def test_capsule_writer_rejects_absolute_run_id_before_filesystem_mutation(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(mode=0o755)
    outside_run = tmp_path / "outside-absolute-run"
    outside_run.mkdir(mode=0o755)

    with pytest.raises(ValueError, match="single safe path component"):
        TaskCapsuleWriter(runs_dir=runs_dir, run_id=str(outside_run.resolve()))

    assert _mode(runs_dir) == 0o755
    assert _mode(outside_run) == 0o755


def test_large_capsule_history_startup_is_constant_scope_and_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(mode=0o755)
    os.chmod(runs_dir, 0o755)
    for index in range(1001):
        run_dir = runs_dir / f"run-{index:04d}"
        run_dir.mkdir()
        artifact = run_dir / "complete.memory.json"
        artifact.write_text("legacy private capsule", encoding="utf-8")
        os.chmod(artifact, 0o644)

    def fail_rescan(_path: object) -> object:
        raise AssertionError("startup must not scan historical run children")

    monkeypatch.setattr(private_artifacts.os, "scandir", fail_rescan)
    prepare_private_runs_root(runs_dir)

    assert _mode(runs_dir) == 0o700
    assert _mode(runs_dir / "run-1000" / "complete.memory.json") == 0o644


def test_capsule_root_rejects_symlink_without_mutating_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-runs"
    outside.mkdir(mode=0o755)
    os.chmod(outside, 0o755)
    (tmp_path / "runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="directories must not be symbolic links"):
        prepare_private_runs_root(tmp_path / "runs")

    assert _mode(outside) == 0o755


def test_capsule_summary_rejects_symlinked_root_without_mutating_target(
    tmp_path: Path,
) -> None:
    outside_root = tmp_path / "outside-runs"
    outside_run = outside_root / "legacy-run"
    outside_run.mkdir(parents=True, mode=0o755)
    os.chmod(outside_root, 0o755)
    os.chmod(outside_run, 0o755)
    artifact = outside_run / "complete.memory.json"
    artifact.write_text("outside capsule", encoding="utf-8")
    os.chmod(artifact, 0o644)
    runs_dir = tmp_path / "runs"
    runs_dir.symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(ValueError, match="directories must not be symbolic links"):
        summarize_run_capsule(runs_dir=runs_dir, run_id="legacy-run")

    assert _mode(outside_root) == 0o755
    assert _mode(outside_run) == 0o755
    assert artifact.read_text(encoding="utf-8") == "outside capsule"
    assert _mode(artifact) == 0o644


@pytest.mark.parametrize(
    "artifact_name",
    ["complete.mv2", "complete.memory.json", "complete.mv2.records.json"],
)
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_task_capsule_rejects_aliases_without_mutating_target(
    tmp_path: Path,
    artifact_name: str,
    link_kind: str,
) -> None:
    run_dir = tmp_path / "runs" / "unsafe-capsule"
    run_dir.mkdir(parents=True)
    outside = tmp_path / f"outside-{artifact_name}"
    outside.write_text("outside capsule", encoding="utf-8")
    os.chmod(outside, 0o644)
    _link(run_dir / artifact_name, outside, link_kind)
    writer = TaskCapsuleWriter(
        runs_dir=tmp_path / "runs",
        run_id="unsafe-capsule",
        backend="memory",
    )

    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        writer.open()

    assert outside.read_text(encoding="utf-8") == "outside capsule"
    assert _mode(outside) == 0o644


def test_memvid_task_capsule_container_and_index_are_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_create(filename: str, **kwargs: object) -> _FakeMemvid:
        del kwargs
        target = Path(filename)
        assert not target.exists()
        target.write_bytes(b"fake capsule mv2")
        os.chmod(target, 0o644)
        return _FakeMemvid()

    _install_fake_memvid(monkeypatch, create=fake_create)
    path = write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="private-memvid-capsule",
        objective="Keep this Memvid prompt private",
        backend="memvid",
    )

    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    assert _mode(path.with_suffix(".mv2.records.json")) == 0o600


def test_vector_sidecar_and_live_sqlite_files_are_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "indexes" / "semantic.vector.sqlite"
    sidecar = VectorSidecar(
        path=path,
        layer=MemoryLayer.SEMANTIC,
        embedder=_StubEmbedder(),
        mv2_path=tmp_path / "memory" / "semantic.mv2",
    )
    sidecar.open()
    try:
        assert _mode(path.parent) == 0o700
        assert _mode(path) == 0o600
        for sqlite_sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
            if sqlite_sidecar.exists():
                assert _mode(sqlite_sidecar) == 0o600
    finally:
        sidecar.close()


@pytest.mark.parametrize("artifact_suffix", ["", "-wal", "-shm"])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_vector_sidecar_rejects_aliases_without_mutating_target(
    tmp_path: Path,
    artifact_suffix: str,
    link_kind: str,
) -> None:
    index_dir = tmp_path / "indexes"
    index_dir.mkdir()
    path = index_dir / "semantic.vector.sqlite"
    if artifact_suffix:
        path.touch()
    outside = tmp_path / f"outside-vector{artifact_suffix or '-main'}"
    outside.write_bytes(b"outside vector")
    os.chmod(outside, 0o644)
    _link(Path(f"{path}{artifact_suffix}"), outside, link_kind)
    sidecar = VectorSidecar(
        path=path,
        layer=MemoryLayer.SEMANTIC,
        embedder=_StubEmbedder(),
        mv2_path=tmp_path / "memory" / "semantic.mv2",
    )

    with pytest.raises(ValueError, match="symbolic links|hard-linked"):
        sidecar.open()

    assert outside.read_bytes() == b"outside vector"
    assert _mode(outside) == 0o644


def _record(record_id: str) -> MemoryRecord:
    return MemoryRecord(
        id=record_id,
        title="Private memory",
        content="Prompts and responses require owner-only storage.",
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.9,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _link(path: Path, target: Path, link_kind: str) -> None:
    if link_kind == "symlink":
        path.symlink_to(target)
    else:
        path.hardlink_to(target)


def _install_fake_memvid(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create: object | None = None,
    use: object | None = None,
) -> None:
    monkeypatch.setattr(
        "nested_memvid_agent.backends.memvid_backend.import_module",
        lambda name: SimpleNamespace(
            create=create or (lambda *args, **kwargs: _FakeMemvid()),
            use=use or (lambda *args, **kwargs: _FakeMemvid()),
        ),
    )
