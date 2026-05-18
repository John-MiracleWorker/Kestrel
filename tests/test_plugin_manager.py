from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.plugin_manager import (
    GitHubPluginSource,
    PluginError,
    PluginManager,
    load_plugin_manifest,
    parse_github_plugin_source,
)
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


class FakeFetcher:
    def __init__(self, source: Path, commit: str = "a" * 40) -> None:
        self.source = source
        self.commit = commit
        self.calls: list[tuple[GitHubPluginSource, str | None]] = []

    def fetch(self, source: GitHubPluginSource, destination: Path, ref: str | None = None) -> str:
        self.calls.append((source, ref))
        shutil.copytree(self.source, destination)
        return self.commit


def test_parse_github_plugin_source_accepts_only_public_github() -> None:
    parsed = parse_github_plugin_source("owner/repo")
    assert parsed.display_url == "https://github.com/owner/repo"
    assert parse_github_plugin_source("https://github.com/owner/repo.git").repo == "repo"

    for source in [
        "git@github.com:owner/repo.git",
        "https://raw.githubusercontent.com/owner/repo/main/plugin.yaml",
        "https://user:token@github.com/owner/repo",
        "https://github.com/owner/repo?token=secret",
    ]:
        with pytest.raises(PluginError):
            parse_github_plugin_source(source)


def test_plugin_manager_installs_disabled_and_enable_materializes_extensions(tmp_path: Path) -> None:
    repo = _kestrel_plugin_repo(tmp_path / "repo")
    state = AgentStateStore(tmp_path / "state.db")
    manager = PluginManager(tmp_path / "plugins", state, fetcher=FakeFetcher(repo))

    installed = manager.install("owner/repo")

    assert installed["id"] == "demo"
    assert installed["enabled"] is False
    assert installed["source_url"] == "https://github.com/owner/repo"
    assert installed["commit_sha"] == "a" * 40
    assert state.get_skill("plugin.demo.hello")["enabled"] is False
    assert state.get_mcp_server("plugin.demo.static")["enabled"] is False

    enabled = manager.set_enabled("demo", True)

    assert enabled["enabled"] is True
    assert state.get_skill("plugin.demo.hello")["enabled"] is True
    assert state.get_mcp_server("plugin.demo.static")["enabled"] is True

    manager.remove("demo")
    with pytest.raises(KeyError):
        state.get_plugin("demo")
    with pytest.raises(KeyError):
        state.get_skill("plugin.demo.hello")


def test_plugin_review_reports_dependency_and_isolation_blockers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_manifest(
        repo,
        {
            "id": "blocked",
            "name": "Blocked Plugin",
            "description": "Declares dependencies and required isolation.",
            "dependencies": {
                "python": ["requests>=2"],
                "node": ["left-pad"],
                "system": ["git"],
            },
            "isolation": {"mode": "container", "required": True},
            "skills": [{"id": "hello", "description": "Hello.", "instructions": "Hello."}],
        },
    )
    state = AgentStateStore(tmp_path / "state.db")
    manager = PluginManager(tmp_path / "plugins", state, fetcher=FakeFetcher(repo))

    review = manager.review("owner/repo")

    assert review["source_url"] == "https://github.com/owner/repo"
    assert review["commit_sha"] == "a" * 40
    assert review["dependency_review"]["requires_install"] is True
    assert review["dependency_review"]["declared"]["python"] == ["requests>=2"]
    assert review["isolation_review"] == {"mode": "container", "required": True, "available": False}
    assert review["enable_blockers"] == [
        "plugin_dependencies_unmanaged",
        "plugin_isolation_unavailable",
    ]
    with pytest.raises(KeyError):
        state.get_plugin("blocked")

    installed = manager.install("owner/repo")
    assert installed["enabled"] is False
    assert installed["risk_report"]["enable_blockers"] == review["enable_blockers"]

    with pytest.raises(PluginError, match="enable blocked"):
        manager.install("owner/repo", overwrite=True, enable=True)
    with pytest.raises(PluginError, match="enable blocked"):
        manager.set_enabled("blocked", True)


def test_plugin_manifest_rejects_malformed_dependency_and_isolation_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    _write_manifest(
        repo,
        {
            "id": "baddeps",
            "name": "Bad Dependencies",
            "description": "Bad dependency metadata.",
            "dependencies": {"python": "requests"},
        },
    )
    with pytest.raises(PluginError, match="dependencies.python"):
        load_plugin_manifest(repo)

    _write_manifest(
        repo,
        {
            "id": "badisolation",
            "name": "Bad Isolation",
            "description": "Bad isolation metadata.",
            "isolation": {"mode": "spaceship"},
        },
    )
    with pytest.raises(PluginError, match="isolation.mode"):
        load_plugin_manifest(repo)


def test_enabled_plugins_appear_in_runtime_registry_and_disabled_plugins_do_not(tmp_path: Path) -> None:
    repo = _kestrel_plugin_repo(tmp_path / "repo")
    config = AgentConfig(
        state_path=tmp_path / "state.db",
        plugins_dir=tmp_path / "plugins",
        skills_dir=tmp_path / "skills",
        memory_dir=tmp_path / "memory",
    )
    state = AgentStateStore(config.state_path)
    manager = PluginManager(config.plugins_dir, state, fetcher=FakeFetcher(repo))
    manager.install("owner/repo")
    runs = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
        plugins=manager,
    )

    disabled_names = {spec.name for spec in runs.build_registry().specs()}
    assert "skill.plugin.demo.hello.run" not in disabled_names
    assert "mcp.plugin.demo.static.echo" not in disabled_names

    manager.set_enabled("demo", True)
    enabled_specs = {spec.name: spec for spec in runs.build_registry().specs()}
    assert "skill.plugin.demo.hello.run" in enabled_specs
    assert enabled_specs["mcp.plugin.demo.static.echo"].requires_approval is True


def test_plugin_update_rejects_manifest_id_drift(tmp_path: Path) -> None:
    repo = _kestrel_plugin_repo(tmp_path / "repo")
    state = AgentStateStore(tmp_path / "state.db")
    fetcher = FakeFetcher(repo)
    manager = PluginManager(tmp_path / "plugins", state, fetcher=fetcher)
    manager.install("owner/repo")
    drift_repo = tmp_path / "drift-repo"
    _kestrel_plugin_repo(drift_repo, plugin_id="renamed")
    fetcher.source = drift_repo

    with pytest.raises(PluginError, match="manifest id changed"):
        manager.update("demo")

    assert state.get_plugin("demo")["id"] == "demo"
    with pytest.raises(KeyError):
        state.get_plugin("renamed")


def test_plugin_mcp_trust_flags_cannot_downgrade_approval_by_default(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_manifest(
        repo,
        {
            "id": "trusty",
            "name": "Trusty Plugin",
            "description": "Attempts to self-trust MCP tools.",
            "risk": "low",
            "mcp_servers": [
                {
                    "id": "static",
                    "transport": "stdio",
                    "risk_policy": "trust_manifest",
                    "tools": [
                        {
                            "name": "read_safe",
                            "description": "Read safe data.",
                            "risk": "low",
                            "requires_approval": False,
                            "trusted": True,
                            "allow_autonomous": True,
                        }
                    ],
                }
            ],
        },
    )
    state = AgentStateStore(tmp_path / "state.db")
    manager = PluginManager(tmp_path / "plugins", state, fetcher=FakeFetcher(repo))

    installed = manager.install("owner/repo", enable=True)
    server = state.get_mcp_server("plugin.trusty.static")

    assert installed["id"] == "trusty"
    assert server["risk_policy"] == "approval_by_default"
    assert server["tools"][0]["risk"] == "medium"
    assert server["tools"][0]["requires_approval"] is True


def test_plugin_mcp_stdio_command_is_allowlisted_and_hashed(tmp_path: Path) -> None:
    bad_repo = tmp_path / "bad-repo"
    bad_repo.mkdir()
    _write_manifest(
        bad_repo,
        {
            "id": "badmcp",
            "name": "Bad MCP",
            "description": "Uses a shell launcher.",
            "mcp_servers": [{"id": "shell", "transport": "stdio", "command": "/bin/sh", "args": ["-c", "echo hi"]}],
        },
    )
    state = AgentStateStore(tmp_path / "state.db")
    manager = PluginManager(tmp_path / "plugins", state, fetcher=FakeFetcher(bad_repo))

    with pytest.raises(PluginError, match="not allowed"):
        manager.install("owner/bad")

    good_repo = tmp_path / "good-repo"
    good_repo.mkdir()
    _write_manifest(
        good_repo,
        {
            "id": "goodmcp",
            "name": "Good MCP",
            "description": "Uses an allowlisted launcher.",
            "mcp_servers": [
                {
                    "id": "node",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["@modelcontextprotocol/server-filesystem", "."],
                    "tools": [{"name": "read", "description": "Read", "risk": "low"}],
                }
            ],
        },
    )
    manager = PluginManager(tmp_path / "plugins", state, fetcher=FakeFetcher(good_repo))

    manager.install("owner/good", enable=True)
    server = state.get_mcp_server("plugin.goodmcp.node")

    assert server["vetting"]["stdio_command_hash"].startswith("sha256:")
    assert server["vetting"]["connect_requires_approval"] is True


def test_mcp_connect_refuses_plugin_command_hash_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_manifest(
        repo,
        {
            "id": "tamper",
            "name": "Tamper MCP",
            "description": "Hash mismatch should block connect.",
            "mcp_servers": [
                {
                    "id": "node",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["@modelcontextprotocol/server-filesystem", "."],
                    "tools": [{"name": "read", "description": "Read", "risk": "low"}],
                }
            ],
        },
    )
    state = AgentStateStore(tmp_path / "state.db")
    PluginManager(tmp_path / "plugins", state, fetcher=FakeFetcher(repo)).install("owner/tamper", enable=True)
    row = state.get_mcp_server("plugin.tamper.node")
    row["args"] = ["@modelcontextprotocol/server-filesystem", "/tmp"]
    state.upsert_mcp_server(row)

    result = MCPManager(state).connect_server("plugin.tamper.node")

    assert result["ok"] is False
    assert "hash mismatch" in result["message"]


def test_mcp_plugin_connect_requires_explicit_approval(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_manifest(
        repo,
        {
            "id": "approvalmcp",
            "name": "Approval MCP",
            "description": "Connect approval should block plugin MCP startup.",
            "mcp_servers": [
                {
                    "id": "node",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["@modelcontextprotocol/server-filesystem", "."],
                    "tools": [{"name": "read", "description": "Read", "risk": "low"}],
                }
            ],
        },
    )
    state = AgentStateStore(tmp_path / "state.db")
    PluginManager(tmp_path / "plugins", state, fetcher=FakeFetcher(repo)).install("owner/approval", enable=True)
    manager = MCPManager(state)

    blocked = manager.connect_server("plugin.approvalmcp.node")

    assert blocked["ok"] is False
    assert blocked["message"] == "MCP connect approval required."
    assert blocked["server"]["status"] == "approval_required"
    assert blocked["server"]["session_state"] == "approval_required"

    approved = manager.approve_server_connect("plugin.approvalmcp.node")
    connected = manager.connect_server("plugin.approvalmcp.node")

    assert approved["vetting"]["connect_approved"] is True
    assert connected["ok"] is True
    assert connected["server"]["session_state"] == "static"


def test_hermes_manifest_is_supported_without_executing_python_hooks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "__init__.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    (repo / "plugin.yaml").write_text(
        "\n".join(
            [
                "name: Hermes Sample",
                "description: Compatible metadata only",
                "requires_env: [HERMES_TOKEN]",
                "tools: [python_registered_tool]",
            ]
        ),
        encoding="utf-8",
    )

    manifest = load_plugin_manifest(repo)

    assert manifest.format == "hermes"
    assert manifest.id == "Hermes-Sample"
    assert manifest.requires_env == ("HERMES_TOKEN",)
    assert "python_hooks_ignored" in manifest.unsupported_features
    assert "python_tool_registration_ignored" in manifest.unsupported_features


def test_manifest_paths_cannot_escape_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (repo / "escape.md").symlink_to(outside)
    _write_manifest(
        repo,
        {
            "id": "badpath",
            "name": "Bad Path",
            "description": "Path escape should fail.",
            "skills": [
                {
                    "id": "escape",
                    "description": "Escape",
                    "instructions_path": "escape.md",
                }
            ],
        },
    )

    with pytest.raises(PluginError):
        load_plugin_manifest(repo)


def _kestrel_plugin_repo(path: Path, *, plugin_id: str = "demo") -> Path:
    path.mkdir()
    _write_manifest(
        path,
        {
            "id": plugin_id,
            "name": "Demo Plugin",
            "version": "1.0.0",
            "description": "A deterministic test plugin.",
            "risk": "low",
            "permissions": ["repo-read"],
            "skills": [
                {
                    "id": "hello",
                    "name": "Hello",
                    "description": "Say hello.",
                    "instructions": "Return a friendly hello.",
                    "risk": "low",
                }
            ],
            "mcp_servers": [
                {
                    "id": "static",
                    "name": "Static MCP",
                    "transport": "stdio",
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo a value.",
                            "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
                            "risk": "low",
                        }
                    ],
                }
            ],
        },
    )
    return path


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    (path / "kestrel.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
