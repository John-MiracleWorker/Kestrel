from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from nested_memvid_agent import memory_backup as memory_backup_module
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS
from nested_memvid_agent.memory_backup import MemoryBackupError, MemoryBackupManager


def _seed_memory(memory_dir: Path, suffix: str = "original") -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    for spec in DEFAULT_LAYER_SPECS.values():
        path = memory_dir / spec.mv2_file
        path.write_bytes(f"{spec.layer.value}:{suffix}".encode())
        path.with_suffix(f"{path.suffix}.records.json").write_text(
            f'{{"layer":"{spec.layer.value}","value":"{suffix}"}}',
            encoding="utf-8",
        )


@pytest.mark.parametrize(
    "backup_relative", [Path("memory/backups"), Path("."), Path("memory")]
)
def test_memory_backup_rejects_overlapping_memory_and_backup_roots(
    tmp_path: Path,
    backup_relative: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    backup_root = tmp_path / backup_relative

    with pytest.raises(MemoryBackupError, match="must not overlap"):
        MemoryBackupManager(memory_dir=memory_dir, backup_root=backup_root)


def test_memory_backup_round_trip_validates_checksums_and_creates_safety_copy(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir)
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)

    manifest = manager.create(retain=4)
    assert manager.validate(manifest["backup_id"])["ok"] is True

    _seed_memory(memory_dir, suffix="changed")
    restored = manager.restore(manifest["backup_id"], retain=4)

    assert restored["restored_files"] == 12
    assert restored["safety_backup_id"] != manifest["backup_id"]
    assert restored["safety_backup_restorable"] is True
    for spec in DEFAULT_LAYER_SPECS.values():
        assert (memory_dir / spec.mv2_file).read_bytes() == f"{spec.layer.value}:original".encode()
    assert len(manager.list_backups()) == 2


def test_memory_backup_restores_when_live_memory_is_missing(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir)
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)
    manifest = manager.create()
    shutil.rmtree(memory_dir)
    verified: list[Path] = []

    restored = manager.restore(
        manifest["backup_id"],
        verify_staging=lambda path: verified.append(path),
    )

    assert restored["safety_backup_id"] is None
    assert restored["safety_backup_restorable"] is False
    assert len(verified) == 1
    assert verified[0] != memory_dir
    for spec in DEFAULT_LAYER_SPECS.values():
        assert (memory_dir / spec.mv2_file).is_file()


def test_memory_backup_verifies_staging_before_replacing_live_memory(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir, suffix="backup")
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)
    manifest = manager.create()
    _seed_memory(memory_dir, suffix="live")

    def reject_staging(path: Path) -> None:
        assert path != memory_dir
        raise MemoryBackupError("staging verification failed")

    with pytest.raises(MemoryBackupError, match="staging verification failed"):
        manager.restore(manifest["backup_id"], verify_staging=reject_staging)

    for spec in DEFAULT_LAYER_SPECS.values():
        assert (memory_dir / spec.mv2_file).read_bytes() == f"{spec.layer.value}:live".encode()


def test_restore_retention_does_not_prune_the_selected_older_backup(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir, suffix="oldest")
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)
    selected = manager.create(retain=10)
    for suffix in ("middle", "newest"):
        _seed_memory(memory_dir, suffix=suffix)
        manager.create(retain=10)

    restored = manager.restore(selected["backup_id"], retain=2)

    assert restored["backup_id"] == selected["backup_id"]
    assert (backups / selected["backup_id"]).is_dir()
    for spec in DEFAULT_LAYER_SPECS.values():
        assert (memory_dir / spec.mv2_file).read_bytes() == f"{spec.layer.value}:oldest".encode()


def test_memory_backup_rejects_corruption_and_path_traversal(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir)
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)
    manifest = manager.create()
    backup_id = manifest["backup_id"]
    (backups / backup_id / "memory" / "working.mv2").write_bytes(b"corrupt")

    validation = manager.validate(backup_id)
    assert validation["ok"] is False
    assert any(error.startswith("checksum:") for error in validation["errors"])
    with pytest.raises(MemoryBackupError):
        manager.restore(backup_id)
    with pytest.raises(MemoryBackupError):
        manager.validate("../escape")


def test_memory_backup_rejects_symlinked_sources_and_unexpected_manifest_files(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir)
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)
    first_spec = next(iter(DEFAULT_LAYER_SPECS.values()))
    layer = memory_dir / first_spec.mv2_file
    sidecar = layer.with_suffix(f"{layer.suffix}.records.json")
    outside = tmp_path / "outside.json"
    outside.write_text("secret", encoding="utf-8")
    sidecar.unlink()
    sidecar.symlink_to(outside)

    with pytest.raises(MemoryBackupError, match="cannot be a symlink"):
        manager.create()

    sidecar.unlink()
    sidecar.write_text("{}", encoding="utf-8")
    manifest = manager.create()
    manifest_path = backups / manifest["backup_id"] / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"].append({"path": "memory/unexpected.txt", "size": 0, "sha256": ""})
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    validation = manager.validate(manifest["backup_id"])
    assert validation["ok"] is False
    assert "unexpected:memory/unexpected.txt" in validation["errors"]


@pytest.mark.parametrize("unsafe_path", ["../escape.mv2", "/absolute.mv2", "memory/../escape.mv2"])
def test_memory_backup_rejects_unsafe_and_duplicate_manifest_targets(tmp_path: Path, unsafe_path: str) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir)
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)
    manifest = manager.create()
    manifest_path = backups / manifest["backup_id"] / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate = dict(payload["files"][0])
    payload["files"].append(duplicate)
    payload["files"][0]["path"] = unsafe_path
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    validation = manager.validate(manifest["backup_id"])

    assert validation["ok"] is False
    assert any(error.startswith(("invalid_path:", "duplicate:")) for error in validation["errors"])
    with pytest.raises(MemoryBackupError):
        manager.restore(manifest["backup_id"])


def test_memory_backup_rejects_hard_linked_backup_payload(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir)
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)
    manifest = manager.create()
    payload_path = backups / manifest["backup_id"] / str(manifest["files"][0]["path"])
    external_link = tmp_path / "external-hardlink"
    external_link.hardlink_to(payload_path)

    validation = manager.validate(manifest["backup_id"])

    assert validation["ok"] is False
    assert any(error.startswith("hardlink:") for error in validation["errors"])


def test_failed_directory_swap_restores_the_entire_previous_memory_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir, suffix="backup")
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)
    manifest = manager.create()
    _seed_memory(memory_dir, suffix="live-before-failure")
    real_replace = memory_backup_module.os.replace

    def fail_staged_swap(source: Path | str, target: Path | str) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name.startswith(".memory.restore-") and target_path == memory_dir:
            raise OSError("injected staged swap failure")
        real_replace(source, target)

    monkeypatch.setattr(memory_backup_module.os, "replace", fail_staged_swap)

    with pytest.raises(OSError, match="injected staged swap failure"):
        manager.restore(manifest["backup_id"])

    for spec in DEFAULT_LAYER_SPECS.values():
        assert (memory_dir / spec.mv2_file).read_bytes() == f"{spec.layer.value}:live-before-failure".encode()
    assert not list(tmp_path.glob(".memory.restore-*"))
    assert not list(tmp_path.glob(".memory.rollback-*"))


def test_failed_post_swap_fsync_removes_new_memory_when_no_live_set_existed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = tmp_path / "memory"
    backups = tmp_path / "backups"
    _seed_memory(memory_dir, suffix="backup")
    manager = MemoryBackupManager(memory_dir=memory_dir, backup_root=backups)
    manifest = manager.create()
    shutil.rmtree(memory_dir)
    real_fsync_directory = memory_backup_module._fsync_directory

    def fail_parent_fsync(path: Path) -> None:
        if path == tmp_path and memory_dir.exists():
            raise OSError("injected parent fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(memory_backup_module, "_fsync_directory", fail_parent_fsync)

    with pytest.raises(OSError, match="injected parent fsync failure"):
        manager.restore(manifest["backup_id"])

    assert not memory_dir.exists()
    assert not list(tmp_path.glob(".memory.restore-*"))
