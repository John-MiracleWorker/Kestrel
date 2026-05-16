from __future__ import annotations

import json
import subprocess
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


def test_malformed_tool_arguments_fail_cleanly(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    result = registry.execute(
        ToolCall(name="memory.search", arguments="not an object"),  # type: ignore[arg-type]
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert not result.success
    assert result.error == "invalid_tool_arguments"


def test_high_risk_tool_with_allow_flag_still_requests_approval(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}, id="shell1"),
        ToolContext(memory=memory, config=AgentConfig(allow_shell=True), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "approval_required"


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
        ToolCall(name="diagnosis.recall", arguments={"failure_text": "ModuleNotFoundError: nested_memvid_agent", "k": 3}),
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
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "seed"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True)
    return base.stdout.strip()


def _approved_context(memory: object, tmp_path: Path, call: ToolCall, *, allow_shell: bool = False) -> ToolContext:
    return ToolContext(
        memory=memory,  # type: ignore[arg-type]
        config=AgentConfig(allow_file_write=True, allow_shell=allow_shell),
        workspace=tmp_path,
        approved_tool_call_ids=frozenset({call.id}),
        approved_tool_call_arguments={call.id: call.arguments},
    )


def test_repair_prepare_creates_approved_branch_from_clean_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(name="repair.prepare", arguments={"branch": "codex/repair-test"}, id="repair_prepare")

    result = registry.execute(
        call,
        _approved_context(memory, tmp_path, call),
    )

    assert result.success
    assert result.data["branch"] == "codex/repair-test"
    assert result.data["base_sha"]
    assert result.data["mode"] == "branch"
    current = subprocess.run(["git", "branch", "--show-current"], cwd=tmp_path, check=True, capture_output=True, text=True)
    assert current.stdout.strip() == "codex/repair-test"


def test_repair_prepare_refuses_dirty_repo_by_default(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(name="repair.prepare", arguments={"branch": "codex/dirty-test"}, id="repair_dirty")

    result = registry.execute(
        call,
        _approved_context(memory, tmp_path, call),
    )

    assert not result.success
    assert result.error == "dirty_worktree"


def test_repair_status_reports_active_repair_branch_and_changed_files(tmp_path: Path) -> None:
    base = _init_git_repo(tmp_path)
    subprocess.run(["git", "switch", "-c", "codex/repair-status"], cwd=tmp_path, check=True, capture_output=True, text=True)
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
    subprocess.run(["git", "switch", "-c", "codex/repair-apply"], cwd=tmp_path, check=True, capture_output=True, text=True)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    patch_text = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-seed\n+patched\n"
    apply_call = ToolCall(name="repair.apply_patch", arguments={"patch": patch_text}, id="repair_apply")

    applied = registry.execute(apply_call, _approved_context(memory, tmp_path, apply_call))

    assert applied.success
    assert (tmp_path / "README.md").read_text() == "patched\n"

    validate_call = ToolCall(
        name="repair.validate",
        arguments={"command": ["python", "-c", "import sys; print('bad'); sys.exit(2)"]},
        id="repair_validate",
    )
    validated = registry.execute(validate_call, _approved_context(memory, tmp_path, validate_call, allow_shell=True))
    assert not validated.success
    assert validated.error == "repair_validation_failed"
    assert validated.data["diagnosis"]["classification"] in {"tool_failure", "unknown_failure"}

    rollback_call = ToolCall(name="repair.rollback", arguments={}, id="repair_rollback")
    rolled_back = registry.execute(rollback_call, _approved_context(memory, tmp_path, rollback_call))
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


def test_repair_orchestrate_validate_recalls_lessons_and_blocks_unchanged_retry(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(["git", "switch", "-c", "codex/repair-loop"], cwd=tmp_path, check=True, capture_output=True, text=True)
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
    subprocess.run(["git", "switch", "-c", "codex/repair-loop-change"], cwd=tmp_path, check=True, capture_output=True, text=True)
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
        "git.branch",
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

    monkeypatch.setattr("nested_memvid_agent.tools.builtin.subprocess.run", fake_run)

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
        ToolCall(name="file.write", arguments={"path": "../outside.txt", "content": "no"}, id="write1"),
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


def test_lint_run_uses_shell_enablement_and_allowlist(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()

    blocked = registry.execute(
        ToolCall(name="lint.run", arguments={"command": ["ruff", "check", "."]}),
        ToolContext(memory=memory, config=AgentConfig(allow_shell=False), workspace=tmp_path),
    )
    assert blocked.error == "tool_disabled"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["ruff", "check", "."]
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(command, 0, stdout="clean", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.builtin.subprocess.run", fake_run)
    allowed = registry.execute(
        ToolCall(name="lint.run", arguments={"command": ["ruff", "check", "."]}, id="lint1"),
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"lint1"}),
            approved_tool_call_arguments={"lint1": {"command": ["ruff", "check", "."]}},
        ),
    )

    assert allowed.success
    assert "clean" in allowed.content


def test_repair_review_creates_commit_gate_after_successful_validation(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(["git", "switch", "-c", "codex/repair-review"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "README.md").write_text("patched\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True, text=True)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="repair.review",
        arguments={
            "validation": {"success": True, "command": ["pytest", "-q"], "content": "passed"},
            "summary": "README patch validated with tests.",
        },
    )

    result = registry.execute(call, ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path))

    assert result.success
    assert result.data["commit_gate"]["commit_allowed"] is True
    assert result.data["commit_gate"]["approval_required_before_commit"] is True
    review_path = tmp_path / ".nest" / "repair_reviews" / f"{result.data['review_id']}.json"
    assert review_path.exists()
    assert json.loads(review_path.read_text())["diff_hash"] == result.data["diff_hash"]


def test_git_commit_blocks_repair_branch_without_reviewer_gate(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(["git", "switch", "-c", "codex/repair-no-review"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "README.md").write_text("patched\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True, text=True)
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(name="git.commit", arguments={"message": "repair commit"}, id="commit_repair_no_review")

    result = registry.execute(call, _approved_context(memory, tmp_path, call))
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    assert not result.success
    assert result.error == "repair_review_required"
    assert after == before


def test_git_commit_allows_repair_branch_with_current_reviewer_gate(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "switch", "-c", "codex/repair-reviewed"], cwd=tmp_path, check=True, capture_output=True, text=True)
    (tmp_path / "README.md").write_text("patched\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True, text=True)
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    review = registry.execute(
        ToolCall(
            name="repair.review",
            arguments={"validation": {"success": True, "command": ["pytest", "-q"]}, "summary": "validated"},
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    call = ToolCall(
        name="git.commit",
        arguments={"message": "repair commit", "repair_review_id": review.data["review_id"]},
        id="commit_repair_reviewed",
    )

    result = registry.execute(call, _approved_context(memory, tmp_path, call))
    log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=tmp_path, check=True, capture_output=True, text=True)

    assert result.success
    assert log.stdout.strip() == "repair commit"
    assert result.data["repair_review_id"] == review.data["review_id"]


def test_git_commit_requires_approval_and_never_pushes(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(name="git.commit", arguments={"message": "test commit"}, id="commit1")

    blocked = registry.execute(call, ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path))
    assert blocked.error == "approval_required"

    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="[main abc] test commit", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.builtin.subprocess.run", fake_run)
    approved = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"commit1"}),
        ),
    )

    assert approved.success
    assert captured["command"] == ["git", "commit", "-m", "test commit"]
    assert "push" not in captured["command"]


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
        ToolCall(name="memory.inspect", arguments={"query": "structured JSON", "layers": ["semantic"]}),
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
    blocked = registry.execute(import_call, ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path))
    assert blocked.error == "approval_required"

    imported = registry.execute(
        import_call,
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"import1"}),
        ),
    )
    assert imported.success
    assert memory.retrieve(RetrievalQuery(query="Approved import", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3))


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
            config=AgentConfig(allow_policy_writes=False),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"import_policy"}),
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
        RetrievalQuery(query="Repeatable test recipe", layers=(MemoryLayer.PROCEDURAL,), k_per_layer=3)
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
    hits = memory.retrieve(RetrievalQuery(query=".mv2 file per memory layer", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3))
    assert hits
    assert hits[0].record.metadata["nested_learning"]["context_flow"]["id"] == "episode_to_semantic"


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
        ToolContext(memory=memory, config=AgentConfig(allow_policy_writes=False), workspace=tmp_path),
    )

    assert not result.success
    assert result.error == "policy_write_disabled"
    assert not memory.retrieve(RetrievalQuery(query="explicit review before changing policy", layers=(MemoryLayer.POLICY,), k_per_layer=3))
