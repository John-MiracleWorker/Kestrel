from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from nested_memvid_agent import agent_backup as agent_backup_module
from nested_memvid_agent.agent_backup import AgentBackupManager
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayerSpec, load_layer_specs
from nested_memvid_agent.memory_backup import MemoryBackupError
from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.repair_integrity import (
    load_repair_artifact,
    write_repair_artifact,
)
from nested_memvid_agent.runtime_ownership import (
    RUNTIME_OWNERSHIP_ERROR,
    PrimaryRuntimeOwnership,
    RuntimeOwnershipError,
)


def _windows_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip("Windows junction creation is unavailable on this runner")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_agent_backup_rejects_configured_junction_before_resolving_it(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside-memory"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    junction = runtime / "memory"
    _windows_junction(junction, outside)

    try:
        with pytest.raises(MemoryBackupError, match="Windows reparse point"):
            AgentBackupManager(
                memory_dir=junction,
                state_path=runtime / "state" / "agent.db",
                backup_root=tmp_path / "backups",
            )
        assert sentinel.read_text(encoding="utf-8") == "untouched"
        assert tuple(path.name for path in outside.iterdir()) == ("sentinel.txt",)
    finally:
        if os.path.lexists(junction):
            os.rmdir(junction)


def _seed_memory(
    memory_dir: Path,
    value: str,
    *,
    specs: dict[MemoryLayer, LayerSpec] | None = None,
) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    for spec in (specs or DEFAULT_LAYER_SPECS).values():
        layer_path = memory_dir / spec.mv2_file
        layer_path.write_bytes(f"{spec.layer.value}:{value}".encode())
        layer_path.with_suffix(f"{layer_path.suffix}.records.json").write_text(
            json.dumps({"layer": spec.layer.value, "value": value}),
            encoding="utf-8",
        )


def _seed_state(state_path: Path, value: str) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # ``sqlite3.Connection.__exit__`` commits or rolls back but does not close
    # the native database handle. Windows will then correctly refuse the live
    # database swaps exercised by restore tests, so make fixture ownership
    # explicit instead of relying on cyclic garbage collection.
    with closing(sqlite3.connect(state_path)) as connection, connection:
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        assert journal_mode is not None and str(journal_mode[0]).lower() == "wal"
        connection.execute("CREATE TABLE IF NOT EXISTS snapshot_probe (value TEXT NOT NULL)")
        connection.execute("DELETE FROM snapshot_probe")
        connection.execute("INSERT INTO snapshot_probe(value) VALUES (?)", (value,))


def _state_value(state_path: Path) -> str:
    with closing(sqlite3.connect(state_path)) as connection, connection:
        row = connection.execute("SELECT value FROM snapshot_probe").fetchone()
    assert row is not None
    return str(row[0])


def test_sqlite_backup_helpers_close_connections_before_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        def __init__(self, value: tuple[str]) -> None:
            self._value = value

        def fetchone(self) -> tuple[str]:
            return self._value

    class _Connection:
        def __init__(self) -> None:
            self.closed = False

        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def backup(self, _target: object) -> None:
            return None

        def execute(self, statement: str) -> _Cursor:
            value = ("delete",) if "journal_mode=DELETE" in statement else ("ok",)
            return _Cursor(value)

        def close(self) -> None:
            self.closed = True

    connections: list[_Connection] = []

    def connect(*_args: object, **_kwargs: object) -> _Connection:
        connection = _Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(agent_backup_module.sqlite3, "connect", connect)

    agent_backup_module._snapshot_sqlite(tmp_path / "state.db", tmp_path / "snapshot.db")
    agent_backup_module._verify_sqlite(tmp_path / "snapshot.db")

    assert len(connections) == 3
    assert all(connection.closed for connection in connections)


def test_backup_hash_and_copy_use_binary_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "payload.bin"
    payload = b"literal\nwindows\r\nbytes\x1a\x00"
    source.write_bytes(payload)
    target = tmp_path / "stage" / "payload.bin"
    synthetic_binary_flag = 1 << 29
    native_binary_flag = getattr(os, "O_BINARY", 0)
    real_open = os.open
    observed_flags: list[int] = []

    def open_without_synthetic_flag(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        path_text = os.fspath(path)
        if path_text == source.name or Path(path_text) in {source, target}:
            observed_flags.append(flags)
        native_flags = (flags & ~synthetic_binary_flag) | native_binary_flag
        return real_open(path, native_flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        agent_backup_module.os,
        "O_BINARY",
        synthetic_binary_flag,
        raising=False,
    )
    monkeypatch.setattr(agent_backup_module.os, "open", open_without_synthetic_flag)

    identity, size, digest = agent_backup_module._inspect_backup_source(source)
    agent_backup_module._copy_validated_backup_source(
        source,
        target,
        source_root=source_root,
        expected_identity=identity,
        manifest_entry={"size": size, "sha256": digest},
        target_mode=0o600,
    )

    assert target.read_bytes() == payload
    assert len(observed_flags) == 3
    assert all(flags & synthetic_binary_flag for flags in observed_flags)


def _manager(
    tmp_path: Path,
    *,
    specs: dict[MemoryLayer, LayerSpec] | None = None,
    include_repair_integrity: bool = False,
) -> tuple[AgentBackupManager, dict[str, Path]]:
    runtime = tmp_path / ".nest"
    paths = {
        "runtime": runtime,
        "memory": runtime / "memory",
        "state": runtime / "state" / "agent.db",
        "runs": runtime / "runs",
        "skills": runtime / "skills",
        "plugins": runtime / "plugins",
        "mcp": runtime / "config" / "mcp_servers.json",
        "channels": runtime / "config" / "channels.json",
        "settings": runtime / "config" / "runtime_settings.json",
        "layers": runtime / "config" / "layers.json",
        "backups": tmp_path / "agent-backups",
    }
    return (
        AgentBackupManager(
            memory_dir=paths["memory"],
            state_path=paths["state"],
            backup_root=paths["backups"],
            runs_dir=paths["runs"],
            skills_dir=paths["skills"],
            plugins_dir=paths["plugins"],
            mcp_config_path=paths["mcp"],
            channel_config_path=paths["channels"],
            runtime_settings_path=paths["settings"],
            layer_config_path=paths["layers"],
            repair_artifact_root=paths["runtime"] if include_repair_integrity else None,
            specs=specs,
        ),
        paths,
    )


def _seed_runtime(paths: dict[str, Path], value: str) -> None:
    _seed_memory(paths["memory"], value)
    _seed_state(paths["state"], value)
    for key, relative in (
        ("runs", "run-1/complete.mv2"),
        ("skills", "sample/SKILL.md"),
        ("plugins", "sample/plugin.json"),
    ):
        path = paths[key] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{key}:{value}", encoding="utf-8")
    for key in ("mcp", "channels", "settings"):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
        paths[key].write_text(json.dumps({"value": value}), encoding="utf-8")
    paths["layers"].parent.mkdir(parents=True, exist_ok=True)
    paths["layers"].write_text(
        json.dumps(
            {
                layer.value: {"mv2_file": spec.mv2_file}
                for layer, spec in DEFAULT_LAYER_SPECS.items()
            }
        ),
        encoding="utf-8",
    )


def _assert_seeded_runtime(paths: dict[str, Path], value: str) -> None:
    assert _state_value(paths["state"]) == value
    for spec in DEFAULT_LAYER_SPECS.values():
        layer_path = paths["memory"] / spec.mv2_file
        assert layer_path.read_bytes() == f"{spec.layer.value}:{value}".encode()
        assert (
            json.loads(
                layer_path.with_suffix(f"{layer_path.suffix}.records.json").read_text(
                    encoding="utf-8"
                )
            )["value"]
            == value
        )
    for key, relative in (
        ("runs", "run-1/complete.mv2"),
        ("skills", "sample/SKILL.md"),
        ("plugins", "sample/plugin.json"),
    ):
        assert (paths[key] / relative).read_text(encoding="utf-8") == f"{key}:{value}"
    for key in ("mcp", "channels", "settings"):
        assert json.loads(paths[key].read_text(encoding="utf-8")) == {"value": value}


def test_agent_backup_restores_repair_signing_identity_and_receipts(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path, include_repair_integrity=True)
    _seed_runtime(paths, "repair-integrity")
    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    artifact_id = "repair_validation_agent_backup"
    write_repair_artifact(
        tmp_path,
        "repair_validations",
        artifact_id,
        {
            "schema_version": 1,
            "validation_id": artifact_id,
            "success": True,
        },
    )
    key_path = paths["runtime"] / "repair_receipt_signing.v2.key"
    original_key = key_path.read_bytes()

    manifest = manager.create()
    key_path.unlink()
    shutil.rmtree(paths["runtime"] / "repair_validations")
    restored = manager.restore(str(manifest["backup_id"]))

    assert "repair_signing_key" in restored["restored_components"]
    assert "repair_validations" in restored["restored_components"]
    assert key_path.read_bytes() == original_key
    receipt = load_repair_artifact(
        tmp_path,
        collection="repair_validations",
        artifact_id=artifact_id,
        expected_prefix="repair_validation_",
        id_field="validation_id",
    )
    assert receipt["success"] is True
    assert manifest["components"]["repair_signing_key"]["present"] is True
    assert manifest["components"]["repair_validations"]["present"] is True


def test_agent_restore_legacy_backup_removes_live_repair_trust_material(
    tmp_path: Path,
) -> None:
    legacy_manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "legacy")
    legacy_manifest = legacy_manager.create()
    assert "repair_signing_key" not in legacy_manifest["components"]

    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    write_repair_artifact(
        tmp_path,
        "repair_validations",
        "repair_live_only_validation",
        {
            "schema_version": 1,
            "validation_id": "repair_live_only_validation",
            "success": True,
        },
    )
    key_path = paths["runtime"] / "repair_receipt_signing.v2.key"
    validations_path = paths["runtime"] / "repair_validations"
    assert key_path.is_file()
    assert validations_path.is_dir()

    full_manager, _ = _manager(tmp_path, include_repair_integrity=True)
    validation = full_manager.validate(str(legacy_manifest["backup_id"]))
    assert validation["ok"] is True
    assert validation["migration_warnings"]

    restored = full_manager.restore(str(legacy_manifest["backup_id"]))

    assert restored["migration_warnings"]
    assert {"repair_signing_key", "repair_validations"}.issubset(restored["removed_components"])
    assert not key_path.exists()
    assert not validations_path.exists()


def test_agent_backup_round_trip_restores_memory_state_capsules_config_and_extensions(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "original")
    secret_path = paths["runtime"] / "secrets" / "local_vault.json"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text('{"raw":"do-not-copy"}', encoding="utf-8")

    manifest = manager.create(retain=4)
    validation = manager.validate(str(manifest["backup_id"]))

    assert validation["ok"] is True
    assert manifest["complete"] is True
    assert "secret_broker_raw_values" in manifest["excluded"]
    assert not list((paths["backups"] / str(manifest["backup_id"])).rglob("*vault*"))
    state_snapshot = (
        paths["backups"] / str(manifest["backup_id"]) / "components" / "state" / "agent.db"
    )
    with closing(sqlite3.connect(state_snapshot)) as connection, connection:
        snapshot_mode = connection.execute("PRAGMA journal_mode").fetchone()
    assert snapshot_mode is not None and str(snapshot_mode[0]).lower() == "delete"
    assert not state_snapshot.with_name("agent.db-wal").exists()
    assert not state_snapshot.with_name("agent.db-shm").exists()

    _seed_runtime(paths, "changed")
    secret_path.write_text('{"raw":"current-secret"}', encoding="utf-8")
    restored = manager.restore(str(manifest["backup_id"]), retain=4)

    assert restored["safety_backup_complete"] is True
    assert restored["secrets_restored"] is False
    assert set(restored["restored_components"]) == {
        "memory",
        "state",
        "runs",
        "skills",
        "plugins",
        "mcp_config",
        "channel_config",
        "runtime_settings",
        "layer_config",
    }
    assert _state_value(paths["state"]) == "original"
    assert (paths["runs"] / "run-1" / "complete.mv2").read_text() == "runs:original"
    assert (paths["skills"] / "sample" / "SKILL.md").read_text() == "skills:original"
    assert (paths["plugins"] / "sample" / "plugin.json").read_text() == "plugins:original"
    assert json.loads(paths["settings"].read_text()) == {"value": "original"}
    assert {layer: spec.mv2_file for layer, spec in load_layer_specs(paths["layers"]).items()} == {
        layer: spec.mv2_file for layer, spec in DEFAULT_LAYER_SPECS.items()
    }
    assert secret_path.read_text() == '{"raw":"current-secret"}'
    for spec in DEFAULT_LAYER_SPECS.values():
        assert (paths["memory"] / spec.mv2_file).read_bytes() == (
            f"{spec.layer.value}:original".encode()
        )


def test_agent_backup_create_reports_retention_failure_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "committed")

    def fail_prune(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected retention prune failure")

    monkeypatch.setattr(manager, "_prune_locked", fail_prune)

    created = manager.create(retain=1)

    assert (paths["backups"] / str(created["backup_id"]) / "manifest.json").is_file()
    assert manager.validate(str(created["backup_id"]))["ok"] is True
    assert created["maintenance_warnings"] == [
        {
            "code": "retention_prune_failed",
            "error_type": "OSError",
            "message": "injected retention prune failure",
        }
    ]


def test_agent_restore_reports_retention_failure_after_live_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=4)
    _seed_runtime(paths, "live")

    def fail_prune(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected retention prune failure")

    monkeypatch.setattr(manager, "_prune_locked", fail_prune)

    restored = manager.restore(str(manifest["backup_id"]), retain=4)

    assert restored["maintenance_warnings"] == [
        {
            "code": "retention_prune_failed",
            "error_type": "OSError",
            "message": "injected retention prune failure",
        }
    ]
    assert (paths["backups"] / str(restored["safety_backup_id"]) / "manifest.json").is_file()
    assert _state_value(paths["state"]) == "backup"
    for spec in DEFAULT_LAYER_SPECS.values():
        assert (paths["memory"] / spec.mv2_file).read_bytes() == (
            f"{spec.layer.value}:backup".encode()
        )


def test_agent_backup_and_restore_refuse_a_live_primary_owner_before_any_write(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    restore_manifest = manager.create(retain=6)
    _seed_runtime(paths, "live")
    backup_entries_before = sorted(path.name for path in paths["backups"].iterdir())
    live_memory_before = {
        path.name: path.read_bytes() for path in paths["memory"].iterdir() if path.is_file()
    }
    preflight_calls: list[str] = []
    verification_calls: list[str] = []
    ownership = PrimaryRuntimeOwnership(paths["state"])
    ownership.acquire()
    try:
        with pytest.raises(
            RuntimeOwnershipError,
            match=f"^{RUNTIME_OWNERSHIP_ERROR}$",
        ):
            manager.create(
                retain=6,
                preflight=lambda: preflight_calls.append("create"),
            )
        with pytest.raises(
            RuntimeOwnershipError,
            match=f"^{RUNTIME_OWNERSHIP_ERROR}$",
        ):
            manager.restore(
                str(restore_manifest["backup_id"]),
                retain=6,
                preflight=lambda: preflight_calls.append("restore"),
                verify_memory_staging=lambda *_: verification_calls.append("restore"),
            )

        assert preflight_calls == []
        assert verification_calls == []
        assert sorted(path.name for path in paths["backups"].iterdir()) == (backup_entries_before)
        assert _state_value(paths["state"]) == "live"
        assert {
            path.name: path.read_bytes() for path in paths["memory"].iterdir() if path.is_file()
        } == live_memory_before
        assert not list(paths["runtime"].rglob("*.restore-*.tmp"))
        assert not list(paths["runtime"].rglob("*.rollback-*.tmp"))
    finally:
        ownership.release()

    successor_manifest = manager.create(retain=6)
    assert successor_manifest["complete"] is True
    restored = manager.restore(str(restore_manifest["backup_id"]), retain=6)
    assert restored["backup_id"] == restore_manifest["backup_id"]
    assert _state_value(paths["state"]) == "backup"


def test_agent_backup_runtime_owner_releases_after_preflight_and_restore_failures(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=4)
    _seed_runtime(paths, "live")

    def assert_runtime_owned() -> None:
        contender = PrimaryRuntimeOwnership(paths["state"])
        with pytest.raises(
            RuntimeOwnershipError,
            match=f"^{RUNTIME_OWNERSHIP_ERROR}$",
        ):
            contender.acquire()

    def fail_preflight() -> None:
        assert_runtime_owned()
        raise RuntimeError("injected backup preflight failure")

    with pytest.raises(RuntimeError, match="injected backup preflight failure"):
        manager.create(preflight=fail_preflight)
    successor = PrimaryRuntimeOwnership(paths["state"])
    successor.acquire()
    successor.release()

    def fail_staging(*_args: object) -> None:
        assert_runtime_owned()
        raise RuntimeError("injected restore verification failure")

    with pytest.raises(RuntimeError, match="injected restore verification failure"):
        manager.restore(
            str(manifest["backup_id"]),
            retain=4,
            verify_memory_staging=fail_staging,
        )
    successor.acquire()
    successor.release()
    assert _state_value(paths["state"]) == "live"
    assert not list(paths["runtime"].rglob("*.restore-*.tmp"))
    assert not list(paths["runtime"].rglob("*.rollback-*.tmp"))


def test_agent_restore_reinstates_optional_component_absence(tmp_path: Path) -> None:
    manager, paths = _manager(tmp_path)
    _seed_memory(paths["memory"], "original")
    _seed_state(paths["state"], "original")
    manifest = manager.create(retain=4)
    later_plugin = paths["plugins"] / "later" / "plugin.json"
    later_plugin.parent.mkdir(parents=True)
    later_plugin.write_text('{"installed":"later"}', encoding="utf-8")

    restored = manager.restore(str(manifest["backup_id"]), retain=4)

    assert "plugins" in restored["removed_components"]
    assert not paths["plugins"].exists()


def test_agent_backup_rejects_component_symlinks_and_manifest_corruption(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "original")
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    skill_file = paths["skills"] / "sample" / "SKILL.md"
    skill_file.unlink()
    skill_file.symlink_to(outside)

    with pytest.raises(MemoryBackupError, match="contains a symlink"):
        manager.create()

    skill_file.unlink()
    skill_file.write_text("safe", encoding="utf-8")
    manifest = manager.create()
    backup_dir = paths["backups"] / str(manifest["backup_id"])
    state_backup = backup_dir / "components" / "state" / "agent.db"
    state_backup.write_bytes(b"corrupt")

    validation = manager.validate(str(manifest["backup_id"]))

    assert validation["ok"] is False
    assert any(
        error.startswith(("size:components/state/", "checksum:components/state/", "sqlite:"))
        for error in validation["errors"]
    )
    with pytest.raises(MemoryBackupError, match="validation failed"):
        manager.restore(str(manifest["backup_id"]))


def test_agent_restore_rejects_source_bytes_changed_immediately_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=4)
    backup_skill = (
        paths["backups"]
        / str(manifest["backup_id"])
        / "components"
        / "skills"
        / "sample"
        / "SKILL.md"
    )
    _seed_runtime(paths, "live")
    real_stage = manager._stage_component
    injected = False

    def change_source_before_stage(
        component: agent_backup_module.AgentBackupComponent,
        *,
        backup_dir: Path,
        entries: list[dict[str, object]],
        validated_sources: dict[str, agent_backup_module._ValidatedBackupSource],
        embedded_layer_config: bool = False,
    ) -> Path:
        nonlocal injected
        if component.name == "skills" and not injected:
            injected = True
            backup_skill.write_text("changed-after-validation", encoding="utf-8")
        return real_stage(
            component,
            backup_dir=backup_dir,
            entries=entries,
            validated_sources=validated_sources,
            embedded_layer_config=embedded_layer_config,
        )

    monkeypatch.setattr(manager, "_stage_component", change_source_before_stage)

    with pytest.raises(MemoryBackupError, match="changed after validation"):
        manager.restore(str(manifest["backup_id"]), retain=4)

    assert injected is True
    _assert_seeded_runtime(paths, "live")
    assert not list(paths["runtime"].rglob("*.restore-*.tmp"))
    assert not list(paths["runtime"].rglob("*.rollback-*.tmp"))


def test_agent_restore_rejects_source_symlink_substituted_immediately_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=4)
    backup_skill = (
        paths["backups"]
        / str(manifest["backup_id"])
        / "components"
        / "skills"
        / "sample"
        / "SKILL.md"
    )
    outside = tmp_path / "same-checksum-outside"
    outside.write_bytes(backup_skill.read_bytes())
    _seed_runtime(paths, "live")
    real_stage = manager._stage_component
    injected = False

    def substitute_symlink_before_stage(
        component: agent_backup_module.AgentBackupComponent,
        *,
        backup_dir: Path,
        entries: list[dict[str, object]],
        validated_sources: dict[str, agent_backup_module._ValidatedBackupSource],
        embedded_layer_config: bool = False,
    ) -> Path:
        nonlocal injected
        if component.name == "skills" and not injected:
            injected = True
            backup_skill.unlink()
            backup_skill.symlink_to(outside)
        return real_stage(
            component,
            backup_dir=backup_dir,
            entries=entries,
            validated_sources=validated_sources,
            embedded_layer_config=embedded_layer_config,
        )

    monkeypatch.setattr(manager, "_stage_component", substitute_symlink_before_stage)

    with pytest.raises(MemoryBackupError, match="not a regular file"):
        manager.restore(str(manifest["backup_id"]), retain=4)

    assert injected is True
    assert outside.read_text(encoding="utf-8") == "skills:backup"
    _assert_seeded_runtime(paths, "live")
    assert not list(paths["runtime"].rglob("*.restore-*.tmp"))
    assert not list(paths["runtime"].rglob("*.rollback-*.tmp"))


def test_agent_restore_rejects_undeclared_entry_added_to_staging(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=4)
    _seed_runtime(paths, "live")

    def add_undeclared_entry(
        memory_stage: Path,
        _layer_config_stage: Path | None,
        _layer_files: dict[MemoryLayer, str],
    ) -> None:
        (memory_stage / "undeclared.mv2").write_bytes(b"not-in-manifest")

    with pytest.raises(MemoryBackupError, match="do not match manifest"):
        manager.restore(
            str(manifest["backup_id"]),
            retain=4,
            verify_memory_staging=add_undeclared_entry,
        )

    _assert_seeded_runtime(paths, "live")
    assert not list(paths["runtime"].rglob("*.restore-*.tmp"))
    assert not list(paths["runtime"].rglob("*.rollback-*.tmp"))


def test_agent_restore_rejects_undeclared_nonprivate_directory_added_to_staging(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=4)
    _seed_runtime(paths, "live")

    def add_undeclared_directory(
        memory_stage: Path,
        _layer_config_stage: Path | None,
        _layer_files: dict[MemoryLayer, str],
    ) -> None:
        injected = memory_stage / "undeclared-empty"
        injected.mkdir(mode=0o700)
        injected.chmod(0o777)

    with pytest.raises(MemoryBackupError, match="directory is not private|do not match manifest"):
        manager.restore(
            str(manifest["backup_id"]),
            retain=4,
            verify_memory_staging=add_undeclared_directory,
        )

    _assert_seeded_runtime(paths, "live")
    assert not list(paths["runtime"].rglob("*.restore-*.tmp"))
    assert not list(paths["runtime"].rglob("*.rollback-*.tmp"))


def test_agent_restore_rolls_back_all_installed_components_when_a_later_swap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=4)
    _seed_runtime(paths, "live")
    real_replace = agent_backup_module.os.replace

    def fail_state_install(source: Path | str, target: Path | str) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name.startswith(".agent.db.restore-") and target_path == paths["state"]:
            raise OSError("injected state install failure")
        real_replace(source, target)

    monkeypatch.setattr(agent_backup_module.os, "replace", fail_state_install)

    with pytest.raises(OSError, match="injected state install failure"):
        manager.restore(str(manifest["backup_id"]), retain=4)

    assert _state_value(paths["state"]) == "live"
    for spec in DEFAULT_LAYER_SPECS.values():
        assert (paths["memory"] / spec.mv2_file).read_bytes() == (
            f"{spec.layer.value}:live".encode()
        )
    assert not list(paths["runtime"].rglob("*.restore-*.tmp"))
    assert not list(paths["runtime"].rglob("*.rollback-*.tmp"))


def test_agent_restore_rolls_back_installed_components_on_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=4)
    _seed_runtime(paths, "live")
    real_replace = agent_backup_module.os.replace

    def interrupt_state_install(source: Path | str, target: Path | str) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name.startswith(".agent.db.restore-") and target_path == paths["state"]:
            # Memory is installed before state in the component transaction.
            raise KeyboardInterrupt("injected interrupt after first install")
        real_replace(source, target)

    monkeypatch.setattr(agent_backup_module.os, "replace", interrupt_state_install)

    with pytest.raises(KeyboardInterrupt, match="injected interrupt after first install"):
        manager.restore(str(manifest["backup_id"]), retain=4)

    _assert_seeded_runtime(paths, "live")
    assert not list(paths["runtime"].rglob("*.restore-*.tmp"))
    assert not list(paths["runtime"].rglob("*.rollback-*.tmp"))


def test_agent_backup_validation_rejects_unknown_component_paths(tmp_path: Path) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "original")
    manifest = manager.create()
    manifest_path = paths["backups"] / str(manifest["backup_id"]) / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"].append({"path": "components/secrets/vault.json", "size": 0, "sha256": ""})
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    validation = manager.validate(str(manifest["backup_id"]))

    assert validation["ok"] is False
    assert any("Unknown agent backup component" in error for error in validation["errors"])


def test_agent_backup_rejects_symlinked_backup_id_alias(tmp_path: Path) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create()
    backup_id = str(manifest["backup_id"])
    alias = paths["backups"] / "alias"
    alias.symlink_to(paths["backups"] / backup_id, target_is_directory=True)

    with pytest.raises(MemoryBackupError, match="cannot be a symlink"):
        manager.validate("alias")
    with pytest.raises(MemoryBackupError, match="cannot be a symlink"):
        manager.restore("alias")


def test_agent_backup_validation_binds_manifest_id_to_requested_directory(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create()
    backup_id = str(manifest["backup_id"])
    manifest_path = paths["backups"] / backup_id / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["backup_id"] = "different-backup"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    validation = manager.validate(backup_id)

    assert validation["ok"] is False
    assert "backup_id_mismatch" in validation["errors"]
    with pytest.raises(MemoryBackupError, match="validation failed"):
        manager.restore(backup_id)


def test_agent_backup_deduplicates_canonical_memory_layer_config(tmp_path: Path) -> None:
    runtime = tmp_path / ".nest"
    manager = AgentBackupManager(
        memory_dir=runtime / "memory",
        state_path=runtime / "state" / "agent.db",
        backup_root=tmp_path / "backups",
        layer_config_path=runtime / "memory" / "layers.json",
    )

    assert "layer_config" not in {component.name for component in manager.components}


def test_agent_restore_materializes_embedded_layer_config_on_clean_host(
    tmp_path: Path,
) -> None:
    specs = {
        layer: replace(spec, mv2_file=f"portable-{layer.value}.mv2")
        for layer, spec in DEFAULT_LAYER_SPECS.items()
    }
    source = tmp_path / "source" / ".nest"
    target = tmp_path / "target" / ".nest"
    backup_root = tmp_path / "backups"
    canonical_source_config = source / "memory" / "layers.json"
    external_target_config = target / "config" / "layers.json"
    _seed_memory(source / "memory", "backup", specs=specs)
    _seed_state(source / "state" / "agent.db", "backup")
    canonical_source_config.write_text(
        json.dumps({layer.value: {"mv2_file": spec.mv2_file} for layer, spec in specs.items()}),
        encoding="utf-8",
    )
    source_manager = AgentBackupManager(
        memory_dir=source / "memory",
        state_path=source / "state" / "agent.db",
        backup_root=backup_root,
        runs_dir=source / "runs",
        skills_dir=source / "skills",
        plugins_dir=source / "plugins",
        mcp_config_path=source / "config" / "mcp.json",
        channel_config_path=source / "config" / "channels.json",
        runtime_settings_path=source / "config" / "runtime_settings.json",
        layer_config_path=canonical_source_config,
        specs=specs,
    )
    manifest = source_manager.create(retain=4)
    assert "layer_config" not in manifest["components"]

    target_manager = AgentBackupManager(
        memory_dir=target / "memory",
        state_path=target / "state" / "agent.db",
        backup_root=backup_root,
        runs_dir=target / "runs",
        skills_dir=target / "skills",
        plugins_dir=target / "plugins",
        mcp_config_path=target / "config" / "mcp.json",
        channel_config_path=target / "config" / "channels.json",
        runtime_settings_path=target / "config" / "runtime_settings.json",
        layer_config_path=external_target_config,
    )
    validation = target_manager.validate(str(manifest["backup_id"]))
    staged_configs: list[dict[MemoryLayer, str]] = []

    def verify_staging(
        memory_stage: Path,
        layer_config_stage: Path | None,
        layer_files: dict[MemoryLayer, str],
    ) -> None:
        assert layer_config_stage is not None
        configured_files = {
            layer: spec.mv2_file for layer, spec in load_layer_specs(layer_config_stage).items()
        }
        assert configured_files == layer_files
        assert all((memory_stage / filename).is_file() for filename in layer_files.values())
        staged_configs.append(configured_files)

    restored = target_manager.restore(
        str(manifest["backup_id"]),
        retain=4,
        verify_memory_staging=verify_staging,
    )

    expected_files = {layer: spec.mv2_file for layer, spec in specs.items()}
    assert validation["ok"] is True
    assert staged_configs == [expected_files]
    assert "layer_config" in restored["restored_components"]
    assert external_target_config.read_bytes() == canonical_source_config.read_bytes()
    assert _state_value(target / "state" / "agent.db") == "backup"
    for layer, filename in expected_files.items():
        assert (target / "memory" / filename).read_bytes() == (f"{layer.value}:backup".encode())


def test_agent_restore_uses_backup_layer_map_across_live_filename_drift(
    tmp_path: Path,
) -> None:
    old_specs = {
        layer: replace(spec, mv2_file=f"old-{layer.value}.mv2")
        for layer, spec in DEFAULT_LAYER_SPECS.items()
    }
    new_specs = {
        layer: replace(spec, mv2_file=f"new-{layer.value}.mv2")
        for layer, spec in DEFAULT_LAYER_SPECS.items()
    }
    manager, paths = _manager(tmp_path, specs=old_specs)
    _seed_memory(paths["memory"], "backup", specs=old_specs)
    _seed_state(paths["state"], "backup")
    paths["layers"].parent.mkdir(parents=True, exist_ok=True)
    paths["layers"].write_text(
        json.dumps({layer.value: {"mv2_file": spec.mv2_file} for layer, spec in old_specs.items()}),
        encoding="utf-8",
    )
    manifest = manager.create(retain=4)

    shutil.rmtree(paths["memory"])
    _seed_memory(paths["memory"], "live", specs=new_specs)
    _seed_state(paths["state"], "live")
    paths["layers"].write_text(
        json.dumps({layer.value: {"mv2_file": spec.mv2_file} for layer, spec in new_specs.items()}),
        encoding="utf-8",
    )
    drifted_manager, _ = _manager(tmp_path, specs=new_specs)
    observed: dict[MemoryLayer, str] = {}

    def verify_staging(
        memory_stage: Path,
        layer_config_stage: Path | None,
        layer_files: dict[MemoryLayer, str],
    ) -> None:
        assert layer_config_stage is not None
        staged_specs = load_layer_specs(layer_config_stage)
        assert {layer: spec.mv2_file for layer, spec in staged_specs.items()} == layer_files
        assert all((memory_stage / filename).is_file() for filename in layer_files.values())
        observed.update(layer_files)

    restored = drifted_manager.restore(
        str(manifest["backup_id"]),
        retain=4,
        verify_memory_staging=verify_staging,
    )

    assert restored["backup_id"] == manifest["backup_id"]
    assert observed == {layer: spec.mv2_file for layer, spec in old_specs.items()}
    assert _state_value(paths["state"]) == "backup"
    assert not any((paths["memory"] / spec.mv2_file).exists() for spec in new_specs.values())
    assert {
        layer: spec.mv2_file for layer, spec in load_layer_specs(paths["layers"]).items()
    } == observed


def test_agent_backup_validation_handles_null_size_and_missing_optional_metadata(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_memory(paths["memory"], "backup")
    _seed_state(paths["state"], "backup")
    manifest = manager.create()
    manifest_path = paths["backups"] / str(manifest["backup_id"]) / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"][0]["size"] = None
    payload["components"].pop("plugins")
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    validation = manager.validate(str(manifest["backup_id"]))

    assert validation["ok"] is False
    assert any("int()" in error or "NoneType" in error for error in validation["errors"])
    assert "missing_component:plugins" in validation["errors"]


def test_agent_backup_round_trip_preserves_only_owner_executable_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    executable = paths["skills"] / "sample" / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(executable, 0o755)
    ordinary = paths["plugins"] / "sample" / "plugin.json"
    os.chmod(ordinary, 0o666)
    manifest = manager.create(retain=4)
    manifest_modes = {entry["path"]: entry["mode"] for entry in manifest["files"]}
    # Windows cannot express a POSIX execute bit through chmod/stat. Preserve
    # it when the source platform exposes the intent and otherwise fail closed
    # to a non-executable canonical mode for a future POSIX restore.
    expected_executable_mode = 0o700 if executable.stat().st_mode & stat.S_IXUSR else 0o600
    assert manifest_modes["components/skills/sample/run.sh"] == expected_executable_mode
    assert manifest_modes["components/plugins/sample/plugin.json"] == 0o600
    assert set(manifest_modes.values()) <= {0o600, 0o700}

    applied_modes: dict[str, int] = {}
    apply_private_file_mode = agent_backup_module._apply_private_file_mode

    def record_applied_mode(path: Path, mode: int, *, descriptor: int) -> None:
        if path.name in {"run.sh", "plugin.json"}:
            applied_modes[path.name] = mode
        apply_private_file_mode(path, mode, descriptor=descriptor)

    monkeypatch.setattr(agent_backup_module, "_apply_private_file_mode", record_applied_mode)

    os.chmod(executable, 0o600)
    manager.restore(str(manifest["backup_id"]), retain=4)

    # Bind restore to the canonical manifest intent even on platforms whose
    # st_mode cannot materialize it. Exact POSIX modes remain mandatory where
    # the runtime declares that it can enforce them.
    assert applied_modes == {
        "run.sh": expected_executable_mode,
        "plugin.json": 0o600,
    }
    if agent_backup_module._ENFORCE_EXACT_POSIX_MODES:
        assert stat.S_IMODE(executable.stat().st_mode) == expected_executable_mode
        assert stat.S_IMODE(ordinary.stat().st_mode) == 0o600
    else:
        assert stat.S_ISREG(executable.lstat().st_mode)
        assert stat.S_ISREG(ordinary.lstat().st_mode)


def test_agent_backup_keeps_canonical_modes_when_platform_cannot_enforce_posix_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    executable = paths["skills"] / "sample" / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(executable, 0o755)
    ordinary = paths["plugins"] / "sample" / "plugin.json"
    os.chmod(ordinary, 0o666)
    real_chmod = os.chmod

    monkeypatch.setattr(agent_backup_module, "_ENFORCE_EXACT_POSIX_MODES", False)

    def limited_chmod(
        _path: Path | str,
        _mode: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del follow_symlinks

    monkeypatch.setattr(agent_backup_module.os, "chmod", limited_chmod)

    manifest = manager.create(retain=4)
    modes = {entry["path"]: entry["mode"] for entry in manifest["files"]}
    # Native Windows does not expose a POSIX execute bit for this source file.
    # Preserve intent when the source can express it, and conservatively leave
    # an unexpressed intent disabled.
    expected_executable_mode = 0o700 if executable.stat().st_mode & stat.S_IXUSR else 0o600
    assert modes["components/skills/sample/run.sh"] == expected_executable_mode
    assert modes["components/plugins/sample/plugin.json"] == 0o600
    assert set(modes.values()) <= {0o600, 0o700}

    _seed_runtime(paths, "live")

    def expose_non_posix_directory_mode(
        memory_stage: Path,
        _layer_config_stage: Path | None,
        _layer_files: dict[MemoryLayer, str],
    ) -> None:
        real_chmod(memory_stage, 0o755)

    manager.restore(
        str(manifest["backup_id"]),
        retain=4,
        verify_memory_staging=expose_non_posix_directory_mode,
    )

    _assert_seeded_runtime(paths, "backup")
    assert executable.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_agent_restore_reports_and_preserves_artifacts_when_rollback_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=5)
    _seed_runtime(paths, "live")
    real_replace = agent_backup_module.os.replace

    def fail_install_and_memory_rollback(source: Path | str, target: Path | str) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name.startswith(".agent.db.restore-") and target_path == paths["state"]:
            raise OSError("injected install failure")
        if source_path.name.startswith(".memory.rollback-") and target_path == paths["memory"]:
            raise KeyboardInterrupt("injected rollback interrupt")
        real_replace(source, target)

    monkeypatch.setattr(
        agent_backup_module.os,
        "replace",
        fail_install_and_memory_rollback,
    )

    with pytest.raises(MemoryBackupError) as exc_info:
        manager.restore(str(manifest["backup_id"]), retain=5)

    message = str(exc_info.value)
    assert "safety_backup_id=" in message
    assert "injected rollback interrupt" in message
    preserved = list(paths["runtime"].rglob("*.rollback-*.tmp"))
    assert preserved
    assert any(path.name.startswith(".memory.rollback-") for path in preserved)
    safety_id = message.split("safety_backup_id=", 1)[1].split(";", 1)[0]
    assert (paths["backups"] / safety_id / "manifest.json").is_file()
    assert _state_value(paths["state"]) == "live"


def test_agent_restore_rollback_never_removes_an_unmoved_sqlite_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    manifest = manager.create(retain=4)
    _seed_runtime(paths, "live")
    live_state_bytes: bytes | None = None
    wal_path = paths["state"].with_name(paths["state"].name + "-wal")
    real_replace = agent_backup_module.os.replace

    def fail_after_main_move(source: Path | str, target: Path | str) -> None:
        nonlocal live_state_bytes
        source_path = Path(source)
        target_path = Path(target)
        if source_path == paths["state"] and ".rollback-" in target_path.name:
            # The pre-restore SQLite safety snapshot may legitimately checkpoint
            # WAL pages. Bind the assertion to the exact main database moved into
            # rollback, not to an earlier representation of the same live state.
            live_state_bytes = source_path.read_bytes()
            real_replace(source, target)
            wal_path.write_bytes(b"untouched-live-wal")
            return
        if source_path == wal_path and ".rollback-" in target_path.name:
            raise OSError("injected WAL move failure")
        real_replace(source, target)

    monkeypatch.setattr(agent_backup_module.os, "replace", fail_after_main_move)

    with pytest.raises(OSError, match="injected WAL move failure"):
        manager.restore(str(manifest["backup_id"]), retain=4)

    assert wal_path.read_bytes() == b"untouched-live-wal"
    assert live_state_bytes is not None
    assert paths["state"].read_bytes() == live_state_bytes


def test_agent_backup_pruning_never_deletes_unmanaged_directories(tmp_path: Path) -> None:
    manager, paths = _manager(tmp_path)
    _seed_runtime(paths, "backup")
    unrelated = paths["backups"] / "important-user-folder"
    unrelated.mkdir(parents=True)
    (unrelated / "manifest.json").write_text(
        json.dumps(
            {
                "schema": manager.schema,
                "backup_id": "different-name",
            }
        ),
        encoding="utf-8",
    )

    manager.create(retain=1)
    manager.create(retain=1)

    assert unrelated.is_dir()
    assert (unrelated / "manifest.json").is_file()


def test_agent_backup_keeps_shared_root_mode_and_rejects_symlink_lock(
    tmp_path: Path,
) -> None:
    manager, paths = _manager(tmp_path)
    paths["backups"].mkdir(mode=0o755)

    assert manager.list_backups() == []
    if os.name != "nt":
        assert stat.S_IMODE(paths["backups"].stat().st_mode) == 0o755

    lock_target = tmp_path / "lock-target"
    lock_target.write_text("do not touch", encoding="utf-8")
    os.chmod(lock_target, 0o644)
    manager.lock_path.symlink_to(lock_target)

    with pytest.raises(MemoryBackupError, match="backup lock"):
        manager.validate("missing")

    assert lock_target.read_text(encoding="utf-8") == "do not touch"
    if os.name != "nt":
        assert stat.S_IMODE(lock_target.stat().st_mode) == 0o644


def test_agent_backup_resolves_symlinked_parent_before_overlap_check(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "runtime" / "memory"
    memory_dir.mkdir(parents=True)
    parent_alias = tmp_path / "memory-alias"
    parent_alias.symlink_to(memory_dir, target_is_directory=True)

    expected = "Windows reparse point" if os.name == "nt" else "must not overlap"
    with pytest.raises(MemoryBackupError, match=expected):
        AgentBackupManager(
            memory_dir=memory_dir,
            state_path=tmp_path / "runtime" / "state" / "agent.db",
            backup_root=parent_alias / "backups",
        )
