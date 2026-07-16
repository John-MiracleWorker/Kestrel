from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .file_lock import lock_exclusive, unlock
from .layers import DEFAULT_LAYER_SPECS, LayerSpec
from .models import MemoryLayer


class MemoryBackupError(RuntimeError):
    pass


class MemoryBackupManager:
    """Atomic, checksummed backup/restore for closed Memvid v2 layer files."""

    def __init__(
        self,
        *,
        memory_dir: Path,
        backup_root: Path,
        specs: dict[MemoryLayer, LayerSpec] | None = None,
    ) -> None:
        self.memory_dir = memory_dir.resolve()
        self.backup_root = backup_root.resolve()
        if self.backup_root == self.memory_dir or self.backup_root.is_relative_to(
            self.memory_dir
        ) or self.memory_dir.is_relative_to(self.backup_root):
            raise MemoryBackupError("Memory and backup directories must not overlap")
        self.specs = specs or DEFAULT_LAYER_SPECS
        self.lock_path = self.backup_root / ".memory-backup.lock"

    def create(self, *, retain: int = 7) -> dict[str, Any]:
        with self._operation_lock():
            files = self._source_files(require_layers=True)
            backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + f"_{uuid4().hex[:8]}"
            temporary = self.backup_root / f".{backup_id}.tmp"
            destination = self.backup_root / backup_id
            temporary.mkdir(parents=True, mode=0o700)
            entries: list[dict[str, Any]] = []
            try:
                for source in files:
                    relative = source.relative_to(self.memory_dir)
                    target = temporary / "memory" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    os.chmod(target, 0o600)
                    _fsync_file(target)
                    entries.append(_file_entry(target, relative=Path("memory") / relative))
                manifest = {
                    "schema": "kestrel.memory_backup.v1",
                    "backup_id": backup_id,
                    "created_at": datetime.now(UTC).isoformat(),
                    "layers": {layer.value: spec.mv2_file for layer, spec in self.specs.items()},
                    "files": entries,
                }
                _fsync_directory(temporary / "memory")
                _write_json_atomic(temporary / "manifest.json", manifest)
                os.replace(temporary, destination)
                _fsync_directory(self.backup_root)
                self._prune_locked(retain=max(1, retain))
                return manifest
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise

    def validate(self, backup_id: str) -> dict[str, Any]:
        with self._operation_lock():
            return self._validate_locked(backup_id)

    def restore(
        self,
        backup_id: str,
        *,
        retain: int = 7,
        verify_staging: Callable[[Path], None] | None = None,
    ) -> dict[str, Any]:
        with self._operation_lock():
            validation = self._validate_locked(backup_id)
            if not validation["ok"]:
                raise MemoryBackupError(f"Backup validation failed: {validation['errors']}")
            safety_backup = self._create_safety_backup_locked()
            backup_dir = self._backup_dir(backup_id)
            manifest = validation["manifest"]
            parent = self.memory_dir.parent
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            staging_dir = parent / f".{self.memory_dir.name}.restore-{uuid4().hex}.tmp"
            rollback_dir = parent / f".{self.memory_dir.name}.rollback-{uuid4().hex}.tmp"
            staging_dir.mkdir(mode=0o700)
            restored_files = 0
            live_moved = False
            staging_installed = False
            try:
                for entry in manifest["files"]:
                    relative = _safe_manifest_path(str(entry["path"]))
                    if not relative.parts or relative.parts[0] != "memory":
                        raise MemoryBackupError("Backup contains a non-memory path")
                    source = backup_dir / relative
                    target = staging_dir / Path(*relative.parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    os.chmod(target, 0o600)
                    _fsync_file(target)
                    restored_files += 1
                if verify_staging is not None:
                    verify_staging(staging_dir)
                _fsync_directory(staging_dir)
                if self.memory_dir.exists():
                    os.replace(self.memory_dir, rollback_dir)
                    live_moved = True
                os.replace(staging_dir, self.memory_dir)
                staging_installed = True
                _fsync_directory(parent)
            except Exception:
                if live_moved and rollback_dir.exists():
                    if self.memory_dir.exists():
                        shutil.rmtree(self.memory_dir, ignore_errors=True)
                    os.replace(rollback_dir, self.memory_dir)
                    _fsync_directory(parent)
                elif staging_installed and self.memory_dir.exists():
                    shutil.rmtree(self.memory_dir, ignore_errors=True)
                    _fsync_directory(parent)
                raise
            finally:
                shutil.rmtree(staging_dir, ignore_errors=True)
                shutil.rmtree(rollback_dir, ignore_errors=True)
            preserved = {backup_id}
            if safety_backup is not None:
                preserved.add(str(safety_backup["backup_id"]))
            self._prune_locked(retain=max(2, retain), preserve=preserved)
            return {
                "schema": "kestrel.memory_restore.v1",
                "backup_id": backup_id,
                "safety_backup_id": None if safety_backup is None else safety_backup["backup_id"],
                "safety_backup_restorable": bool(
                    safety_backup is not None and safety_backup.get("complete", False)
                ),
                "restored_files": restored_files,
            }

    def list_backups(self) -> list[dict[str, Any]]:
        self.backup_root.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        for path in sorted(self.backup_root.iterdir(), reverse=True):
            if not path.is_dir() or path.name.startswith("."):
                continue
            try:
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            rows.append(manifest)
        return rows

    def _create_safety_backup_locked(self) -> dict[str, Any] | None:
        # The operation lock is already held; perform the create body without reacquiring it.
        files = self._source_files(require_layers=False)
        if not files:
            return None
        expected_layers = {(self.memory_dir / spec.mv2_file).resolve() for spec in self.specs.values()}
        complete = expected_layers.issubset(set(files))
        backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + f"_pre_restore_{uuid4().hex[:8]}"
        temporary = self.backup_root / f".{backup_id}.tmp"
        destination = self.backup_root / backup_id
        temporary.mkdir(parents=True, mode=0o700)
        entries: list[dict[str, Any]] = []
        try:
            for source in files:
                relative = source.relative_to(self.memory_dir)
                target = temporary / "memory" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                os.chmod(target, 0o600)
                _fsync_file(target)
                entries.append(_file_entry(target, relative=Path("memory") / relative))
            manifest = {
                "schema": "kestrel.memory_backup.v1",
                "backup_id": backup_id,
                "created_at": datetime.now(UTC).isoformat(),
                "kind": "pre_restore",
                "complete": complete,
                "layers": {layer.value: spec.mv2_file for layer, spec in self.specs.items()},
                "files": entries,
            }
            _fsync_directory(temporary / "memory")
            _write_json_atomic(temporary / "manifest.json", manifest)
            os.replace(temporary, destination)
            _fsync_directory(self.backup_root)
            return manifest
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _validate_locked(self, backup_id: str) -> dict[str, Any]:
        backup_dir = self._backup_dir(backup_id)
        manifest_path = backup_dir / "manifest.json"
        errors: list[str] = []
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "backup_id": backup_id, "errors": [f"manifest_unreadable:{type(exc).__name__}"]}
        if manifest.get("schema") != "kestrel.memory_backup.v1":
            errors.append("unsupported_manifest_schema")
        files = manifest.get("files")
        if not isinstance(files, list):
            errors.append("manifest_files_invalid")
            files = []
        allowed_files = self._allowed_manifest_files()
        seen_files: set[str] = set()
        for entry in files:
            if not isinstance(entry, dict):
                errors.append("manifest_entry_invalid")
                continue
            try:
                relative = _safe_manifest_path(str(entry["path"]))
                relative_name = relative.as_posix()
                if relative_name in seen_files:
                    errors.append(f"duplicate:{relative_name}")
                    continue
                seen_files.add(relative_name)
                if relative_name not in allowed_files:
                    errors.append(f"unexpected:{relative_name}")
                    continue
                path = backup_dir / relative
                if path.is_symlink() or not path.is_file():
                    errors.append(f"missing:{relative.as_posix()}")
                    continue
                if path.stat().st_nlink != 1:
                    errors.append(f"hardlink:{relative.as_posix()}")
                    continue
                if not path.resolve().is_relative_to(backup_dir):
                    errors.append(f"escape:{relative.as_posix()}")
                    continue
                if path.stat().st_size != int(entry.get("size", -1)):
                    errors.append(f"size:{relative.as_posix()}")
                actual = _sha256(path)
                if actual != str(entry.get("sha256", "")):
                    errors.append(f"checksum:{relative.as_posix()}")
            except (KeyError, MemoryBackupError) as exc:
                errors.append(f"invalid_path:{exc}")
        expected_layers = {f"memory/{spec.mv2_file}" for spec in self.specs.values()}
        listed = {str(entry.get("path", "")) for entry in files if isinstance(entry, dict)}
        for missing in sorted(expected_layers - listed):
            errors.append(f"missing_layer:{missing}")
        return {"ok": not errors, "backup_id": backup_id, "errors": errors, "manifest": manifest}

    def _source_files(self, *, require_layers: bool) -> list[Path]:
        files: list[Path] = []
        for spec in self.specs.values():
            candidate = self.memory_dir / spec.mv2_file
            if candidate.is_symlink():
                raise MemoryBackupError(f"Memvid layer cannot be a symlink: {candidate.name}")
            layer_path = candidate.resolve()
            if layer_path.parent != self.memory_dir:
                raise MemoryBackupError(f"Layer path escapes memory directory: {spec.mv2_file}")
            if require_layers and not layer_path.is_file():
                raise MemoryBackupError(f"Missing Memvid layer: {layer_path.name}")
            if layer_path.is_file():
                if layer_path.stat().st_nlink != 1:
                    raise MemoryBackupError(f"Memvid layer cannot be hard-linked: {layer_path.name}")
                files.append(layer_path)
            sidecar = layer_path.with_suffix(f"{layer_path.suffix}.records.json")
            if sidecar.is_symlink():
                raise MemoryBackupError(f"Memvid sidecar cannot be a symlink: {sidecar.name}")
            if sidecar.is_file():
                if sidecar.stat().st_nlink != 1:
                    raise MemoryBackupError(f"Memvid sidecar cannot be hard-linked: {sidecar.name}")
                files.append(sidecar)
        layer_config = self.memory_dir / "layers.json"
        if layer_config.is_symlink():
            raise MemoryBackupError("Memvid layer configuration cannot be a symlink")
        if layer_config.is_file():
            if layer_config.stat().st_nlink != 1:
                raise MemoryBackupError("Memvid layer configuration cannot be hard-linked")
            files.append(layer_config)
        return sorted(set(files))

    def _allowed_manifest_files(self) -> set[str]:
        allowed = {"memory/layers.json"}
        for spec in self.specs.values():
            allowed.add(f"memory/{spec.mv2_file}")
            allowed.add(f"memory/{spec.mv2_file}.records.json")
        return allowed

    def _backup_dir(self, backup_id: str) -> Path:
        if not backup_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for character in backup_id):
            raise MemoryBackupError("Invalid backup id")
        path = (self.backup_root / backup_id).resolve()
        if path.parent != self.backup_root:
            raise MemoryBackupError("Backup path escapes backup root")
        return path

    def _prune_locked(self, *, retain: int, preserve: set[str] | None = None) -> None:
        backups = [path for path in self.backup_root.iterdir() if path.is_dir() and not path.name.startswith(".")]
        kept = set(preserve or ())
        for backup in sorted(backups, reverse=True):
            if len(kept) >= retain:
                break
            kept.add(backup.name)
        for old in backups:
            if old.name not in kept:
                shutil.rmtree(old)

    @contextmanager
    def _operation_lock(self) -> Iterator[None]:
        self.memory_dir.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        memory_lock_path = self.memory_dir.parent / f".{self.memory_dir.name}.kestrel-memory.lock"
        memory_lock_path.touch(mode=0o600, exist_ok=True)
        os.chmod(memory_lock_path, 0o600)
        self.backup_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.backup_root, 0o700)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.lock_path, 0o600)
        with memory_lock_path.open("r+") as memory_handle, self.lock_path.open("r+") as handle:
            memory_locked = False
            backup_locked = False
            try:
                try:
                    lock_exclusive(memory_handle, blocking=False)
                    memory_locked = True
                except OSError as exc:
                    raise MemoryBackupError(
                        "Memvid memory is active; stop runs and retry the backup or restore"
                    ) from exc
                lock_exclusive(handle)
                backup_locked = True
                yield
            finally:
                if backup_locked:
                    unlock(handle)
                if memory_locked:
                    unlock(memory_handle)


def _file_entry(path: Path, *, relative: Path) -> dict[str, Any]:
    return {"path": relative.as_posix(), "size": path.stat().st_size, "sha256": _sha256(path)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_manifest_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise MemoryBackupError(f"Unsafe manifest path: {value}")
    return path


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
