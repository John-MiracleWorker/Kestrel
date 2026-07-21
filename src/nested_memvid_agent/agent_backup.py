from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .file_lock import lock_exclusive, unlock
from .layers import DEFAULT_LAYER_SPECS, LayerSpec, load_layer_specs
from .memory_backup import (
    MemoryBackupError,
    MemoryBackupManager,
    _file_entry,
    _fsync_directory,
    _fsync_file,
    _run_retention_maintenance,
    _safe_manifest_path,
    _write_json_atomic,
)
from .models import MemoryLayer
from .runtime_ownership import PrimaryRuntimeOwnership

AgentBackupKind = Literal["directory", "file", "sqlite"]
_EMBEDDED_LAYER_CONFIG_PATH = "components/memory/layers.json"
_LEGACY_ABSENT_REPAIR_COMPONENTS = frozenset(
    {"repair_signing_key", "repair_validations", "repair_reviews"}
)
# Native Windows exposes only a small subset of POSIX permission semantics:
# ``chmod`` can toggle read-only state but cannot reliably materialize or report
# exact 0600/0700 modes.  Manifests still carry those canonical intended modes
# so a later restore onto POSIX can enforce them.
_ENFORCE_EXACT_POSIX_MODES = os.name != "nt"


@dataclass(frozen=True)
class AgentBackupComponent:
    name: str
    path: Path
    kind: AgentBackupKind
    required: bool = False


@dataclass(frozen=True)
class _ValidatedBackupSource:
    """Identity of the exact regular file inspected during validation."""

    device: int
    inode: int
    mode: int
    links: int
    size: int
    modified_ns: int
    changed_ns: int


class AgentBackupManager:
    """Checksummed backup/restore for Kestrel memory and control-plane identity.

    The Secret Broker vault and operational logs are intentionally excluded. Raw
    secrets need a separately encrypted/keychain-backed recovery story; copying
    them into an ordinary backup would weaken the broker boundary.
    """

    schema = "kestrel.agent_backup.v1"

    def __init__(
        self,
        *,
        memory_dir: Path,
        state_path: Path,
        backup_root: Path,
        runs_dir: Path | None = None,
        skills_dir: Path | None = None,
        plugins_dir: Path | None = None,
        mcp_config_path: Path | None = None,
        channel_config_path: Path | None = None,
        runtime_settings_path: Path | None = None,
        layer_config_path: Path | None = None,
        repair_artifact_root: Path | None = None,
        specs: dict[MemoryLayer, LayerSpec] | None = None,
    ) -> None:
        self.memory_dir = memory_dir.resolve()
        self.state_path = state_path.resolve()
        # Keep the final path component unresolved so a symlink supplied as the
        # backup root can be rejected instead of silently following it.
        normalized_backup_root = Path(os.path.abspath(backup_root))
        self.backup_root = normalized_backup_root.parent.resolve() / normalized_backup_root.name
        self.specs = specs or DEFAULT_LAYER_SPECS
        self.components = self._build_components(
            runs_dir=runs_dir,
            skills_dir=skills_dir,
            plugins_dir=plugins_dir,
            mcp_config_path=mcp_config_path,
            channel_config_path=channel_config_path,
            runtime_settings_path=runtime_settings_path,
            layer_config_path=layer_config_path,
            repair_artifact_root=repair_artifact_root,
        )
        self._component_by_name = {component.name: component for component in self.components}
        self.lock_path = self.backup_root / ".agent-backup.lock"
        self._validate_layout()

    def create(
        self,
        *,
        retain: int = 7,
        preflight: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        with self._runtime_operation():
            if preflight is not None:
                preflight()
            with self._operation_lock():
                manifest = self._create_locked(kind="manual", require_required=True)
                response = dict(manifest)
                response["maintenance_warnings"] = _run_retention_maintenance(
                    lambda: self._prune_locked(
                        retain=max(1, retain),
                        preserve={str(manifest["backup_id"])},
                    )
                )
                return response

    def validate(self, backup_id: str) -> dict[str, Any]:
        with self._operation_lock():
            return self._validate_locked(backup_id)

    def list_backups(self) -> list[dict[str, Any]]:
        self._ensure_backup_root()
        rows: list[dict[str, Any]] = []
        for path in sorted(self.backup_root.iterdir(), reverse=True):
            if path.is_symlink() or not path.is_dir() or path.name.startswith("."):
                continue
            try:
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if (
                isinstance(manifest, dict)
                and manifest.get("schema") == self.schema
                and manifest.get("backup_id") == path.name
            ):
                rows.append(manifest)
        return rows

    def restore(
        self,
        backup_id: str,
        *,
        retain: int = 7,
        preflight: Callable[[], None] | None = None,
        verify_memory_staging: Callable[[Path, Path | None, dict[MemoryLayer, str]], None]
        | None = None,
    ) -> dict[str, Any]:
        with self._runtime_operation():
            if preflight is not None:
                preflight()
            return self._restore_owned(
                backup_id,
                retain=retain,
                verify_memory_staging=verify_memory_staging,
            )

    def _restore_owned(
        self,
        backup_id: str,
        *,
        retain: int,
        verify_memory_staging: Callable[[Path, Path | None, dict[MemoryLayer, str]], None] | None,
    ) -> dict[str, Any]:
        with self._operation_lock():
            validation = self._validate_locked(backup_id, capture_sources=True)
            if not validation["ok"]:
                raise MemoryBackupError(f"Agent backup validation failed: {validation['errors']}")
            safety_backup = self._create_locked(kind="pre_restore", require_required=False)
            manifest = validation["manifest"]
            validated_sources = validation["_validated_sources"]
            backup_dir = self._backup_dir(backup_id)
            staged: dict[str, Path] = {}
            rollbacks: dict[str, list[tuple[Path, Path]]] = {}
            installed: list[str] = []
            removed: list[str] = []
            applied: list[str] = []
            touched: list[str] = []
            installed_destinations: set[Path] = set()
            restored_files = 0
            restore_succeeded = False
            try:
                for component in self.components:
                    metadata = manifest["components"].get(component.name)
                    embedded_layer_config = (
                        component.name == "layer_config"
                        and component.name not in manifest["components"]
                        and _has_embedded_layer_config(manifest)
                    )
                    if not embedded_layer_config and (
                        not isinstance(metadata, dict) or not bool(metadata.get("present"))
                    ):
                        continue
                    stage = self._stage_component(
                        component,
                        backup_dir=backup_dir,
                        entries=manifest["files"],
                        validated_sources=validated_sources,
                        embedded_layer_config=embedded_layer_config,
                    )
                    staged[component.name] = stage
                    restored_files += (
                        1 if embedded_layer_config else int(metadata.get("file_count", 0))
                    )
                    if component.kind == "sqlite":
                        _verify_sqlite(stage)

                if verify_memory_staging is not None:
                    memory_stage = staged.get("memory")
                    if memory_stage is None:
                        raise MemoryBackupError("Agent backup has no staged memory component")
                    verify_memory_staging(
                        memory_stage,
                        staged.get("layer_config"),
                        _manifest_layer_files(manifest.get("memory_layers")),
                    )

                # The optional verifier is allowed to inspect staged memory but
                # not to alter the transaction.  Revalidate every stage after it
                # returns, while no live component has been moved yet.
                for component in self.components:
                    stage_path = staged.get(component.name)
                    if stage_path is None:
                        continue
                    self._verify_staged_component(
                        component,
                        stage=stage_path,
                        entries=manifest["files"],
                        embedded_layer_config=(
                            component.name == "layer_config"
                            and component.name not in manifest["components"]
                            and _has_embedded_layer_config(manifest)
                        ),
                    )

                for component in self.components:
                    metadata = manifest["components"].get(component.name)
                    if not isinstance(metadata, dict) and not (
                        component.name == "layer_config"
                        and component.name not in manifest["components"]
                        and _has_embedded_layer_config(manifest)
                    ):
                        continue
                    stage_path = staged.get(component.name)
                    component.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    rollback_token = uuid4().hex
                    component_rollbacks: list[tuple[Path, Path]] = []
                    rollbacks[component.name] = component_rollbacks
                    live_component_paths = _live_component_paths(component)
                    for live_path in live_component_paths:
                        if live_path.is_symlink():
                            raise MemoryBackupError(
                                f"Refusing to replace symlinked component: {component.name}"
                            )
                    for live_path in live_component_paths:
                        if not (live_path.exists() or live_path.is_symlink()):
                            continue
                        rollback_path = live_path.with_name(
                            f".{live_path.name}.rollback-{rollback_token}.tmp"
                        )
                        os.replace(live_path, rollback_path)
                        component_rollbacks.append((live_path, rollback_path))
                        if component.name not in touched:
                            touched.append(component.name)
                    if not component_rollbacks:
                        rollbacks.pop(component.name)
                    if stage_path is not None:
                        if component.name not in touched:
                            touched.append(component.name)
                        os.replace(stage_path, component.path)
                        installed_destinations.add(component.path)
                        installed.append(component.name)
                    elif component.name in rollbacks:
                        removed.append(component.name)
                    applied.append(component.name)
                    _fsync_directory(component.path.parent)
                restore_succeeded = True
            except BaseException as exc:
                rollback_errors = self._rollback_touched_components(
                    touched=touched,
                    rollbacks=rollbacks,
                    installed_destinations=installed_destinations,
                )
                if rollback_errors:
                    preserved_rollbacks = sorted(
                        str(rollback_path)
                        for component_rollbacks in rollbacks.values()
                        for _, rollback_path in component_rollbacks
                        if rollback_path.exists() or rollback_path.is_symlink()
                    )
                    details = "; ".join(rollback_errors)
                    preserved_paths_text = ", ".join(preserved_rollbacks) or "none"
                    raise MemoryBackupError(
                        "Agent restore failed and rollback was incomplete; "
                        f"safety_backup_id={safety_backup['backup_id']}; "
                        "rollback_errors="
                        f"{details}; preserved_rollback_paths={preserved_paths_text}"
                    ) from exc
                raise
            finally:
                for stage in staged.values():
                    _remove_path(stage, ignore_errors=True)
                if restore_succeeded:
                    for component_rollbacks in rollbacks.values():
                        for _, rollback_path in component_rollbacks:
                            _remove_path(rollback_path, ignore_errors=True)

            preserved = {backup_id, str(safety_backup["backup_id"])}
            return {
                "schema": "kestrel.agent_restore.v1",
                "backup_id": backup_id,
                "safety_backup_id": safety_backup["backup_id"],
                "safety_backup_complete": bool(safety_backup.get("complete")),
                "migration_warnings": list(validation.get("migration_warnings", [])),
                "restored_components": installed,
                "removed_components": removed,
                "restored_files": restored_files,
                "secrets_restored": False,
                "maintenance_warnings": _run_retention_maintenance(
                    lambda: self._prune_locked(
                        retain=max(2, retain),
                        preserve=preserved,
                    )
                ),
            }

    @contextmanager
    def _runtime_operation(self) -> Iterator[None]:
        """Exclude a live primary runtime for the full backup transaction."""

        ownership = PrimaryRuntimeOwnership(self.state_path)
        try:
            ownership.acquire()
            yield
        finally:
            ownership.release()

    def _rollback_touched_components(
        self,
        *,
        touched: list[str],
        rollbacks: dict[str, list[tuple[Path, Path]]],
        installed_destinations: set[Path],
    ) -> list[str]:
        errors: list[str] = []
        for component_name in reversed(touched):
            component = self._component_by_name[component_name]
            component_rollbacks = rollbacks.get(component_name, [])
            for live_path in _live_component_paths(component):
                if live_path not in installed_destinations:
                    continue
                try:
                    _remove_path(live_path)
                except BaseException as rollback_exc:
                    errors.append(f"remove:{component_name}:{live_path.name}:{rollback_exc}")
            for live_path, rollback_path in component_rollbacks:
                if not (rollback_path.exists() or rollback_path.is_symlink()):
                    errors.append(f"missing:{component_name}:{rollback_path.name}")
                    continue
                if live_path.exists() or live_path.is_symlink():
                    errors.append(f"occupied:{component_name}:{live_path.name}")
                    continue
                try:
                    os.replace(rollback_path, live_path)
                except BaseException as rollback_exc:
                    errors.append(f"replace:{component_name}:{rollback_path.name}:{rollback_exc}")
            try:
                _fsync_directory(component.path.parent)
            except BaseException as rollback_exc:
                errors.append(f"fsync:{component_name}:{rollback_exc}")
        return errors

    def _build_components(
        self,
        *,
        runs_dir: Path | None,
        skills_dir: Path | None,
        plugins_dir: Path | None,
        mcp_config_path: Path | None,
        channel_config_path: Path | None,
        runtime_settings_path: Path | None,
        layer_config_path: Path | None,
        repair_artifact_root: Path | None,
    ) -> tuple[AgentBackupComponent, ...]:
        if (
            layer_config_path is not None
            and layer_config_path.resolve() == self.memory_dir / "layers.json"
        ):
            # The canonical in-memory-directory layer contract is already copied
            # and validated as part of the memory component.
            layer_config_path = None
        components = [
            AgentBackupComponent("memory", self.memory_dir, "directory", required=True),
            AgentBackupComponent("state", self.state_path, "sqlite", required=True),
        ]
        optional: tuple[tuple[str, Path | None, AgentBackupKind], ...] = (
            ("runs", runs_dir, "directory"),
            ("skills", skills_dir, "directory"),
            ("plugins", plugins_dir, "directory"),
            ("mcp_config", mcp_config_path, "file"),
            ("channel_config", channel_config_path, "file"),
            ("runtime_settings", runtime_settings_path, "file"),
            ("layer_config", layer_config_path, "file"),
            (
                "repair_signing_key",
                None
                if repair_artifact_root is None
                else repair_artifact_root / "repair_receipt_signing.v2.key",
                "file",
            ),
            (
                "repair_validations",
                None
                if repair_artifact_root is None
                else repair_artifact_root / "repair_validations",
                "directory",
            ),
            (
                "repair_reviews",
                None if repair_artifact_root is None else repair_artifact_root / "repair_reviews",
                "directory",
            ),
        )
        components.extend(
            AgentBackupComponent(name, path.resolve(), kind)
            for name, path, kind in optional
            if path is not None
        )
        return tuple(components)

    def _validate_layout(self) -> None:
        seen_paths: dict[Path, str] = {}
        for component in self.components:
            prior = seen_paths.get(component.path)
            if prior is not None:
                raise MemoryBackupError(
                    f"Agent backup components overlap: {prior} and {component.name}"
                )
            seen_paths[component.path] = component.name
            if component.kind == "directory" and (
                self.backup_root == component.path
                or self.backup_root.is_relative_to(component.path)
                or component.path.is_relative_to(self.backup_root)
            ):
                raise MemoryBackupError(
                    f"Backup root and component must not overlap: {component.name}"
                )
        for index, left in enumerate(self.components):
            for right in self.components[index + 1 :]:
                overlaps = (left.kind == "directory" and right.path.is_relative_to(left.path)) or (
                    right.kind == "directory" and left.path.is_relative_to(right.path)
                )
                if overlaps:
                    raise MemoryBackupError(
                        f"Agent backup components overlap: {left.name} and {right.name}"
                    )

    def _create_locked(self, *, kind: str, require_required: bool) -> dict[str, Any]:
        backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + f"_{uuid4().hex[:8]}"
        if kind == "pre_restore":
            backup_id += "_pre_restore"
        temporary = self.backup_root / f".{backup_id}.tmp"
        destination = self.backup_root / backup_id
        temporary.mkdir(parents=True, mode=0o700)
        entries: list[dict[str, Any]] = []
        component_metadata: dict[str, dict[str, Any]] = {}
        try:
            for component in self.components:
                present = (
                    component.path.is_file()
                    if component.kind != "directory"
                    else component.path.is_dir()
                )
                if component.required and require_required and not present:
                    raise MemoryBackupError(
                        f"Missing required agent backup component: {component.name}"
                    )
                before_count = len(entries)
                if present:
                    if component.name == "memory":
                        self._copy_memory_component(
                            temporary, entries, require_layers=require_required
                        )
                    elif component.kind == "directory":
                        self._copy_directory_component(component, temporary, entries)
                    elif component.kind == "sqlite":
                        self._copy_sqlite_component(component, temporary, entries)
                    else:
                        self._copy_file_component(component, temporary, entries)
                component_metadata[component.name] = {
                    "kind": component.kind,
                    "present": present,
                    "required": component.required,
                    "file_count": len(entries) - before_count,
                    "target_name": component.path.name,
                }
            complete = all(
                bool(component_metadata[item.name]["present"])
                for item in self.components
                if item.required
            )
            manifest = {
                "schema": self.schema,
                "backup_id": backup_id,
                "created_at": datetime.now(UTC).isoformat(),
                "kind": kind,
                "complete": complete,
                "memory_layers": {layer.value: spec.mv2_file for layer, spec in self.specs.items()},
                "components": component_metadata,
                "files": entries,
                "excluded": [
                    "secret_broker_raw_values",
                    "operational_logs",
                    "worker_worktrees",
                    "rebuildable_vector_sidecars_outside_memory",
                ],
            }
            _write_json_atomic(temporary / "manifest.json", manifest)
            os.replace(temporary, destination)
            _fsync_directory(self.backup_root)
            return manifest
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _copy_memory_component(
        self,
        temporary: Path,
        entries: list[dict[str, Any]],
        *,
        require_layers: bool,
    ) -> None:
        helper = MemoryBackupManager(
            memory_dir=self.memory_dir,
            backup_root=self.backup_root,
            specs=self.specs,
        )
        target_root = temporary / "components" / "memory"
        target_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        for source in helper._source_files(require_layers=require_layers):
            relative = source.relative_to(self.memory_dir)
            target = target_root / relative
            canonical_mode = _copy_private_file(source, target)
            entries.append(
                _agent_file_entry(
                    target,
                    relative=Path("components") / "memory" / relative,
                    canonical_mode=canonical_mode,
                )
            )
        _fsync_directory(target_root)

    def _copy_directory_component(
        self,
        component: AgentBackupComponent,
        temporary: Path,
        entries: list[dict[str, Any]],
    ) -> None:
        target_root = temporary / "components" / component.name
        target_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        for source in sorted(component.path.rglob("*")):
            relative = source.relative_to(component.path)
            if source.is_symlink():
                raise MemoryBackupError(
                    f"Agent backup component contains a symlink: {component.name}/{relative}"
                )
            target = target_root / relative
            if source.is_dir():
                target.mkdir(parents=True, mode=0o700, exist_ok=True)
                continue
            if not source.is_file():
                raise MemoryBackupError(
                    f"Agent backup component contains an unsupported entry: {component.name}/{relative}"
                )
            if source.stat().st_nlink != 1:
                raise MemoryBackupError(
                    f"Agent backup component contains a hard link: {component.name}/{relative}"
                )
            canonical_mode = _copy_private_file(
                source,
                target,
                preserve_owner_execute=True,
            )
            entries.append(
                _agent_file_entry(
                    target,
                    relative=Path("components") / component.name / relative,
                    canonical_mode=canonical_mode,
                )
            )
        _fsync_tree(target_root)

    def _copy_file_component(
        self,
        component: AgentBackupComponent,
        temporary: Path,
        entries: list[dict[str, Any]],
    ) -> None:
        _assert_regular_private_source(component)
        relative = Path("components") / component.name / component.path.name
        target = temporary / relative
        canonical_mode = _copy_private_file(component.path, target)
        entries.append(_agent_file_entry(target, relative=relative, canonical_mode=canonical_mode))

    def _copy_sqlite_component(
        self,
        component: AgentBackupComponent,
        temporary: Path,
        entries: list[dict[str, Any]],
    ) -> None:
        _assert_regular_private_source(component)
        relative = Path("components") / component.name / component.path.name
        target = temporary / relative
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _snapshot_sqlite(component.path, target)
        os.chmod(target, 0o600)
        _fsync_file(target)
        entries.append(_agent_file_entry(target, relative=relative, canonical_mode=0o600))

    def _validate_locked(
        self,
        backup_id: str,
        *,
        capture_sources: bool = False,
    ) -> dict[str, Any]:
        backup_dir = self._backup_dir(backup_id)
        try:
            manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return {
                "ok": False,
                "backup_id": backup_id,
                "errors": [f"manifest_unreadable:{type(exc).__name__}"],
            }
        errors: list[str] = []
        if not isinstance(manifest, dict) or manifest.get("schema") != self.schema:
            errors.append("unsupported_manifest_schema")
            manifest = manifest if isinstance(manifest, dict) else {}
        if backup_dir.name != backup_id or manifest.get("backup_id") != backup_id:
            errors.append("backup_id_mismatch")
        try:
            layer_files = _manifest_layer_files(manifest.get("memory_layers"))
        except MemoryBackupError as exc:
            errors.append(f"manifest_memory_layers_invalid:{exc}")
            layer_files = {}
        components = manifest.get("components")
        if not isinstance(components, dict):
            errors.append("manifest_components_invalid")
            components = {}
        # Agent backups created before repair-integrity artifacts joined the
        # full-backup contract have no metadata for these optional components.
        # Treat them as explicitly absent so restore removes any live signing
        # key/receipts instead of retaining mismatched trust material.  That is
        # a fail-closed migration: restored policy evidence must be revalidated.
        legacy_absent_components: list[str] = []
        for component_name in sorted(
            _LEGACY_ABSENT_REPAIR_COMPONENTS & set(self._component_by_name)
        ):
            if component_name in components:
                continue
            component = self._component_by_name[component_name]
            components[component_name] = {
                "kind": component.kind,
                "present": False,
                "required": component.required,
                "file_count": 0,
                "target_name": component.path.name,
            }
            legacy_absent_components.append(component_name)
        unknown_components = sorted(set(components) - set(self._component_by_name))
        errors.extend(f"unknown_component:{name}" for name in unknown_components)
        files = manifest.get("files")
        if not isinstance(files, list):
            errors.append("manifest_files_invalid")
            files = []
        seen: set[str] = set()
        validated_sources: dict[str, _ValidatedBackupSource] = {}
        counts: dict[str, int] = {name: 0 for name in self._component_by_name}
        for entry in files:
            if not isinstance(entry, dict):
                errors.append("manifest_entry_invalid")
                continue
            try:
                relative = _safe_manifest_path(str(entry["path"]))
                relative_name = relative.as_posix()
                if relative_name in seen:
                    errors.append(f"duplicate:{relative_name}")
                    continue
                seen.add(relative_name)
                component = self._component_for_manifest_path(relative)
                metadata = components.get(component.name)
                declared_target = (
                    metadata.get("target_name") if isinstance(metadata, dict) else None
                )
                self._validate_component_relative_path(
                    component,
                    relative,
                    declared_target=declared_target,
                    layer_files=layer_files,
                )
                path = backup_dir / relative
                if path.is_symlink() or not path.is_file():
                    errors.append(f"missing:{relative_name}")
                    continue
                if path.stat().st_nlink != 1:
                    errors.append(f"hardlink:{relative_name}")
                    continue
                if not path.resolve().is_relative_to(backup_dir):
                    errors.append(f"escape:{relative_name}")
                    continue
                identity, copied_size, copied_sha256 = _inspect_backup_source(
                    path,
                    root=backup_dir,
                )
                if copied_size != int(entry.get("size", -1)):
                    errors.append(f"size:{relative_name}")
                if copied_sha256 != str(entry.get("sha256", "")):
                    errors.append(f"checksum:{relative_name}")
                validated_sources[relative_name] = identity
                _manifest_file_mode(entry)
                counts[component.name] += 1
            except (KeyError, MemoryBackupError, TypeError, ValueError) as exc:
                errors.append(f"invalid_path:{exc}")

        canonical_layer_config = _EMBEDDED_LAYER_CONFIG_PATH
        for component in self.components:
            metadata = components.get(component.name)
            if not isinstance(metadata, dict):
                if (
                    component.name == "layer_config"
                    and component.name not in components
                    and canonical_layer_config in seen
                ):
                    continue
                errors.append(f"missing_component:{component.name}")
                continue
            if not isinstance(metadata.get("present"), bool):
                errors.append(f"component_present_invalid:{component.name}")
            present = metadata.get("present") is True
            try:
                declared_count = int(metadata.get("file_count", -1))
            except (TypeError, ValueError):
                errors.append(f"component_count_invalid:{component.name}")
                declared_count = -1
            if declared_count != counts[component.name]:
                errors.append(f"component_count:{component.name}")
            if metadata.get("kind") != component.kind:
                errors.append(f"component_kind:{component.name}")
            try:
                _safe_component_target_name(metadata.get("target_name"))
            except MemoryBackupError:
                errors.append(f"component_target_invalid:{component.name}")
            if metadata.get("required") is not component.required:
                errors.append(f"component_required:{component.name}")
            if component.required and not present:
                errors.append(f"required_component_absent:{component.name}")
            if not present and counts[component.name] != 0:
                errors.append(f"absent_component_has_files:{component.name}")
            if present and component.kind in {"file", "sqlite"} and counts[component.name] != 1:
                errors.append(f"component_file_count:{component.name}")

        component_root = backup_dir / "components"
        actual_files: set[str] = set()
        if component_root.is_dir():
            for path in component_root.rglob("*"):
                relative_name = path.relative_to(backup_dir).as_posix()
                if path.is_symlink():
                    errors.append(f"symlink:{relative_name}")
                elif path.is_file():
                    actual_files.add(relative_name)
        for unexpected in sorted(actual_files - seen):
            errors.append(f"unlisted:{unexpected}")

        expected_layers = {f"components/memory/{mv2_file}" for mv2_file in layer_files.values()}
        for missing in sorted(expected_layers - seen):
            errors.append(f"missing_layer:{missing}")
        layer_config_candidates: list[tuple[str, Path]] = []
        if canonical_layer_config in seen:
            layer_config_candidates.append(
                (canonical_layer_config, backup_dir / canonical_layer_config)
            )
        external_layer_metadata = components.get("layer_config")
        if (
            isinstance(external_layer_metadata, dict)
            and external_layer_metadata.get("present") is True
        ):
            try:
                external_name = _safe_component_target_name(
                    external_layer_metadata.get("target_name")
                )
            except MemoryBackupError:
                external_name = ""
            if external_name:
                relative_name = f"components/layer_config/{external_name}"
                layer_config_candidates.append((relative_name, backup_dir / relative_name))
        for relative_name, config_path in layer_config_candidates:
            if any(
                error.startswith((f"checksum:{relative_name}", f"size:{relative_name}"))
                for error in errors
            ):
                continue
            try:
                config_specs = load_layer_specs(config_path)
            except (KeyError, OSError, TypeError, ValueError) as exc:
                errors.append(f"layer_config_invalid:{relative_name}:{type(exc).__name__}")
                continue
            configured_layer_files = {layer: spec.mv2_file for layer, spec in config_specs.items()}
            if configured_layer_files != layer_files:
                errors.append(f"layer_config_mismatch:{relative_name}")
        state_metadata = components.get("state")
        state_target = (
            state_metadata.get("target_name") if isinstance(state_metadata, dict) else None
        )
        try:
            state_name = _safe_component_target_name(state_target)
        except MemoryBackupError:
            state_name = self._component_by_name["state"].path.name
        state_backup_path = backup_dir / "components" / "state" / state_name
        if state_backup_path.is_file() and not any(
            error.startswith(("checksum:components/state/", "size:components/state/"))
            for error in errors
        ):
            try:
                _verify_sqlite(state_backup_path)
            except MemoryBackupError as exc:
                errors.append(f"sqlite:{exc}")
        result: dict[str, Any] = {
            "ok": not errors,
            "backup_id": backup_id,
            "errors": errors,
            "migration_warnings": (
                [
                    "legacy_backup_missing_repair_integrity_artifacts; "
                    "restore will remove live repair trust material and policy evidence "
                    "will fail closed until revalidated"
                ]
                if legacy_absent_components
                else []
            ),
            "manifest": manifest,
        }
        if capture_sources:
            result["_validated_sources"] = validated_sources
        return result

    def _component_for_manifest_path(self, relative: Path) -> AgentBackupComponent:
        if len(relative.parts) < 3 or relative.parts[0] != "components":
            raise MemoryBackupError(f"Unexpected agent backup path: {relative.as_posix()}")
        component = self._component_by_name.get(relative.parts[1])
        if component is None:
            raise MemoryBackupError(f"Unknown agent backup component: {relative.parts[1]}")
        return component

    def _validate_component_relative_path(
        self,
        component: AgentBackupComponent,
        relative: Path,
        *,
        declared_target: object,
        layer_files: dict[MemoryLayer, str],
    ) -> None:
        component_relative = Path(*relative.parts[2:])
        if component.kind in {"file", "sqlite"}:
            target_name = _safe_component_target_name(declared_target)
            if component_relative != Path(target_name):
                raise MemoryBackupError(
                    f"Unexpected file for component {component.name}: {component_relative.as_posix()}"
                )
            return
        if component.name != "memory":
            return
        allowed = {"layers.json", ".validation-integrity.key"}
        for mv2_file in layer_files.values():
            allowed.add(mv2_file)
            allowed.add(f"{mv2_file}.records.json")
        if component_relative.as_posix() not in allowed:
            raise MemoryBackupError(f"Unexpected memory file: {component_relative.as_posix()}")

    def _stage_component(
        self,
        component: AgentBackupComponent,
        *,
        backup_dir: Path,
        entries: list[dict[str, Any]],
        validated_sources: dict[str, _ValidatedBackupSource],
        embedded_layer_config: bool = False,
    ) -> Path:
        relevant = self._staging_entries(
            component,
            entries=entries,
            embedded_layer_config=embedded_layer_config,
        )
        component.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        stage = component.path.with_name(f".{component.path.name}.restore-{uuid4().hex}.tmp")
        try:
            if component.kind == "directory":
                stage.mkdir(mode=0o700)
                for source_relative, target_relative, mode, entry in relevant:
                    _copy_validated_backup_source(
                        backup_dir / source_relative,
                        stage / target_relative,
                        source_root=backup_dir,
                        expected_identity=validated_sources.get(source_relative.as_posix()),
                        manifest_entry=entry,
                        target_mode=mode,
                    )
                _fsync_tree(stage)
            else:
                if len(relevant) != 1:
                    raise MemoryBackupError(
                        f"Expected one backup file for component: {component.name}"
                    )
                source_relative, _target_relative, mode, entry = relevant[0]
                _copy_validated_backup_source(
                    backup_dir / source_relative,
                    stage,
                    source_root=backup_dir,
                    expected_identity=validated_sources.get(source_relative.as_posix()),
                    manifest_entry=entry,
                    target_mode=mode,
                )
            self._verify_staged_component(
                component,
                stage=stage,
                entries=entries,
                embedded_layer_config=embedded_layer_config,
            )
            return stage
        except Exception:
            _remove_path(stage, ignore_errors=True)
            raise

    def _staging_entries(
        self,
        component: AgentBackupComponent,
        *,
        entries: list[dict[str, Any]],
        embedded_layer_config: bool,
    ) -> list[tuple[Path, Path, int, dict[str, Any]]]:
        relevant: list[tuple[Path, Path, int, dict[str, Any]]] = []
        for entry in entries:
            relative = _safe_manifest_path(str(entry["path"]))
            if len(relative.parts) < 3 or relative.parts[1] != component.name:
                continue
            relevant.append(
                (
                    relative,
                    Path(*relative.parts[2:]),
                    _manifest_file_mode(entry),
                    entry,
                )
            )
        if embedded_layer_config:
            embedded_path = Path(_EMBEDDED_LAYER_CONFIG_PATH)
            embedded_entries = [
                entry
                for entry in entries
                if _safe_manifest_path(str(entry["path"])) == embedded_path
            ]
            if len(embedded_entries) != 1:
                raise MemoryBackupError("Expected one embedded layer configuration in agent backup")
            relevant.append(
                (
                    embedded_path,
                    Path(component.path.name),
                    _manifest_file_mode(embedded_entries[0]),
                    embedded_entries[0],
                )
            )
        return relevant

    def _verify_staged_component(
        self,
        component: AgentBackupComponent,
        *,
        stage: Path,
        entries: list[dict[str, Any]],
        embedded_layer_config: bool,
    ) -> None:
        relevant = self._staging_entries(
            component,
            entries=entries,
            embedded_layer_config=embedded_layer_config,
        )
        if component.kind != "directory":
            if len(relevant) != 1:
                raise MemoryBackupError(f"Expected one staged file for component: {component.name}")
            _verify_staged_file(stage, relevant[0][3])
            return

        expected = {target.as_posix(): entry for _, target, _, entry in relevant}
        expected_directories: set[str] = set()
        for relative_name in expected:
            parent = Path(relative_name).parent
            while parent != Path("."):
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        root_metadata = stage.lstat()
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or (_ENFORCE_EXACT_POSIX_MODES and stat.S_IMODE(root_metadata.st_mode) != 0o700)
        ):
            raise MemoryBackupError(
                f"Staged component root is not a private directory: {component.name}"
            )
        actual: set[str] = set()
        actual_directories: set[str] = set()
        for path in stage.rglob("*"):
            relative_name = path.relative_to(stage).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise MemoryBackupError(
                    f"Staged component contains a symlink: {component.name}/{relative_name}"
                )
            if stat.S_ISDIR(metadata.st_mode):
                if _ENFORCE_EXACT_POSIX_MODES and stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise MemoryBackupError(
                        f"Staged component directory is not private: "
                        f"{component.name}/{relative_name}"
                    )
                actual_directories.add(relative_name)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise MemoryBackupError(
                    f"Staged component contains an unsupported entry: "
                    f"{component.name}/{relative_name}"
                )
            actual.add(relative_name)
        undeclared = sorted(actual - set(expected))
        missing = sorted(set(expected) - actual)
        undeclared_directories = sorted(actual_directories - expected_directories)
        missing_directories = sorted(expected_directories - actual_directories)
        if undeclared or missing or undeclared_directories or missing_directories:
            raise MemoryBackupError(
                f"Staged component entries do not match manifest: {component.name}; "
                f"undeclared={undeclared}; missing={missing}; "
                f"undeclared_directories={undeclared_directories}; "
                f"missing_directories={missing_directories}"
            )
        for relative_name, entry in expected.items():
            _verify_staged_file(stage / relative_name, entry)

    def _backup_dir(self, backup_id: str) -> Path:
        if not backup_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in backup_id
        ):
            raise MemoryBackupError("Invalid backup id")
        candidate = self.backup_root / backup_id
        if candidate.is_symlink():
            raise MemoryBackupError("Agent backup directory cannot be a symlink")
        path = candidate.resolve()
        if path.parent != self.backup_root or path.name != backup_id:
            raise MemoryBackupError("Backup path escapes backup root")
        return path

    def _prune_locked(self, *, retain: int, preserve: set[str] | None = None) -> None:
        backups = self._managed_backup_directories()
        known_names = {path.name for path in backups}
        kept = set(preserve or ()) & known_names
        for backup in sorted(backups, reverse=True):
            if len(kept) >= retain:
                break
            kept.add(backup.name)
        for old in backups:
            if old.name not in kept:
                shutil.rmtree(old)

    def _managed_backup_directories(self) -> list[Path]:
        managed: list[Path] = []
        pattern = re.compile(r"\d{8}T\d{6}\.\d{6}Z_[0-9a-f]{8}(?:_pre_restore)?")
        for path in self.backup_root.iterdir():
            if path.is_symlink() or not path.is_dir() or pattern.fullmatch(path.name) is None:
                continue
            try:
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            if (
                isinstance(manifest, dict)
                and manifest.get("schema") == self.schema
                and manifest.get("backup_id") == path.name
            ):
                managed.append(path)
        return managed

    def _ensure_backup_root(self) -> None:
        if self.backup_root.exists() or self.backup_root.is_symlink():
            if self.backup_root.is_symlink() or not self.backup_root.is_dir():
                raise MemoryBackupError("Agent backup root must be a regular directory")
            return
        self.backup_root.mkdir(parents=True, mode=0o700)
        os.chmod(self.backup_root, 0o700)

    @contextmanager
    def _operation_lock(self) -> Iterator[None]:
        self.memory_dir.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        memory_lock_path = self.memory_dir.parent / f".{self.memory_dir.name}.kestrel-memory.lock"
        self._ensure_backup_root()
        with (
            _private_lock_handle(memory_lock_path) as memory_handle,
            _private_lock_handle(self.lock_path) as backup_handle,
        ):
            memory_locked = False
            backup_locked = False
            try:
                try:
                    lock_exclusive(memory_handle, blocking=False)
                    memory_locked = True
                except OSError as exc:
                    raise MemoryBackupError(
                        "Memvid memory is active; stop Kestrel before agent backup or restore"
                    ) from exc
                lock_exclusive(backup_handle)
                backup_locked = True
                yield
            finally:
                if backup_locked:
                    unlock(backup_handle)
                if memory_locked:
                    unlock(memory_handle)


@contextmanager
def _private_lock_handle(path: Path) -> Iterator[Any]:
    try:
        before_open = os.lstat(path)
    except FileNotFoundError:
        before_open = None
    except OSError as exc:
        raise MemoryBackupError(f"Unable to inspect agent backup lock: {path}") from exc
    if before_open is not None and (
        not stat.S_ISREG(before_open.st_mode) or before_open.st_nlink != 1
    ):
        raise MemoryBackupError(f"Agent backup lock must be a regular, singly linked file: {path}")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise MemoryBackupError(f"Unable to open safe agent backup lock: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        after_open = os.lstat(path)
        identity_changed = not os.path.samestat(metadata, after_open)
        replaced_existing = before_open is not None and not os.path.samestat(
            before_open,
            metadata,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not stat.S_ISREG(after_open.st_mode)
            or after_open.st_nlink != 1
            or identity_changed
            or replaced_existing
        ):
            raise MemoryBackupError(
                f"Agent backup lock must be a regular, singly linked file: {path}"
            )
        _apply_private_file_mode(path, 0o600, descriptor=descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise MemoryBackupError(f"Unable to open safe agent backup lock: {path}") from exc
    except Exception:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "r+") as handle:
        yield handle


def _manifest_layer_files(raw: object) -> dict[MemoryLayer, str]:
    if not isinstance(raw, dict):
        raise MemoryBackupError("memory_layers must be an object")
    expected_names = {layer.value for layer in MemoryLayer}
    if set(raw) != expected_names:
        raise MemoryBackupError("memory_layers must declare every layer exactly once")
    result: dict[MemoryLayer, str] = {}
    seen_files: set[str] = set()
    for layer in MemoryLayer:
        value = raw.get(layer.value)
        if not isinstance(value, str):
            raise MemoryBackupError(f"memory layer filename must be text: {layer.value}")
        path = _safe_manifest_path(value)
        if len(path.parts) != 1 or path.name != value or path.suffix != ".mv2":
            raise MemoryBackupError(f"invalid memory layer filename: {value}")
        if value in seen_files:
            raise MemoryBackupError(f"duplicate memory layer filename: {value}")
        seen_files.add(value)
        result[layer] = value
    return result


def _has_embedded_layer_config(manifest: dict[str, Any]) -> bool:
    files = manifest.get("files")
    return isinstance(files, list) and any(
        isinstance(entry, dict) and entry.get("path") == _EMBEDDED_LAYER_CONFIG_PATH
        for entry in files
    )


def _safe_component_target_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise MemoryBackupError("component target name must be text")
    path = _safe_manifest_path(raw)
    if len(path.parts) != 1 or path.name != raw:
        raise MemoryBackupError("component target name must be one filename")
    return raw


def _manifest_file_mode(entry: dict[str, Any]) -> int:
    raw = entry.get("mode")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw not in {0o600, 0o700}:
        raise MemoryBackupError("file mode must be 0600 or 0700")
    return raw


def _source_identity(metadata: os.stat_result) -> _ValidatedBackupSource:
    return _ValidatedBackupSource(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        links=metadata.st_nlink,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


@contextmanager
def _open_regular_beneath(
    root: Path,
    relative: Path,
) -> Iterator[tuple[int, _ValidatedBackupSource]]:
    """Open a regular file without following any path component on POSIX.

    CPython does not expose equivalent directory-relative handles on every
    supported Windows build. The fallback still binds every component and the
    final file before and after opening; POSIX uses openat-style ``dir_fd``
    traversal so a replaced parent cannot redirect the source open.
    """

    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise MemoryBackupError(f"Unsafe backup source path: {relative}")
    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        with _open_regular_beneath_fallback(root, relative) as opened:
            yield opened
        return

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_descriptors: list[int] = []
    directory_edges: list[tuple[int, str, int, _ValidatedBackupSource]] = []
    file_descriptor = -1
    root_identity: _ValidatedBackupSource | None = None
    file_identity: _ValidatedBackupSource | None = None
    leaf_name = relative.parts[-1]
    try:
        root_before = root.lstat()
        if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
            raise MemoryBackupError(f"Backup source root is not a real directory: {root}")
        root_descriptor = os.open(root, directory_flags)
        directory_descriptors.append(root_descriptor)
        root_opened = os.fstat(root_descriptor)
        root_identity = _source_identity(root_opened)
        if not stat.S_ISDIR(root_opened.st_mode) or root_identity != _source_identity(root_before):
            raise MemoryBackupError(f"Backup source root changed while opening: {root}")

        current_descriptor = root_descriptor
        for part in relative.parts[:-1]:
            visible = os.stat(part, dir_fd=current_descriptor, follow_symlinks=False)
            if stat.S_ISLNK(visible.st_mode) or not stat.S_ISDIR(visible.st_mode):
                raise MemoryBackupError(f"Backup source parent is not a real directory: {part}")
            child_descriptor = os.open(part, directory_flags, dir_fd=current_descriptor)
            child_opened = os.fstat(child_descriptor)
            child_identity = _source_identity(child_opened)
            if not stat.S_ISDIR(child_opened.st_mode) or child_identity != _source_identity(
                visible
            ):
                os.close(child_descriptor)
                raise MemoryBackupError(f"Backup source parent changed while opening: {part}")
            directory_descriptors.append(child_descriptor)
            directory_edges.append((current_descriptor, part, child_descriptor, child_identity))
            current_descriptor = child_descriptor

        visible_file = os.stat(
            leaf_name,
            dir_fd=current_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(visible_file.st_mode) or not stat.S_ISREG(visible_file.st_mode):
            raise MemoryBackupError(f"Backup source is not a regular file: {relative}")
        if visible_file.st_nlink != 1:
            raise MemoryBackupError(f"Backup source is hard-linked: {relative}")
        file_descriptor = os.open(leaf_name, file_flags, dir_fd=current_descriptor)
        file_opened = os.fstat(file_descriptor)
        file_identity = _source_identity(file_opened)
        if (
            not stat.S_ISREG(file_opened.st_mode)
            or file_opened.st_nlink != 1
            or file_identity != _source_identity(visible_file)
        ):
            raise MemoryBackupError(f"Backup source changed while opening: {relative}")

        yield file_descriptor, file_identity

        if root_identity != _source_identity(root.lstat()):
            raise MemoryBackupError(f"Backup source root changed while reading: {root}")
        for parent_descriptor, part, child_descriptor, child_identity in directory_edges:
            visible = os.stat(part, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                stat.S_ISLNK(visible.st_mode)
                or not stat.S_ISDIR(visible.st_mode)
                or _source_identity(visible) != child_identity
                or _source_identity(os.fstat(child_descriptor)) != child_identity
            ):
                raise MemoryBackupError(f"Backup source parent changed while reading: {part}")
        final_visible = os.stat(
            leaf_name,
            dir_fd=current_descriptor,
            follow_symlinks=False,
        )
        if (
            file_identity != _source_identity(os.fstat(file_descriptor))
            or file_identity != _source_identity(final_visible)
            or not stat.S_ISREG(final_visible.st_mode)
            or final_visible.st_nlink != 1
        ):
            raise MemoryBackupError(f"Backup source changed while reading: {relative}")
    except OSError as exc:
        raise MemoryBackupError(f"Unable to open stable backup source: {root / relative}") from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


@contextmanager
def _open_regular_beneath_fallback(
    root: Path,
    relative: Path,
) -> Iterator[tuple[int, _ValidatedBackupSource]]:
    directory_snapshots: list[tuple[Path, _ValidatedBackupSource]] = []
    current = root
    for part in relative.parts[:-1]:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise MemoryBackupError(f"Backup source parent is not a real directory: {current}")
        directory_snapshots.append((current, _source_identity(metadata)))
        current /= part
    parent_metadata = current.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise MemoryBackupError(f"Backup source parent is not a real directory: {current}")
    directory_snapshots.append((current, _source_identity(parent_metadata)))
    path = root / relative
    path_before = path.lstat()
    if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
        raise MemoryBackupError(f"Backup source is not a regular file: {path}")
    if path_before.st_nlink != 1:
        raise MemoryBackupError(f"Backup source is hard-linked: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        identity = _source_identity(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or identity != _source_identity(path_before)
        ):
            raise MemoryBackupError(f"Backup source changed while opening: {path}")
        yield descriptor, identity
        if identity != _source_identity(os.fstat(descriptor)) or identity != _source_identity(
            path.lstat()
        ):
            raise MemoryBackupError(f"Backup source changed while reading: {path}")
        for directory, expected in directory_snapshots:
            visible = directory.lstat()
            if (
                stat.S_ISLNK(visible.st_mode)
                or not stat.S_ISDIR(visible.st_mode)
                or _source_identity(visible) != expected
            ):
                raise MemoryBackupError(f"Backup source parent changed while reading: {directory}")
    finally:
        os.close(descriptor)


def _inspect_backup_source(
    path: Path,
    *,
    root: Path | None = None,
) -> tuple[_ValidatedBackupSource, int, str]:
    """Hash one stable, singly linked regular file through a no-follow descriptor."""

    try:
        source_root = path.parent if root is None else root
        relative = path.relative_to(source_root)
        with _open_regular_beneath(source_root, relative) as (descriptor, expected):
            digest = hashlib.sha256()
            copied_size = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                copied_size += len(chunk)
        return expected, copied_size, digest.hexdigest()
    except OSError as exc:
        raise MemoryBackupError(f"Unable to read stable backup source: {path}") from exc


def _copy_validated_backup_source(
    source: Path,
    target: Path,
    *,
    source_root: Path,
    expected_identity: _ValidatedBackupSource | None,
    manifest_entry: dict[str, Any],
    target_mode: int,
) -> None:
    """Copy and verify the exact source validated before the restore safety backup."""

    if expected_identity is None:
        raise MemoryBackupError(f"Backup source was not bound during validation: {source}")
    if target_mode not in {0o600, 0o700}:
        raise MemoryBackupError(f"Unsafe private restore mode: {oct(target_mode)}")
    try:
        expected_size = int(manifest_entry.get("size", -1))
        expected_sha256 = str(manifest_entry.get("sha256", ""))
    except (TypeError, ValueError) as exc:
        raise MemoryBackupError(f"Invalid manifest metadata for backup source: {source}") from exc

    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    target_descriptor = -1
    target_created = False
    copy_complete = False
    try:
        relative = source.relative_to(source_root)
        with _open_regular_beneath(source_root, relative) as (
            source_descriptor,
            opened_source,
        ):
            if opened_source != expected_identity:
                raise MemoryBackupError(f"Backup source changed after validation: {source}")

            target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            target_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            target_descriptor = os.open(target, target_flags, 0o600)
            target_created = True
            opened_target = os.fstat(target_descriptor)
            if not stat.S_ISREG(opened_target.st_mode) or opened_target.st_nlink != 1:
                raise MemoryBackupError(f"Staged restore target is not a regular file: {target}")

            digest = hashlib.sha256()
            copied_size = 0
            while chunk := os.read(source_descriptor, 1024 * 1024):
                digest.update(chunk)
                copied_size += len(chunk)
                pending = memoryview(chunk)
                while pending:
                    written = os.write(target_descriptor, pending)
                    if written <= 0:
                        raise OSError("short staged restore write")
                    pending = pending[written:]
        if copied_size != expected_size or digest.hexdigest() != expected_sha256:
            raise MemoryBackupError(f"Backup source content changed after validation: {source}")

        _apply_private_file_mode(target, target_mode, descriptor=target_descriptor)
        final_target = os.fstat(target_descriptor)
        if (
            not stat.S_ISREG(final_target.st_mode)
            or final_target.st_nlink != 1
            or final_target.st_size != copied_size
            or (_ENFORCE_EXACT_POSIX_MODES and stat.S_IMODE(final_target.st_mode) != target_mode)
        ):
            raise MemoryBackupError(f"Staged restore target changed while copying: {target}")
        os.fsync(target_descriptor)
        copy_complete = True
    except OSError as exc:
        raise MemoryBackupError(f"Unable to stage stable backup source: {source}") from exc
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if target_created and not copy_complete:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass


def _staged_file_matches_manifest(
    path: Path,
    entry: dict[str, Any],
    expected_mode: int,
) -> bool:
    try:
        identity, copied_size, copied_sha256 = _inspect_backup_source(path)
        return (
            identity.links == 1
            and (not _ENFORCE_EXACT_POSIX_MODES or stat.S_IMODE(identity.mode) == expected_mode)
            and copied_size == int(entry.get("size", -1))
            and copied_sha256 == str(entry.get("sha256", ""))
        )
    except (MemoryBackupError, OSError, TypeError, ValueError):
        return False


def _verify_staged_file(path: Path, entry: dict[str, Any]) -> None:
    expected_mode = _manifest_file_mode(entry)
    if not _staged_file_matches_manifest(path, entry, expected_mode):
        raise MemoryBackupError(f"Staged restore file does not match manifest: {path}")


def _agent_file_entry(
    path: Path,
    *,
    relative: Path,
    canonical_mode: int,
) -> dict[str, Any]:
    entry = _file_entry(path, relative=relative)
    if canonical_mode not in {0o600, 0o700}:
        raise MemoryBackupError(f"Unsafe private backup mode: {oct(canonical_mode)}")
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    if _ENFORCE_EXACT_POSIX_MODES and actual_mode != canonical_mode:
        raise MemoryBackupError(f"Unsafe private backup mode: {oct(actual_mode)}")
    entry["mode"] = canonical_mode
    return entry


def _assert_regular_private_source(component: AgentBackupComponent) -> None:
    if component.path.is_symlink() or not component.path.is_file():
        raise MemoryBackupError(f"Invalid agent backup component: {component.name}")
    if component.path.stat().st_nlink != 1:
        raise MemoryBackupError(f"Agent backup component cannot be hard-linked: {component.name}")


def _copy_private_file(
    source: Path,
    target: Path,
    *,
    preserve_owner_execute: bool = False,
    target_mode: int | None = None,
) -> int:
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    shutil.copy2(source, target)
    if target_mode is None:
        target_mode = (
            0o700 if preserve_owner_execute and source.stat().st_mode & stat.S_IXUSR else 0o600
        )
    if target_mode not in {0o600, 0o700}:
        raise MemoryBackupError(f"Unsafe private restore mode: {oct(target_mode)}")
    os.chmod(target, target_mode)
    _fsync_file(target)
    return target_mode


def _apply_private_file_mode(
    path: Path,
    mode: int,
    *,
    descriptor: int,
) -> None:
    """Apply a private mode using the strongest native platform primitive."""

    if _ENFORCE_EXACT_POSIX_MODES:
        os.fchmod(descriptor, mode)
        return
    # Native Windows has no exact POSIX mode model and may not expose fchmod.
    # chmod still clears a read-only attribute while the manifest retains the
    # canonical intended mode for a future POSIX restore.
    os.chmod(path, mode)


def _snapshot_sqlite(source: Path, target: Path) -> None:
    source_uri = source.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source_connection:
            with sqlite3.connect(target) as target_connection:
                source_connection.backup(target_connection)
                result = target_connection.execute("PRAGMA integrity_check").fetchone()
                if result is None or str(result[0]).lower() != "ok":
                    raise MemoryBackupError("SQLite snapshot failed integrity_check")
                # AgentStateStore intentionally runs in WAL mode. A SQLite backup
                # inherits that persistent database setting, which can otherwise
                # leave unmanifested -wal/-shm files beside the snapshot. Convert
                # the closed backup artifact into one self-contained database.
                target_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                journal_mode = target_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
                if journal_mode is None or str(journal_mode[0]).lower() != "delete":
                    raise MemoryBackupError("SQLite snapshot could not leave WAL journal mode")
    except sqlite3.Error as exc:
        raise MemoryBackupError(f"SQLite snapshot failed: {exc}") from exc
    sidecars = [target.with_name(target.name + suffix) for suffix in ("-wal", "-shm")]
    unexpected = [path.name for path in sidecars if path.exists()]
    if unexpected:
        raise MemoryBackupError("SQLite snapshot left journal sidecars: " + ", ".join(unexpected))


def _verify_sqlite(path: Path) -> None:
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise MemoryBackupError(f"SQLite backup is unreadable: {exc}") from exc
    if result is None or str(result[0]).lower() != "ok":
        raise MemoryBackupError("SQLite backup failed integrity_check")


def _fsync_tree(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for path in sorted(directories, reverse=True):
        _fsync_directory(path)
    _fsync_directory(root)


def _remove_path(path: Path, *, ignore_errors: bool = False) -> None:
    if path.is_symlink() or path.is_file():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if not ignore_errors:
                raise
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=ignore_errors)


def _live_component_paths(component: AgentBackupComponent) -> tuple[Path, ...]:
    if component.kind != "sqlite":
        return (component.path,)
    return (
        component.path,
        component.path.with_name(component.path.name + "-wal"),
        component.path.with_name(component.path.name + "-shm"),
    )
