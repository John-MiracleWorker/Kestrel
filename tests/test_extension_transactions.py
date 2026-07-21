from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

import nested_memvid_agent.extension_transaction as extension_transaction
import nested_memvid_agent.plugin_manager as plugin_manager_module
import nested_memvid_agent.state_store as state_store_module
import nested_memvid_agent.tools.builtin as builtin_tools_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.plugin_manager import GitHubPluginSource, PluginError, PluginManager
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import SkillInstallTool


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
def test_extension_copy_rejects_child_junction_without_touching_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    junction = source / "linked-child"
    _windows_junction(junction, outside)

    try:
        with pytest.raises(
            extension_transaction.ExtensionTransactionError,
            match="symbolic link|reparse point",
        ):
            extension_transaction.copy_regular_tree(source, destination)
        assert sentinel.read_text(encoding="utf-8") == "untouched"
        assert tuple(destination.iterdir()) == ()
    finally:
        if os.path.lexists(junction):
            os.rmdir(junction)


def test_extension_move_restores_source_when_post_publish_validation_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "sentinel.txt").write_text("exact", encoding="utf-8")
    destination = tmp_path / "destination"
    real_lstat = Path.lstat
    injected = False

    def fail_once(path: Path) -> os.stat_result:
        nonlocal injected
        if path == destination and os.path.lexists(path) and not injected:
            injected = True
            raise OSError("injected post-publication validation failure")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_once)

    with pytest.raises(OSError, match="injected post-publication"):
        extension_transaction._move_real_directory(source, destination)

    assert injected is True
    assert source.is_dir()
    assert (source / "sentinel.txt").read_text(encoding="utf-8") == "exact"
    assert os.path.lexists(destination) is False


class TransactionFetcher:
    def __init__(self, source: Path, commit: str = "a" * 40) -> None:
        self.source = source
        self.commit = commit

    def fetch(
        self,
        source: GitHubPluginSource,
        destination: Path,
        ref: str | None = None,
    ) -> str:
        del source, ref
        shutil.copytree(self.source, destination)
        return self.commit


def test_extension_tree_uses_full_lstat_instead_of_incomplete_direntry_metadata(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "manifest.json").write_text("{}\n", encoding="utf-8")
    (nested / "SKILL.md").write_text("Review input.\n", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()
    real_scandir = extension_transaction.os.scandir

    class EntryWithoutCompleteStat:
        def __init__(self, entry: os.DirEntry[str]) -> None:
            self.name = entry.name
            self.path = entry.path

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            del follow_symlinks
            raise AssertionError("DirEntry.stat metadata must not be trusted")

    class ScandirWithoutCompleteStat:
        def __init__(self, path: object) -> None:
            self._scanned = real_scandir(path)

        def __enter__(self) -> object:
            entries = self._scanned.__enter__()
            return iter(EntryWithoutCompleteStat(entry) for entry in entries)

        def __exit__(self, *args: object) -> object:
            return self._scanned.__exit__(*args)

    monkeypatch.setattr(
        extension_transaction.os,
        "scandir",
        lambda path: ScandirWithoutCompleteStat(path),
    )

    extension_transaction.fsync_tree(source)
    extension_transaction.copy_regular_tree(source, destination)

    assert (destination / "manifest.json").read_text(encoding="utf-8") == "{}\n"
    assert (destination / "nested" / "SKILL.md").read_text(encoding="utf-8") == (
        "Review input.\n"
    )


@pytest.mark.parametrize("failed_move", [1, 2])
def test_plugin_overwrite_move_failures_restore_exact_old_tree_and_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    failed_move: int,
) -> None:
    manager, state, fetcher = _installed_plugin(tmp_path)
    before_tree = _tree_snapshot(tmp_path / "plugins" / "demo")
    before_state = _plugin_state_snapshot(state, "demo")
    fetcher.source = _plugin_repo(tmp_path / "repo-v2", version="2.0.0")
    real_replace = extension_transaction.os.replace
    move_count = 0

    def fail_selected_move(source: object, destination: object, *args: Any, **kwargs: Any) -> None:
        nonlocal move_count
        source_path = Path(os.fspath(source))
        destination_path = Path(os.fspath(destination))
        if source_path.parent == tmp_path / "plugins" and destination_path.parent == source_path.parent:
            move_count += 1
            if move_count == failed_move:
                raise OSError("injected plugin directory move failure")
        real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(extension_transaction.os, "replace", fail_selected_move)

    with pytest.raises(OSError, match="injected plugin directory move failure"):
        manager.update("demo")

    assert _tree_snapshot(tmp_path / "plugins" / "demo") == before_tree
    assert _plugin_state_snapshot(state, "demo") == before_state
    _assert_no_extension_debris(tmp_path / "plugins")


def test_plugin_second_manifest_failure_never_reaches_live_tree(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    manager, state, fetcher = _installed_plugin(tmp_path)
    before_tree = _tree_snapshot(tmp_path / "plugins" / "demo")
    before_state = _plugin_state_snapshot(state, "demo")
    fetcher.source = _plugin_repo(tmp_path / "repo-v2", version="2.0.0")
    real_load = plugin_manager_module.load_plugin_manifest
    calls = 0

    def fail_second_read(path: Path) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PluginError("injected second manifest read failure")
        return real_load(path)

    monkeypatch.setattr(plugin_manager_module, "load_plugin_manifest", fail_second_read)

    with pytest.raises(PluginError, match="second manifest read failure"):
        manager.update("demo")

    assert _tree_snapshot(tmp_path / "plugins" / "demo") == before_tree
    assert _plugin_state_snapshot(state, "demo") == before_state
    _assert_no_extension_debris(tmp_path / "plugins")


def test_plugin_upsert_failure_restores_exact_old_tree_and_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    manager, state, fetcher = _installed_plugin(tmp_path)
    before_tree = _tree_snapshot(tmp_path / "plugins" / "demo")
    before_state = _plugin_state_snapshot(state, "demo")
    fetcher.source = _plugin_repo(tmp_path / "repo-v2", version="2.0.0")
    real_upsert = state_store_module._upsert_plugin_row

    def fail_upsert(*args: Any, **kwargs: Any) -> None:
        real_upsert(*args, **kwargs)
        raise RuntimeError("injected plugin upsert failure")

    monkeypatch.setattr(state_store_module, "_upsert_plugin_row", fail_upsert)

    with pytest.raises(RuntimeError, match="plugin upsert failure"):
        manager.update("demo")

    assert _tree_snapshot(tmp_path / "plugins" / "demo") == before_tree
    assert _plugin_state_snapshot(state, "demo") == before_state
    _assert_no_extension_debris(tmp_path / "plugins")


def test_plugin_mid_sync_failure_rolls_back_all_rows_and_old_tree(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    manager, state, fetcher = _installed_plugin(tmp_path)
    before_tree = _tree_snapshot(tmp_path / "plugins" / "demo")
    before_state = _plugin_state_snapshot(state, "demo")
    fetcher.source = _plugin_repo(tmp_path / "repo-v2", version="2.0.0")

    def fail_mcp_sync(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("injected mid-sync failure")

    monkeypatch.setattr(state_store_module, "_upsert_mcp_server_row", fail_mcp_sync)

    with pytest.raises(RuntimeError, match="mid-sync failure"):
        manager.update("demo")

    assert _tree_snapshot(tmp_path / "plugins" / "demo") == before_tree
    assert _plugin_state_snapshot(state, "demo") == before_state
    _assert_no_extension_debris(tmp_path / "plugins")


def test_fresh_plugin_state_failure_leaves_no_live_or_partial_rows(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repo = _plugin_repo(tmp_path / "repo", version="1.0.0")
    state = AgentStateStore(tmp_path / "state.db")
    manager = PluginManager(
        tmp_path / "plugins",
        state,
        fetcher=TransactionFetcher(repo),
    )
    real_upsert = state_store_module._upsert_plugin_row

    def fail_upsert(*args: Any, **kwargs: Any) -> None:
        real_upsert(*args, **kwargs)
        raise RuntimeError("injected fresh plugin upsert failure")

    monkeypatch.setattr(state_store_module, "_upsert_plugin_row", fail_upsert)

    with pytest.raises(RuntimeError, match="fresh plugin upsert failure"):
        manager.install("owner/repo")

    assert not (tmp_path / "plugins" / "demo").exists()
    assert state.list_plugins() == []
    assert not any(str(row["id"]).startswith("plugin.demo.") for row in state.list_skills())
    assert not any(
        str(row["id"]).startswith("plugin.demo.") for row in state.list_mcp_servers()
    )
    _assert_no_extension_debris(tmp_path / "plugins")


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires elevated Windows rights")
def test_plugin_overwrite_refuses_live_symlink_without_touching_target(tmp_path: Path) -> None:
    repo = _plugin_repo(tmp_path / "repo", version="1.0.0")
    plugins_root = tmp_path / "plugins"
    victim = plugins_root / "victim"
    victim.mkdir(parents=True)
    sentinel = victim / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    (plugins_root / "demo").symlink_to(victim, target_is_directory=True)
    state = AgentStateStore(tmp_path / "state.db")
    manager = PluginManager(plugins_root, state, fetcher=TransactionFetcher(repo))

    with pytest.raises(
        extension_transaction.ExtensionTransactionError,
        match="real directory",
    ):
        manager.install("owner/repo", overwrite=True)

    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert (plugins_root / "demo").is_symlink()
    assert state.list_plugins() == []


def test_plugin_enable_failure_is_one_atomic_state_transaction(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    manager, state, _fetcher = _installed_plugin(tmp_path)
    manager.set_enabled("demo", False)
    before_tree = _tree_snapshot(tmp_path / "plugins" / "demo")
    before_state = _plugin_state_snapshot(state, "demo")

    def fail_mcp_sync(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("injected enable sync failure")

    monkeypatch.setattr(state_store_module, "_upsert_mcp_server_row", fail_mcp_sync)

    with pytest.raises(RuntimeError, match="enable sync failure"):
        manager.set_enabled("demo", True)

    assert _tree_snapshot(tmp_path / "plugins" / "demo") == before_tree
    assert _plugin_state_snapshot(state, "demo") == before_state


def test_plugin_remove_failure_restores_exact_tree_and_all_state_rows(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    manager, state, _fetcher = _installed_plugin(tmp_path)
    state.set_capability_override(
        "skill",
        "plugin.demo.hello",
        True,
        expected_revision=0,
        default_enabled=False,
    )
    before_tree = _tree_snapshot(tmp_path / "plugins" / "demo")
    before_state = _plugin_state_snapshot(state, "demo")
    real_delete = state_store_module._delete_capability_override_row
    calls = 0

    def fail_mid_delete(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected plugin state delete failure")
        real_delete(*args, **kwargs)

    monkeypatch.setattr(state_store_module, "_delete_capability_override_row", fail_mid_delete)

    with pytest.raises(RuntimeError, match="state delete failure"):
        manager.remove("demo")

    assert _tree_snapshot(tmp_path / "plugins" / "demo") == before_tree
    assert _plugin_state_snapshot(state, "demo") == before_state
    _assert_no_extension_debris(tmp_path / "plugins")


def test_successful_plugin_overwrite_publishes_only_complete_new_generation(
    tmp_path: Path,
) -> None:
    manager, state, fetcher = _installed_plugin(tmp_path)
    before_state = _plugin_state_snapshot(state, "demo")
    fetcher.source = _plugin_repo(tmp_path / "repo-v2", version="2.0.0")

    updated = manager.update("demo")

    after_state = _plugin_state_snapshot(state, "demo")
    assert updated["manifest"]["version"] == "2.0.0"
    assert after_state != before_state
    assert (
        tmp_path / "plugins" / "demo" / "generated" / "skills" / "hello" / "SKILL.md"
    ).read_text(encoding="utf-8") == "Instructions for plugin version 2.0.0."
    _assert_no_extension_debris(tmp_path / "plugins")


def test_enabled_plugin_is_quiesced_before_any_live_directory_move(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    manager, state, fetcher = _installed_plugin(tmp_path)
    fetcher.source = _plugin_repo(tmp_path / "repo-v2", version="2.0.0")
    live = tmp_path / "plugins" / "demo"
    real_replace = extension_transaction.os.replace
    observed_quiesce: list[bool] = []

    def inspect_before_move(source: object, destination: object, *args: Any, **kwargs: Any) -> None:
        source_path = Path(os.fspath(source))
        destination_path = Path(os.fspath(destination))
        if source_path == live or destination_path == live:
            plugin_disabled = state.get_plugin("demo")["enabled"] is False
            skills_disabled = all(
                row["enabled"] is False
                for row in state.list_skills()
                if str(row["id"]).startswith("plugin.demo.")
            )
            mcp_disabled = all(
                row["enabled"] is False
                for row in state.list_mcp_servers()
                if str(row["id"]).startswith("plugin.demo.")
            )
            observed_quiesce.append(plugin_disabled and skills_disabled and mcp_disabled)
        real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(extension_transaction.os, "replace", inspect_before_move)

    updated = manager.update("demo")

    assert observed_quiesce and all(observed_quiesce)
    assert updated["enabled"] is True
    assert state.get_skill("plugin.demo.hello")["enabled"] is True
    assert state.get_mcp_server("plugin.demo.static")["enabled"] is True


def test_plugin_rollback_cleanup_failure_is_reported_fail_closed(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    manager, state, fetcher = _installed_plugin(tmp_path)
    before_tree = _tree_snapshot(tmp_path / "plugins" / "demo")
    before_state = _plugin_state_snapshot(state, "demo")
    fetcher.source = _plugin_repo(tmp_path / "repo-v2", version="2.0.0")
    real_remove = extension_transaction.remove_tree_verified
    real_upsert = state_store_module._upsert_plugin_row

    def fail_failed_tree_cleanup(path: Path) -> None:
        if ".failed-" in path.name:
            raise extension_transaction.ExtensionCleanupIncompleteError(
                "injected rollback cleanup uncertainty"
            )
        real_remove(path)

    def fail_upsert(*args: Any, **kwargs: Any) -> None:
        real_upsert(*args, **kwargs)
        raise RuntimeError("injected state failure before rollback")

    monkeypatch.setattr(extension_transaction, "remove_tree_verified", fail_failed_tree_cleanup)
    monkeypatch.setattr(state_store_module, "_upsert_plugin_row", fail_upsert)

    with pytest.raises(
        extension_transaction.ExtensionCleanupIncompleteError,
        match="rollback cleanup uncertainty",
    ):
        manager.update("demo")

    assert _tree_snapshot(tmp_path / "plugins" / "demo") == before_tree
    assert _plugin_state_snapshot(state, "demo") == before_state
    assert any(".failed-" in path.name for path in (tmp_path / "plugins").iterdir())


def test_plugin_old_generation_cleanup_failure_leaves_exact_new_live_state(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    manager, state, fetcher = _installed_plugin(tmp_path)
    fetcher.source = _plugin_repo(tmp_path / "repo-v2", version="2.0.0")
    real_remove = extension_transaction.remove_tree_verified

    def fail_old_generation_cleanup(path: Path) -> None:
        if ".rollback-" in path.name:
            raise extension_transaction.ExtensionCleanupIncompleteError(
                "injected old plugin cleanup uncertainty"
            )
        real_remove(path)

    monkeypatch.setattr(extension_transaction, "remove_tree_verified", fail_old_generation_cleanup)

    with pytest.raises(
        extension_transaction.ExtensionCleanupIncompleteError,
        match="old plugin cleanup uncertainty",
    ):
        manager.update("demo")

    assert state.get_plugin("demo")["manifest"]["version"] == "2.0.0"
    assert (
        tmp_path / "plugins" / "demo" / "generated" / "skills" / "hello" / "SKILL.md"
    ).read_text(encoding="utf-8") == "Instructions for plugin version 2.0.0."
    assert any(".rollback-" in path.name for path in (tmp_path / "plugins").iterdir())


@pytest.mark.parametrize("failed_write", [1, 2])
def test_skill_write_failures_preserve_exact_old_capsule(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    failed_write: int,
) -> None:
    tool, context, arguments, skill_dir = _skill_fixture(tmp_path)
    before = _tree_snapshot(skill_dir)
    real_write = builtin_tools_module.write_regular_file
    calls = 0

    def fail_selected_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_write:
            raise OSError("injected staged skill write failure")
        real_write(path, content, mode=mode)

    monkeypatch.setattr(builtin_tools_module, "write_regular_file", fail_selected_write)

    result = tool.run(arguments, context)

    assert result.error == "skill_install_failed"
    assert _tree_snapshot(skill_dir) == before
    _assert_no_extension_debris(context.config.skills_dir)


@pytest.mark.parametrize("failed_move", [1, 2])
def test_skill_rename_failures_preserve_exact_old_capsule(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    failed_move: int,
) -> None:
    tool, context, arguments, skill_dir = _skill_fixture(tmp_path)
    before = _tree_snapshot(skill_dir)
    real_replace = extension_transaction.os.replace
    moves = 0

    def fail_selected_move(source: object, destination: object, *args: Any, **kwargs: Any) -> None:
        nonlocal moves
        source_path = Path(os.fspath(source))
        destination_path = Path(os.fspath(destination))
        if source_path.parent == context.config.skills_dir and destination_path.parent == source_path.parent:
            moves += 1
            if moves == failed_move:
                raise OSError("injected skill rename failure")
        real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(extension_transaction.os, "replace", fail_selected_move)

    result = tool.run(arguments, context)

    assert result.error == "skill_install_failed"
    assert _tree_snapshot(skill_dir) == before
    _assert_no_extension_debris(context.config.skills_dir)


def test_fresh_skill_write_failure_leaves_no_partial_live_capsule(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    tool, context, arguments, skill_dir = _skill_fixture(tmp_path, create_old=False)

    def fail_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected fresh skill write failure")

    monkeypatch.setattr(builtin_tools_module, "write_regular_file", fail_write)

    result = tool.run(arguments, context)

    assert result.error == "skill_install_failed"
    assert not skill_dir.exists()
    _assert_no_extension_debris(context.config.skills_dir)


def test_successful_skill_overwrite_publishes_only_complete_new_capsule(
    tmp_path: Path,
) -> None:
    tool, context, arguments, skill_dir = _skill_fixture(tmp_path)

    result = tool.run(arguments, context)

    assert result.success is True
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "new complete instructions"
    assert json.loads((skill_dir / "skill.json").read_text(encoding="utf-8"))["id"] == (
        "transactional-skill"
    )
    assert not (skill_dir / "old-only.txt").exists()
    _assert_no_extension_debris(context.config.skills_dir)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires elevated Windows rights")
def test_skill_overwrite_refuses_live_symlink_without_touching_target(tmp_path: Path) -> None:
    tool, context, arguments, skill_dir = _skill_fixture(tmp_path, create_old=False)
    victim = context.config.skills_dir / "victim"
    victim.mkdir(parents=True)
    sentinel = victim / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    skill_dir.symlink_to(victim, target_is_directory=True)
    arguments["overwrite"] = True

    result = tool.run(arguments, context)

    assert result.error == "skill_install_failed"
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert skill_dir.is_symlink()


def test_skill_old_generation_cleanup_failure_returns_fail_closed_new_generation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    tool, context, arguments, skill_dir = _skill_fixture(tmp_path)
    real_remove = extension_transaction.remove_tree_verified

    def fail_old_generation_cleanup(path: Path) -> None:
        if ".rollback-" in path.name:
            raise extension_transaction.ExtensionCleanupIncompleteError(
                "injected old skill cleanup uncertainty"
            )
        real_remove(path)

    monkeypatch.setattr(extension_transaction, "remove_tree_verified", fail_old_generation_cleanup)

    result = tool.run(arguments, context)

    assert result.error == "skill_install_cleanup_incomplete"
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "new complete instructions"
    assert not (skill_dir / "old-only.txt").exists()
    assert any(
        ".rollback-" in path.name for path in context.config.skills_dir.iterdir()
    )


def _installed_plugin(
    tmp_path: Path,
) -> tuple[PluginManager, AgentStateStore, TransactionFetcher]:
    repo = _plugin_repo(tmp_path / "repo-v1", version="1.0.0")
    state = AgentStateStore(tmp_path / "state.db")
    fetcher = TransactionFetcher(repo)
    manager = PluginManager(tmp_path / "plugins", state, fetcher=fetcher)
    manager.install("owner/repo", enable=True)
    return manager, state, fetcher


def _plugin_repo(path: Path, *, version: str) -> Path:
    path.mkdir()
    manifest = {
        "id": "demo",
        "name": "Transactional Demo",
        "version": version,
        "description": "A deterministic transactional plugin.",
        "risk": "low",
        "skills": [
            {
                "id": "hello",
                "name": "Hello",
                "description": "Say hello.",
                "instructions": f"Instructions for plugin version {version}.",
                "risk": "low",
            }
        ],
        "mcp_servers": [
            {
                "id": "static",
                "name": "Static MCP",
                "transport": "stdio",
                "tools": [{"name": "echo", "description": "Echo.", "risk": "low"}],
            }
        ],
    }
    (path / "kestrel.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _plugin_state_snapshot(state: AgentStateStore, plugin_id: str) -> dict[str, Any]:
    prefix = f"plugin.{plugin_id}."
    return {
        "plugin": state.get_plugin(plugin_id),
        "skills": [row for row in state.list_skills() if str(row["id"]).startswith(prefix)],
        "mcp": [
            row for row in state.list_mcp_servers() if str(row["id"]).startswith(prefix)
        ],
        "overrides": [
            row
            for row in state.list_capability_overrides()
            if str(row["capability_id"]).startswith(prefix)
            or str(row["capability_id"]).startswith(f"skill.{prefix}")
            or str(row["capability_id"]).startswith(f"mcp.{prefix}")
        ],
    }


def _skill_fixture(
    tmp_path: Path,
    *,
    create_old: bool = True,
) -> tuple[SkillInstallTool, ToolContext, dict[str, Any], Path]:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "transactional-skill"
    if create_old:
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.json").write_text('{"old": true}\n', encoding="utf-8")
        (skill_dir / "SKILL.md").write_text("old instructions", encoding="utf-8")
        (skill_dir / "old-only.txt").write_text("old generation", encoding="utf-8")
    memory = build_memory_system("memory", tmp_path / "memory")
    context = ToolContext(
        memory=memory,
        config=AgentConfig(allow_file_write=True, skills_dir=skills_dir),
        workspace=tmp_path,
    )
    arguments: dict[str, Any] = {
        "manifest": {
            "id": "transactional-skill",
            "name": "Transactional Skill",
            "description": "Exercise atomic skill installation.",
            "risk": "low",
            "runtime": {"type": "instruction"},
        },
        "instructions": "new complete instructions",
        "overwrite": create_old,
    }
    return SkillInstallTool(), context, arguments, skill_dir


def _tree_snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_mode & 0o777, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_no_extension_debris(root: Path) -> None:
    assert not [
        path.name
        for path in root.iterdir()
        if ".stage-" in path.name
        or ".rollback-" in path.name
        or ".failed-" in path.name
    ]
