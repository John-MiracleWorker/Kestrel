from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep

from pytest import MonkeyPatch

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution, ToolSpec
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry


class SlowTool(AgentTool):
    spec = ToolSpec(
        name="slow.tool",
        description="Sleeps longer than the configured timeout.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        sleep(0.2)
        return ToolExecution(
            call=ToolCall(name=self.spec.name, arguments=arguments),
            success=True,
            content="finished",
        )


def test_tool_registry_times_out_slow_tools(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = ToolRegistry()
    registry.register(SlowTool())
    config = AgentConfig(tool_timeout_seconds=0.01)

    result = registry.execute(
        ToolCall(name="slow.tool", arguments={}),
        ToolContext(memory=memory, config=config, workspace=tmp_path),
    )

    assert result.success is False
    assert result.error == "tool_timeout"
    assert "timed out" in result.content


def test_subprocess_tool_timeout_kills_child_process_and_caps_requested_timeout(
    tmp_path: Path,
) -> None:
    script = tmp_path / "sleep_then_write.py"
    marker = tmp_path / "should_not_exist.txt"
    script.write_text(
        "import pathlib, time\n"
        "time.sleep(5)\n"
        f"pathlib.Path({str(marker)!r}).write_text('finished')\n",
        encoding="utf-8",
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="test.run",
        arguments={"command": [sys.executable, str(script)], "timeout": 30},
        id="kill_sleep",
    )

    result = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True, tool_timeout_seconds=0.2),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"kill_sleep"}),
            approved_tool_call_arguments={"kill_sleep": call.arguments},
        ),
    )

    sleep(0.4)
    assert result.success is False
    assert result.error == "tool_timeout"
    assert marker.exists() is False


def test_memory_search_tool_returns_hits(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            title="Needle fact",
            content="The needle lives in semantic memory.",
            confidence=0.8,
        )
    )
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="memory.search", arguments={"query": "needle", "k": 3}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert result.success
    assert "Needle fact" in result.content


def test_memory_search_invalid_layer_returns_structured_failure(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(name="memory.search", arguments={"query": "needle", "layers": ["bogus"]}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert not result.success
    assert result.error == "invalid_tool_arguments"
    assert "Unknown memory layer" in result.content


def test_memory_write_accepts_only_working_and_episodic_direct_writes(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    for layer in ("working", "episodic"):
        result = registry.execute(
            ToolCall(
                name="memory.write",
                arguments={
                    "layer": layer,
                    "kind": "observation",
                    "title": f"Direct {layer} note",
                    "content": f"Direct {layer} notes stay volatile or event-scoped.",
                    "confidence": 0.8,
                },
            ),
            ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
        )

        assert result.success
        assert memory.retrieve(
            RetrievalQuery(
                query=f"Direct {layer} notes", layers=(MemoryLayer(layer),), k_per_layer=3
            )
        )


def test_memory_write_rejects_direct_stable_layer_writes(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    cases = [
        ("semantic", "fact", "Stable fact", "Stable facts require the nested learning path."),
        (
            "procedural",
            "procedure",
            "Stable procedure",
            "Stable procedures require repeated validation.",
        ),
        ("self", "fact", "Stable self record", "Self records require the self.remember path."),
    ]

    for layer, kind, title, content in cases:
        result = registry.execute(
            ToolCall(
                name="memory.write",
                arguments={
                    "layer": layer,
                    "kind": kind,
                    "title": title,
                    "content": content,
                    "confidence": 0.99,
                    "importance": 0.9,
                },
            ),
            ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
        )

        assert not result.success
        assert result.error == "stable_memory_write_rejected"
        assert "memory.learn" in result.content
        assert not memory.retrieve(
            RetrievalQuery(query=content, layers=(MemoryLayer(layer),), k_per_layer=3)
        )


def test_memory_write_policy_guard_remains_stricter_than_stable_direct_writes(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    arguments = {
        "layer": "policy",
        "kind": "policy",
        "title": "Direct policy",
        "content": "Direct policy writes must remain gated.",
        "confidence": 0.99,
        "importance": 0.95,
    }

    disabled = registry.execute(
        ToolCall(name="memory.write", arguments=arguments),
        ToolContext(
            memory=memory, config=AgentConfig(allow_policy_writes=False), workspace=tmp_path
        ),
    )
    enabled = registry.execute(
        ToolCall(name="memory.write", arguments=arguments),
        ToolContext(
            memory=memory, config=AgentConfig(allow_policy_writes=True), workspace=tmp_path
        ),
    )

    assert disabled.success is False
    assert disabled.error == "policy_write_disabled"
    assert enabled.success is False
    assert enabled.error == "stable_memory_write_rejected"
    assert "memory.learn" in enabled.content
    assert not memory.retrieve(
        RetrievalQuery(query="Direct policy writes", layers=(MemoryLayer.POLICY,), k_per_layer=3)
    )


def test_file_read_rejects_path_escape(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="file.read", arguments={"path": "../outside.txt"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "file_read_failed"


def test_high_risk_tool_requires_enablement(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}),
        ToolContext(memory=memory, config=AgentConfig(allow_shell=False), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "tool_disabled"


def test_shell_run_blocks_remote_publishing_escape_routes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="should not run", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.process_tools.subprocess.Popen", fake_run)
    blocked_commands = [
        ["git", "push", "origin", "main"],
        ["git", "push", "--force", "origin", "main"],
        ["git", "tag", "v1.0.0"],
        ["git", "remote", "set-url", "origin", "git@github.com:evil/repo.git"],
        ["gh", "repo", "edit", "--visibility", "public"],
        ["gh", "secret", "set", "TOKEN"],
        ["gh", "workflow", "enable", "deploy.yml"],
    ]

    for index, command in enumerate(blocked_commands):
        call = ToolCall(
            name="shell.run", arguments={"command": command}, id=f"remote_escape_{index}"
        )
        result = registry.execute(
            call,
            ToolContext(
                memory=memory,
                config=AgentConfig(allow_shell=True),
                workspace=tmp_path,
                approved_tool_call_ids=frozenset({call.id}),
                approved_tool_call_arguments={call.id: call.arguments},
            ),
        )
        assert result.error == "remote_mutation_blocked", command

    assert calls == []


def test_shell_run_python_escape_is_not_allowlisted(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="should not run", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.process_tools.subprocess.Popen", fake_run)
    call = ToolCall(
        name="shell.run",
        arguments={
            "command": ["python", "-c", "import subprocess; subprocess.run(['git', 'push'])"]
        },
        id="python_escape",
    )

    result = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"python_escape"}),
            approved_tool_call_arguments={"python_escape": call.arguments},
        ),
    )

    assert result.error == "command_not_allowlisted"
    assert calls == []


def test_remote_publishing_config_is_disabled_by_default_and_env_gated(
    monkeypatch: MonkeyPatch,
) -> None:
    default_config = AgentConfig()
    assert default_config.allow_git_push is False
    assert default_config.allow_remote_mutation is False
    assert default_config.git_write_mode == "local_branch"
    assert default_config.protected_branches == ("main", "master", "release/*")

    monkeypatch.setenv("NEST_AGENT_ALLOW_GIT_PUSH", "1")
    monkeypatch.setenv("NEST_AGENT_ALLOW_REMOTE_MUTATION", "true")
    monkeypatch.setenv("NEST_AGENT_GIT_WRITE_MODE", "fork_pr")
    monkeypatch.setenv("NEST_AGENT_PROTECTED_BRANCHES", "main,stable/*")

    env_config = AgentConfig.from_env()
    assert env_config.allow_git_push is True
    assert env_config.allow_remote_mutation is True
    assert env_config.git_write_mode == "fork_pr"
    assert env_config.protected_branches == ("main", "stable/*")


def test_mapped_high_risk_tools_require_capability_even_when_approval_is_disabled(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    plugin_repo = tmp_path / "plugin-repo"
    plugin_repo.mkdir()
    (plugin_repo / "kestrel.plugin.json").write_text(
        json.dumps(
            {
                "id": "disabledplug",
                "name": "Disabled Plugin",
                "description": "Should not be fetched while plugin installs are disabled.",
                "skills": [{"id": "hello", "description": "Hello.", "instructions": "Hello."}],
            }
        ),
        encoding="utf-8",
    )

    def fake_fetch(self: object, source: object, destination: Path, ref: str | None = None) -> str:
        del self, source, ref
        shutil.copytree(plugin_repo, destination)
        return "d" * 40

    monkeypatch.setattr("nested_memvid_agent.plugin_manager.GitPluginFetcher.fetch", fake_fetch)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    config = AgentConfig(
        require_approval_for_high_risk_tools=False,
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
    )
    cases = [
        ("shell.run", {"command": ["echo", "capability-bypass"]}),
        ("file.write", {"path": "blocked.txt", "content": "blocked"}),
        ("patch.apply", {"patch": "diff --git a/blocked.txt b/blocked.txt\n"}),
        ("codex.exec", {"prompt": "summarize this repo"}),
        (
            "skill.install",
            {
                "manifest": {
                    "id": "blocked-skill",
                    "name": "Blocked Skill",
                    "description": "Should not install without file-write enablement.",
                    "runtime": {"type": "instruction"},
                },
                "instructions": "Do nothing.",
            },
        ),
        ("plugin.install", {"source": "owner/repo"}),
    ]

    for tool_name, arguments in cases:
        result = registry.execute(
            ToolCall(
                name=tool_name, arguments=arguments, id=f"{tool_name.replace('.', '_')}_disabled"
            ),
            ToolContext(memory=memory, config=config, workspace=tmp_path),
        )
        assert result.error == "tool_disabled", tool_name

    assert not (tmp_path / "blocked.txt").exists()
    assert not (tmp_path / "skills" / "blocked-skill").exists()
    assert not (tmp_path / "plugins" / "disabledplug").exists()


def test_malformed_tool_arguments_fail_cleanly(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(name="memory.search", arguments="not an object"),  # type: ignore[arg-type]
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert not result.success
    assert result.error == "invalid_tool_arguments"


def test_id_only_approval_is_not_sufficient_for_high_risk_exact_call(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(
            name="shell.run", arguments={"command": ["echo", "must-not-run"]}, id="shell_id_only"
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"shell_id_only"}),
        ),
    )

    assert not result.success
    assert result.error == "approval_required"


def test_high_risk_tool_with_allow_flag_still_requests_approval(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}, id="shell1"),
        ToolContext(memory=memory, config=AgentConfig(allow_shell=True), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "approval_required"


def test_skill_install_requires_approval_and_writes_capsule_after_exact_approval(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    manifest = {
        "id": "uploaded-review",
        "name": "Uploaded Review",
        "description": "Review uploaded tool changes.",
        "risk": "low",
        "runtime": {"type": "instruction"},
    }
    arguments = {"manifest": manifest, "instructions": "Review the task and return concise notes."}
    call = ToolCall(name="skill.install", arguments=arguments, id="skill_install_exact")

    pending = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_file_write=True, skills_dir=tmp_path / "skills"),
            workspace=tmp_path,
        ),
    )
    assert pending.error == "approval_required"

    approved = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_file_write=True, skills_dir=tmp_path / "skills"),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"skill_install_exact"}),
            approved_tool_call_arguments={"skill_install_exact": arguments},
        ),
    )

    assert approved.success is True
    assert (tmp_path / "skills" / "uploaded-review" / "skill.json").exists()
    assert approved.data["installed"] is True


def test_default_registry_includes_self_and_web_tools() -> None:
    names = {spec.name for spec in build_default_tools().specs()}

    assert {
        "self.inspect",
        "self.reflect",
        "self.remember",
        "self.propose_change",
        "web.search",
        "web.fetch",
    } <= names


def test_self_inspect_returns_redacted_runtime_snapshot(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("KESTREL_SELF_TEST_TOKEN", "secret-token")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    config = AgentConfig(
        api_key_env="KESTREL_SELF_TEST_TOKEN",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        workspace=tmp_path,
    )

    result = registry.execute(
        ToolCall(name="self.inspect", arguments={"include_tools": True}),
        ToolContext(memory=memory, config=config, workspace=tmp_path),
    )

    assert result.success
    assert result.data["identity"]["name"] == "Kestrel"
    assert "self" in {layer["layer"] for layer in result.data["memory_layers"]}
    assert "self.inspect" in {tool["name"] for tool in result.data["tools"]}
    assert result.data["provider"]["api_key_env"] == "KESTREL_SELF_TEST_TOKEN"
    assert result.data["provider"]["api_key_configured"] is True
    assert "secret-token" not in json.dumps(result.data)


def test_self_remember_writes_validated_self_memory(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    arguments = {
        "title": "User workflow preference",
        "content": "The user prefers implementation over analysis once a plan is agreed.",
        "schema": "user_workflow_preference",
        "validation_status": "user_confirmed",
        "confidence": 0.88,
    }

    result = registry.execute(
        ToolCall(name="self.remember", arguments=arguments),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    hits = memory.retrieve(
        RetrievalQuery(
            query="implementation over analysis", layers=(MemoryLayer.SELF,), k_per_layer=3
        )
    )
    assert hits
    record = hits[0].record
    assert record.metadata["self_schema"] == "user_workflow_preference"
    assert record.metadata["validation_status"] == "user_confirmed"
    assert record.evidence[0].source == "self.remember"


def test_self_remember_rejects_low_confidence_self_memory(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(
            name="self.remember",
            arguments={
                "title": "Unvalidated preference",
                "content": "Maybe the user likes this workflow.",
                "schema": "user_workflow_preference",
                "validation_status": "unverified",
                "confidence": 0.5,
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success is False
    assert result.error == "self_memory_rejected"
    assert result.data["promotion_requirements"]["min_validation_score"] == 0.78


def test_self_propose_change_requires_self_modification_enablement(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(
            name="self.propose_change", arguments={"request": "Change Kestrel's tool registry."}
        ),
        ToolContext(
            memory=memory, config=AgentConfig(allow_self_modification=False), workspace=tmp_path
        ),
    )

    assert result.error == "tool_disabled"


def test_self_propose_change_requires_exact_approval_when_enabled(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    arguments = {"request": "Teach Kestrel to expose a richer Soul summary."}
    call = ToolCall(name="self.propose_change", arguments=arguments, id="self_change_1")
    config = AgentConfig(allow_self_modification=True, state_path=tmp_path / "state.db")

    pending = registry.execute(
        call,
        ToolContext(memory=memory, config=config, workspace=tmp_path),
    )

    assert pending.error == "approval_required"

    approved = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=config,
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"self_change_1"}),
            approved_tool_call_arguments={"self_change_1": arguments},
        ),
    )

    assert approved.success
    assert approved.data["required_gates"] == [
        "repair.prepare",
        "repair.apply_patch",
        "repair.validate",
        "repair.review",
        "git.commit",
    ]
    assert approved.data["push_or_merge_allowed"] is False


def test_web_tools_are_gated_and_mockable(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    disabled = registry.execute(
        ToolCall(name="web.search", arguments={"query": "Kestrel self awareness"}),
        ToolContext(memory=memory, config=AgentConfig(allow_web=False), workspace=tmp_path),
    )
    assert disabled.error == "tool_disabled"

    searched = registry.execute(
        ToolCall(name="web.search", arguments={"query": "Kestrel self awareness"}),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_web=True, web_backend="mock"),
            workspace=tmp_path,
        ),
    )
    assert searched.success
    assert searched.data["results"][0]["url"].startswith("https://mock.kestrel.local/search/")

    fetched = registry.execute(
        ToolCall(name="web.fetch", arguments={"url": searched.data["results"][0]["url"]}),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_web=True, web_backend="mock"),
            workspace=tmp_path,
        ),
    )
    assert fetched.success
    assert "Mock web page for Kestrel" in fetched.content

    private = registry.execute(
        ToolCall(name="web.fetch", arguments={"url": "http://127.0.0.1/private"}),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_web=True, web_backend="mock"),
            workspace=tmp_path,
        ),
    )
    assert private.error == "unsafe_url"


def test_plugin_install_requires_enablement_and_exact_approval(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    plugin_repo = tmp_path / "plugin-repo"
    plugin_repo.mkdir()
    (plugin_repo / "kestrel.plugin.json").write_text(
        json.dumps(
            {
                "id": "toolplug",
                "name": "Tool Plugin",
                "description": "Installed through the high-risk plugin tool.",
                "skills": [
                    {
                        "id": "hello",
                        "description": "Say hello.",
                        "instructions": "Return hello.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_fetch(self: object, source: object, destination: Path, ref: str | None = None) -> str:
        del self, source, ref
        shutil.copytree(plugin_repo, destination)
        return "b" * 40

    monkeypatch.setattr("nested_memvid_agent.plugin_manager.GitPluginFetcher.fetch", fake_fetch)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    arguments = {"source": "owner/repo", "enable": True}
    call = ToolCall(name="plugin.install", arguments=arguments, id="plugin_install_exact")

    disabled = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(state_path=tmp_path / "state.db", plugins_dir=tmp_path / "plugins"),
            workspace=tmp_path,
        ),
    )
    assert disabled.error == "tool_disabled"

    pending = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(
                allow_plugin_install=True,
                state_path=tmp_path / "state.db",
                plugins_dir=tmp_path / "plugins",
            ),
            workspace=tmp_path,
        ),
    )
    assert pending.error == "approval_required"

    approved = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(
                allow_plugin_install=True,
                state_path=tmp_path / "state.db",
                plugins_dir=tmp_path / "plugins",
            ),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"plugin_install_exact"}),
            approved_tool_call_arguments={"plugin_install_exact": arguments},
        ),
    )

    assert approved.success is True
    assert approved.data["id"] == "toolplug"
    assert approved.data["enabled"] is True


def test_approved_exact_tool_call_runs_once_and_changed_args_do_not_run(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}, id="shell_exact")

    approved = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"shell_exact"}),
            approved_tool_call_arguments={"shell_exact": {"command": ["echo", "hi"]}},
        ),
    )
    assert approved.success
    assert "hi" in approved.content

    changed = registry.execute(
        ToolCall(name="shell.run", arguments={"command": ["echo", "bye"]}, id="shell_exact"),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"shell_exact"}),
            approved_tool_call_arguments={"shell_exact": {"command": ["echo", "hi"]}},
        ),
    )
    assert not changed.success
    assert changed.error == "approval_required"


def test_tool_exception_returns_structured_failure(tmp_path: Path) -> None:
    class ExplodingTool(AgentTool):
        spec = ToolSpec(name="test.explode", description="Explode", parameters={"type": "object"})

        def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
            raise RuntimeError("boom")

    memory = build_memory_system("memory", tmp_path / "memory")
    registry = ToolRegistry()
    registry.register(ExplodingTool())

    result = registry.execute(
        ToolCall(name="test.explode", arguments={}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert not result.success
    assert result.error == "tool_execution_failed"
    assert "RuntimeError: boom" in result.content


def test_diagnosis_classify_identifies_test_failures(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(
            name="diagnosis.classify",
            arguments={
                "failure_text": "FAILED tests/test_api.py::test_login - AssertionError: expected 200 got 500",
                "source": "pytest",
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["classification"] == "test_failure"
    assert "test failure playbook" in result.content.lower()
    assert result.data["playbook"]["next_actions"]


def test_diagnosis_classify_identifies_bare_assertion_errors(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(
            name="diagnosis.classify",
            arguments={
                "failure_text": "Traceback (most recent call last):\nAssertionError: expected fixed",
                "source": "repair.validate",
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["classification"] == "test_failure"


def test_diagnosis_recall_searches_prior_failure_lessons(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.PROCEDURAL,
            kind=MemoryKind.PROCEDURE,
            title="ImportError repair recipe",
            content="When pytest reports ModuleNotFoundError for nested_memvid_agent, set PYTHONPATH=src before retrying.",
            confidence=0.9,
        )
    )
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(
            name="diagnosis.recall",
            arguments={"failure_text": "ModuleNotFoundError: nested_memvid_agent", "k": 3},
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["classification"] == "missing_dependency"
    assert result.data["hits"]
    assert result.data["hits"][0]["layer"] == "procedural"
    assert "PYTHONPATH=src" in result.content


def test_repair_prepare_requires_approval_even_when_file_write_enabled(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(name="repair.prepare", arguments={"branch": "codex/repair-test"}, id="repair1"),
        ToolContext(memory=memory, config=AgentConfig(allow_file_write=True), workspace=tmp_path),
    )

    assert not result.success
    assert result.error == "approval_required"


def _init_git_repo(path: Path) -> str:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "seed",
        ],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    )
    return base.stdout.strip()


def _approved_context(
    memory: object, tmp_path: Path, call: ToolCall, *, allow_shell: bool = False
) -> ToolContext:
    return ToolContext(
        memory=memory,  # type: ignore[arg-type]
        config=AgentConfig(
            allow_file_write=True,
            allow_shell=allow_shell,
            allow_git_commit=True,
            allow_memory_import=True,
        ),
        workspace=tmp_path,
        approved_tool_call_ids=frozenset({call.id}),
        approved_tool_call_arguments={call.id: call.arguments},
    )


def test_repair_prepare_creates_approved_branch_from_clean_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="repair.prepare", arguments={"branch": "codex/repair-test"}, id="repair_prepare"
    )

    result = registry.execute(
        call,
        _approved_context(memory, tmp_path, call),
    )

    assert result.success
    assert result.data["branch"] == "codex/repair-test"
    assert result.data["base_sha"]
    assert result.data["mode"] == "branch"
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert current.stdout.strip() == "codex/repair-test"


def test_repair_prepare_refuses_dirty_repo_by_default(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="repair.prepare", arguments={"branch": "codex/dirty-test"}, id="repair_dirty"
    )

    result = registry.execute(
        call,
        _approved_context(memory, tmp_path, call),
    )

    assert not result.success
    assert result.error == "dirty_worktree"


def test_repair_prepare_disables_git_checkout_hooks(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    hooks_dir = tmp_path / ".githooks"
    hooks_dir.mkdir()
    hook = hooks_dir / "post-checkout"
    marker = tmp_path / "hook-ran.txt"
    hook.write_text(f"#!/usr/bin/env sh\necho hook-ran > {marker}\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "add", ".githooks/post-checkout"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "add checkout hook",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="repair.prepare", arguments={"branch": "codex/no-hooks"}, id="repair_no_hooks"
    )

    result = registry.execute(
        call,
        _approved_context(memory, tmp_path, call),
    )

    assert result.success
    assert not marker.exists()


def test_repair_status_reports_active_repair_branch_and_changed_files(tmp_path: Path) -> None:
    base = _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "switch", "-c", "codex/repair-status"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "README.md").write_text("changed\n")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(name="repair.status", arguments={"base_sha": base}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["active_repair_branch"] is True
    assert result.data["branch"] == "codex/repair-status"
    assert result.data["base_sha"] == base
    assert "README.md" in result.data["changed_files"]


def test_repair_apply_patch_refuses_non_repair_branch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="repair.apply_patch",
        arguments={"patch": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-seed\n+patched\n"},
        id="repair_apply_main",
    )

    result = registry.execute(call, _approved_context(memory, tmp_path, call))

    assert not result.success
    assert result.error == "not_repair_branch"
    assert (tmp_path / "README.md").read_text() == "seed\n"


def test_repair_apply_validate_and_rollback_on_repair_branch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "switch", "-c", "codex/repair-apply"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    patch_text = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-seed\n+patched\n"
    apply_call = ToolCall(
        name="repair.apply_patch", arguments={"patch": patch_text}, id="repair_apply"
    )

    applied = registry.execute(apply_call, _approved_context(memory, tmp_path, apply_call))

    assert applied.success
    assert (tmp_path / "README.md").read_text() == "patched\n"

    validate_call = ToolCall(
        name="repair.validate",
        arguments={"command": ["python", "-c", "import sys; print('bad'); sys.exit(2)"]},
        id="repair_validate",
    )
    validated = registry.execute(
        validate_call, _approved_context(memory, tmp_path, validate_call, allow_shell=True)
    )
    assert not validated.success
    assert validated.error == "repair_validation_failed"
    assert validated.data["diagnosis"]["classification"] in {"tool_failure", "unknown_failure"}

    rollback_call = ToolCall(name="repair.rollback", arguments={}, id="repair_rollback")
    rolled_back = registry.execute(
        rollback_call, _approved_context(memory, tmp_path, rollback_call)
    )
    assert rolled_back.success
    assert (tmp_path / "README.md").read_text() == "seed\n"


def test_repair_orchestrate_validate_blocks_non_repair_branch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="repair.orchestrate_validate",
        arguments={"command": ["python", "-c", "print('ok')"]},
        id="repair_orchestrate_main",
    )

    result = registry.execute(call, _approved_context(memory, tmp_path, call, allow_shell=True))

    assert not result.success
    assert result.error == "not_repair_branch"


def test_repair_orchestrate_validate_recalls_lessons_and_blocks_unchanged_retry(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "switch", "-c", "codex/repair-loop"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.PROCEDURAL,
            kind=MemoryKind.PROCEDURE,
            title="AssertionError repair lesson",
            content="When repair validation reports AssertionError: expected fixed, inspect the failing assertion before retrying.",
            confidence=0.9,
        )
    )
    registry = build_default_tools()
    command = ["python", "-c", "raise AssertionError('expected fixed')"]
    call = ToolCall(
        name="repair.orchestrate_validate",
        arguments={"command": command, "previous_command": command, "proposed_strategy": ""},
        id="repair_orchestrate_retry",
    )

    result = registry.execute(call, _approved_context(memory, tmp_path, call, allow_shell=True))

    assert result.success
    assert result.data["validation"]["success"] is False
    assert result.data["diagnosis"]["classification"] == "test_failure"
    assert result.data["recall"]["hits"]
    assert result.data["retry_gate"]["retry_allowed"] is False
    assert result.data["retry_gate"]["must_change_strategy_before_retry"] is True
    assert result.data["next_action"] == "change_strategy_before_retry"


def test_repair_orchestrate_validate_allows_changed_strategy_after_recall(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "switch", "-c", "codex/repair-loop-change"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.PROCEDURAL,
            kind=MemoryKind.PROCEDURE,
            title="AssertionError repair lesson",
            content="When repair validation reports AssertionError: expected fixed, inspect the failing assertion before retrying.",
            confidence=0.9,
        )
    )
    registry = build_default_tools()
    command = ["python", "-c", "raise AssertionError('expected fixed')"]
    call = ToolCall(
        name="repair.orchestrate_validate",
        arguments={
            "command": command,
            "previous_command": command,
            "proposed_strategy": "Inspect the failing assertion and update the expected value before retrying.",
        },
        id="repair_orchestrate_changed",
    )

    result = registry.execute(call, _approved_context(memory, tmp_path, call, allow_shell=True))

    assert result.success
    assert result.data["validation"]["success"] is False
    assert result.data["recall"]["hits"]
    assert result.data["retry_gate"]["retry_allowed"] is True
    assert result.data["next_action"] == "apply_changed_strategy_then_retry"


def test_default_registry_includes_spec_tools() -> None:
    registry = build_default_tools()
    names = {spec.name for spec in registry.specs()}
    assert {
        "repair.prepare",
        "repair.status",
        "repair.apply_patch",
        "repair.validate",
        "repair.rollback",
        "repair.orchestrate_validate",
        "repair.review",
        "diagnosis.classify",
        "diagnosis.recall",
        "repo.search",
        "repo.map",
        "patch.apply",
        "test.run",
        "lint.run",
        "git.status",
        "git.diff",
        "git.export_patch",
        "git.branch",
        "git.create_local_branch",
        "git.commit",
        "memvid.verify",
        "memvid.doctor",
        "memvid.stats",
        "memory.inspect",
        "memory.export",
        "memory.import",
        "memory.learn",
        "memory.consolidate",
        "context.pack",
        "context.expand",
        "capsule.summarize",
        "capsule.apply",
        "memory.conflicts",
        "codex.exec",
    } <= names


def test_codex_exec_requires_approval_by_default(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="codex.exec", arguments={"prompt": "summarize this repo"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "tool_disabled"


def test_codex_exec_runs_when_enabled(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="codex done", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.command_tools.subprocess.run", fake_run)

    result = registry.execute(
        ToolCall(
            name="codex.exec",
            arguments={
                "prompt": "summarize this repo",
                "model": "gpt-test",
                "sandbox": "workspace-write",
                "timeout": 45,
            },
            id="codex1",
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_codex_cli=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"codex1"}),
            approved_tool_call_arguments={
                "codex1": {
                    "prompt": "summarize this repo",
                    "model": "gpt-test",
                    "sandbox": "workspace-write",
                    "timeout": 45,
                }
            },
        ),
    )

    assert result.success
    assert "codex done" in result.content
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["codex", "exec"]
    assert ["--cd", str(tmp_path.resolve())] == command[2:4]
    assert ["--sandbox", "workspace-write"] == command[4:6]
    assert "--ephemeral" in command
    assert command[-1] == "summarize this repo"


def test_repo_search_stays_in_workspace(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("needle lives here", encoding="utf-8")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="repo.search", arguments={"query": "needle"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert result.success
    assert "note.txt" in result.content


def test_patch_apply_requires_file_write_enablement(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="patch.apply", arguments={"patch": "diff --git a/a.txt b/a.txt\n"}),
        ToolContext(memory=memory, config=AgentConfig(allow_file_write=False), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "tool_disabled"


def test_file_write_still_blocks_path_escape_when_enabled(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(
            name="file.write", arguments={"path": "../outside.txt", "content": "no"}, id="write1"
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_file_write=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"write1"}),
            approved_tool_call_arguments={"write1": {"path": "../outside.txt", "content": "no"}},
        ),
    )

    assert not result.success
    assert result.error == "file_write_failed"
    assert not (tmp_path.parent / "outside.txt").exists()


def test_lint_run_uses_shell_enablement_and_allowlist(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    blocked = registry.execute(
        ToolCall(name="lint.run", arguments={"command": ["ruff", "check", "."]}),
        ToolContext(memory=memory, config=AgentConfig(allow_shell=False), workspace=tmp_path),
    )
    assert blocked.error == "tool_disabled"

    allowed = registry.execute(
        ToolCall(
            name="lint.run",
            arguments={"command": [sys.executable, "-m", "compileall", "-q", "."]},
            id="lint1",
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"lint1"}),
            approved_tool_call_arguments={
                "lint1": {"command": [sys.executable, "-m", "compileall", "-q", "."]}
            },
        ),
    )

    assert allowed.success
    assert "exit_code=0" in allowed.content


def test_repair_e2e_smoke_reaches_reviewed_commit_gate_after_seeded_failure(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calculator.py").write_text(
        "from calculator import add\n\ndef test_adds_numbers():\n    assert add(2, 3) == 5\n"
    )
    subprocess.run(
        ["git", "add", "calculator.py", "test_calculator.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "seed failing calculator",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    prepare = ToolCall(
        name="repair.prepare", arguments={"branch": "codex/repair-calculator"}, id="prepare_e2e"
    )
    prepared = registry.execute(prepare, _approved_context(memory, tmp_path, prepare))
    assert prepared.success

    patch_call = ToolCall(
        name="repair.apply_patch",
        arguments={
            "patch": "diff --git a/calculator.py b/calculator.py\n"
            "--- a/calculator.py\n"
            "+++ b/calculator.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a - b\n"
            "+    return a + b\n"
        },
        id="patch_e2e",
    )
    patched = registry.execute(patch_call, _approved_context(memory, tmp_path, patch_call))
    assert patched.success

    validation_call = ToolCall(
        name="repair.orchestrate_validate",
        arguments={"command": ["python", "-m", "pytest", "-q", "test_calculator.py"]},
        id="validate_e2e",
    )
    validation = registry.execute(
        validation_call, _approved_context(memory, tmp_path, validation_call, allow_shell=True)
    )
    assert validation.success
    assert validation.data["validation"]["success"] is True
    assert validation.data["next_action"] == "create_repair_review_before_commit"
    assert validation.data["commit_allowed"] is False

    review = registry.execute(
        ToolCall(
            name="repair.review",
            arguments={
                "validation": validation.data["validation"],
                "summary": "Calculator repair validated by targeted pytest.",
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert review.success
    assert review.data["commit_gate"]["commit_allowed"] is True
    assert review.data["commit_gate"]["approval_required_before_commit"] is True
    assert (tmp_path / ".nest" / "repair_reviews" / f"{review.data['review_id']}.json").exists()

    commit_call = ToolCall(
        name="git.commit",
        arguments={
            "message": "repair calculator add",
            "repair_review_id": review.data["review_id"],
        },
        id="commit_e2e",
    )
    blocked_commit = registry.execute(
        commit_call, ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path)
    )
    assert blocked_commit.error == "tool_disabled"

    approval_blocked_commit = registry.execute(
        commit_call,
        ToolContext(memory=memory, config=AgentConfig(allow_git_commit=True), workspace=tmp_path),
    )
    assert approval_blocked_commit.error == "approval_required"

    approved_commit = registry.execute(
        commit_call, _approved_context(memory, tmp_path, commit_call)
    )
    assert approved_commit.success
    assert approved_commit.data["repair_review_id"] == review.data["review_id"]
    assert approved_commit.data["commit_sha"]
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "repair calculator add"
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""


def test_stale_repair_review_blocks_commit_and_rollback_preserves_artifact(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    subprocess.run(
        ["git", "add", "calculator.py"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "seed calculator",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    prepare = ToolCall(
        name="repair.prepare", arguments={"branch": "codex/repair-stale-review"}, id="prepare_stale"
    )
    assert registry.execute(prepare, _approved_context(memory, tmp_path, prepare)).success
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    validation = {
        "success": True,
        "returncode": 0,
        "command": ["pytest", "-q"],
        "content": "passed",
    }
    review = registry.execute(
        ToolCall(
            name="repair.review",
            arguments={"validation": validation, "summary": "Calculator fix reviewed."},
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert review.success

    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a + b + 1\n")
    commit_call = ToolCall(
        name="git.commit",
        arguments={"message": "repair calculator", "repair_review_id": review.data["review_id"]},
        id="commit_stale",
    )
    stale_commit = registry.execute(commit_call, _approved_context(memory, tmp_path, commit_call))
    assert stale_commit.error == "repair_review_stale"

    rollback_call = ToolCall(
        name="repair.rollback",
        arguments={"reason": "stale_repair_review", "review_id": review.data["review_id"]},
        id="rollback_stale",
    )
    rolled_back = registry.execute(
        rollback_call, _approved_context(memory, tmp_path, rollback_call)
    )
    assert rolled_back.success
    assert (tmp_path / "calculator.py").read_text() == "def add(a, b):\n    return a - b\n"
    artifact_path = tmp_path / rolled_back.data["rollback_artifact"]
    assert artifact_path.exists()
    artifact = json.loads(artifact_path.read_text())
    assert artifact["reason"] == "stale_repair_review"
    assert artifact["review_id"] == review.data["review_id"]
    assert artifact["before"]["changed_files"] == ["calculator.py"]
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""


def test_repair_review_creates_commit_gate_after_successful_validation(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "switch", "-c", "codex/repair-review"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "README.md").write_text("patched\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="repair.review",
        arguments={
            "validation": {"success": True, "command": ["pytest", "-q"], "content": "passed"},
            "summary": "README patch validated with tests.",
        },
    )

    result = registry.execute(
        call, ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path)
    )

    assert result.success
    assert result.data["commit_gate"]["commit_allowed"] is True
    assert result.data["commit_gate"]["approval_required_before_commit"] is True
    review_path = tmp_path / ".nest" / "repair_reviews" / f"{result.data['review_id']}.json"
    assert review_path.exists()
    assert json.loads(review_path.read_text())["diff_hash"] == result.data["diff_hash"]


def test_git_commit_blocks_repair_branch_without_reviewer_gate(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "switch", "-c", "codex/repair-no-review"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "README.md").write_text("patched\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="git.commit", arguments={"message": "repair commit"}, id="commit_repair_no_review"
    )

    result = registry.execute(call, _approved_context(memory, tmp_path, call))
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    assert not result.success
    assert result.error == "repair_review_required"
    assert after == before


def test_git_commit_allows_repair_branch_with_current_reviewer_gate(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "switch", "-c", "codex/repair-reviewed"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "README.md").write_text("patched\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    review = registry.execute(
        ToolCall(
            name="repair.review",
            arguments={
                "validation": {"success": True, "command": ["pytest", "-q"]},
                "summary": "validated",
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    call = ToolCall(
        name="git.commit",
        arguments={"message": "repair commit", "repair_review_id": review.data["review_id"]},
        id="commit_repair_reviewed",
    )

    result = registry.execute(call, _approved_context(memory, tmp_path, call))
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.success
    assert log.stdout.strip() == "repair commit"
    assert result.data["repair_review_id"] == review.data["review_id"]


def test_git_commit_requires_approval_and_never_pushes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(name="git.commit", arguments={"message": "test commit"}, id="commit1")

    blocked = registry.execute(
        call,
        ToolContext(memory=memory, config=AgentConfig(allow_git_commit=True), workspace=tmp_path),
    )
    assert blocked.error == "approval_required"

    captured: dict[str, object] = {"commands": []}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["commands"].append(command)  # type: ignore[union-attr]
        captured["kwargs"] = kwargs
        if command == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, stdout="abc123\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="[main abc] test commit", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.git_tools.subprocess.run", fake_run)
    approved = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_git_commit=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"commit1"}),
            approved_tool_call_arguments={"commit1": {"message": "test commit"}},
        ),
    )

    assert approved.success
    commands = captured["commands"]
    assert isinstance(commands, list)
    assert ["git", "commit", "-m", "test commit"] in commands
    assert all("push" not in command for command in commands)


def test_git_create_local_branch_is_local_only_and_approval_gated(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="git.create_local_branch",
        arguments={"branch": "kestrel/self-improve/test"},
        id="branch1",
    )

    blocked = registry.execute(
        call, ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path)
    )
    assert blocked.error == "approval_required"

    protected_call = ToolCall(
        name="git.create_local_branch", arguments={"branch": "main"}, id="branch_main"
    )
    protected = registry.execute(
        protected_call, _approved_context(memory, tmp_path, protected_call)
    )
    assert protected.error == "protected_branch"

    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if command == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(command, 0, stdout="feature\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="Switched to a new branch", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.git_tools.subprocess.run", fake_run)
    result = registry.execute(call, _approved_context(memory, tmp_path, call))

    assert result.success
    assert ["git", "switch", "-c", "kestrel/self-improve/test"] in calls
    assert all("push" not in command for command in calls)


def test_git_export_patch_writes_local_improvement_patch(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    patch_text = "diff --git a/a.txt b/a.txt\n+hello\n"
    call = ToolCall(
        name="git.export_patch",
        arguments={"path": ".kestrel/improvements/demo/diff.patch"},
        id="export_patch",
    )

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert command == ["git", "diff"]
        return subprocess.CompletedProcess(command, 0, stdout=patch_text, stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.git_tools.subprocess.run", fake_run)
    result = registry.execute(call, _approved_context(memory, tmp_path, call))

    assert result.success
    assert result.data["path"] == ".kestrel/improvements/demo/diff.patch"
    assert (
        tmp_path / ".kestrel" / "improvements" / "demo" / "diff.patch"
    ).read_text() == patch_text


def test_git_commit_refuses_protected_branches(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(name="git.commit", arguments={"message": "test commit"}, id="commit_protected")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if command == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(command, 0, stdout="main\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="should not commit", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.git_tools.subprocess.run", fake_run)
    result = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_git_commit=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"commit_protected"}),
            approved_tool_call_arguments={"commit_protected": {"message": "test commit"}},
        ),
    )

    assert result.error == "protected_branch"
    assert ["git", "commit", "-m", "test commit"] not in calls


def test_memory_inspect_export_and_import_are_structured_and_gated(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            title="Structured export fact",
            content="Memory export returns structured JSON.",
            confidence=0.9,
        )
    )
    registry = build_default_tools()

    inspected = registry.execute(
        ToolCall(
            name="memory.inspect", arguments={"query": "structured JSON", "layers": ["semantic"]}
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert inspected.success
    assert json.loads(inspected.content)[0]["record"]["layer"] == "semantic"

    exported = registry.execute(
        ToolCall(name="memory.export", arguments={"layers": ["semantic"]}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert exported.success
    assert json.loads(exported.content)[0]["title"] == "Structured export fact"

    import_call = ToolCall(
        name="memory.import",
        arguments={
            "records": [
                {
                    "layer": "semantic",
                    "kind": "fact",
                    "title": "Imported fact",
                    "content": "Approved import writes non-policy memory.",
                }
            ]
        },
        id="import1",
    )
    blocked = registry.execute(
        import_call,
        ToolContext(
            memory=memory, config=AgentConfig(allow_memory_import=True), workspace=tmp_path
        ),
    )
    assert blocked.error == "approval_required"

    imported = registry.execute(
        import_call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_memory_import=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"import1"}),
            approved_tool_call_arguments={"import1": import_call.arguments},
        ),
    )
    assert imported.success
    assert memory.retrieve(
        RetrievalQuery(query="Approved import", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3)
    )


def test_memory_import_keeps_policy_writes_separately_gated(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(
            name="memory.import",
            arguments={
                "records": [
                    {
                        "layer": "policy",
                        "kind": "policy",
                        "title": "Imported policy",
                        "content": "Never write policy memory without explicit policy enablement.",
                    }
                ]
            },
            id="import_policy",
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_memory_import=True, allow_policy_writes=False),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"import_policy"}),
            approved_tool_call_arguments={
                "import_policy": {
                    "records": [
                        {
                            "layer": "policy",
                            "kind": "policy",
                            "title": "Imported policy",
                            "content": "Never write policy memory without explicit policy enablement.",
                        }
                    ]
                }
            },
        ),
    )

    assert not result.success
    assert result.error == "policy_write_disabled"


def test_memory_consolidate_promotes_repeated_procedure(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.PROCEDURE,
            title="Repeatable test recipe",
            content="Repeatable test recipe: run pytest -q after tool changes.",
            confidence=0.9,
            importance=0.8,
        )
    )
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(
            name="memory.consolidate",
            arguments={
                "query": "Repeatable test recipe",
                "source_layer": "episodic",
                "validation_score": 0.9,
                "repeat_count": 2,
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert result.success
    assert '"target_layer": "procedural"' in result.content
    assert memory.retrieve(
        RetrievalQuery(
            query="Repeatable test recipe", layers=(MemoryLayer.PROCEDURAL,), k_per_layer=3
        )
    )


def test_memory_learn_routes_validated_signal(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": "Validated project fact",
                "content": "The agent stores one .mv2 file per memory layer.",
                "kind": "fact",
                "source_layer": "episodic",
                "validation_score": 0.84,
                "repeat_count": 1,
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["target_layer"] == "semantic"
    hits = memory.retrieve(
        RetrievalQuery(
            query=".mv2 file per memory layer", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3
        )
    )
    assert hits
    assert hits[0].record.metadata["nested_learning"]["context_flow"]["id"] == "episode_to_semantic"
    assert hits[0].record.metadata["validation_evidence"]["legacy_raw_score"] is True


def test_memory_learn_accepts_structured_validation_evidence(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": "Structured evidence fact",
                "content": "Structured validation evidence should compute the score.",
                "kind": "fact",
                "source_layer": "episodic",
                "validation_evidence": {
                    "test_refs": [{"source": "test.run", "locator": "pytest -q"}],
                    "lint_refs": [{"source": "lint.run", "locator": "ruff check"}],
                    "repair_refs": [{"source": "repair.validate", "locator": "compileall"}],
                    "review_refs": [{"source": "repair.review", "locator": "review-1"}],
                },
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["validation_score"] == 1.0
    hits = memory.retrieve(
        RetrievalQuery(query="Structured validation evidence", layers=(MemoryLayer.SEMANTIC,))
    )
    assert hits
    assert hits[0].record.metadata["validation_evidence"]["test_refs"] == ["test.run:pytest -q"]


def test_memory_learn_blocks_policy_without_config_enablement(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": "Policy candidate",
                "content": "Always require explicit review before changing policy memory.",
                "kind": "policy",
                "source_layer": "procedural",
                "target_layer": "policy",
                "validation_score": 0.99,
                "repeat_count": 5,
                "explicit_instruction": True,
            },
        ),
        ToolContext(
            memory=memory, config=AgentConfig(allow_policy_writes=False), workspace=tmp_path
        ),
    )

    assert not result.success
    assert result.error == "policy_write_disabled"
    assert not memory.retrieve(
        RetrievalQuery(
            query="explicit review before changing policy",
            layers=(MemoryLayer.POLICY,),
            k_per_layer=3,
        )
    )


def test_memory_correct_writes_correction_and_hides_superseded_target(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    target_id = memory.put(
        MemoryRecord(
            id="fact-alpha",
            title="Feature alpha",
            content="Feature alpha is enabled.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.86,
        )
    )

    result = build_default_tools().execute(
        ToolCall(
            name="memory.correct",
            arguments={
                "target_record_id": target_id,
                "correction_text": "Feature alpha is not enabled.",
                "evidence": [
                    {"source": "user", "locator": "turn-1", "quote": "actually, alpha is off"}
                ],
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["target_record_id"] == target_id
    active_hits = memory.retrieve(
        RetrievalQuery(query="Feature alpha enabled", layers=(MemoryLayer.SEMANTIC,))
    )
    assert all(hit.record.id != target_id for hit in active_hits)
    audit_hits = memory.retrieve(
        RetrievalQuery(
            query="Feature alpha enabled", layers=(MemoryLayer.SEMANTIC,), include_inactive=True
        )
    )
    assert any(hit.record.id == target_id for hit in audit_hits)
    correction_hits = memory.retrieve(
        RetrievalQuery(query="Feature alpha not enabled", layers=(MemoryLayer.SEMANTIC,))
    )
    assert correction_hits
    assert correction_hits[0].record.kind == MemoryKind.CORRECTION
    assert correction_hits[0].record.metadata["corrects"] == [target_id]


def test_memory_compact_dry_run_reports_without_tombstoning(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            id="old-working",
            title="Old working note",
            content="Old working note says compaction should summarize expired scratch state.",
            layer=MemoryLayer.WORKING,
            confidence=0.5,
            created_at=datetime.now(UTC) - timedelta(days=30),
            metadata={"validation_score": 0.7},
        )
    )

    result = build_default_tools().execute(
        ToolCall(name="memory.compact", arguments={"layer": "working"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["dry_run"] is True
    assert result.data["layer"] == "working"
    assert result.data["candidate_count"] >= 1
    assert memory.retrieve(
        RetrievalQuery(query="expired scratch state", layers=(MemoryLayer.WORKING,))
    )
