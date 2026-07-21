from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Any, Literal, cast

import pytest
from pytest import MonkeyPatch

import nested_memvid_agent.tools.process_tools as process_tools
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.extension_runner import (
    ContainerExecutionRequest,
    ContainerExecutionResult,
)
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.repair_integrity import repair_snapshot
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution, ToolSpec
from nested_memvid_agent.secret_broker import SecretBroker
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.command_tools import CodexExecTool, _is_python_executable_name
from nested_memvid_agent.tools.process_tools import (
    _run_subprocess,
    _SubprocessToolOutcomeIndeterminate,
    _SubprocessToolTimeout,
    cancel_subprocesses_for_run,
)
from nested_memvid_agent.tools.registry import RetryingRegistry, ToolRegistry
from nested_memvid_agent.validation_runner import (
    IsolatedValidationResult,
)
from nested_memvid_agent.validation_runner import (
    run_isolated_validation as run_real_isolated_validation,
)


@pytest.fixture(autouse=True)
def _isolated_repair_validation_stub(monkeypatch: MonkeyPatch) -> None:
    """Keep repair unit tests deterministic; real OCI coverage is integration-only."""

    class LocalUnitRunner:
        def run(self, request: ContainerExecutionRequest) -> ContainerExecutionResult:
            normalized = list(request.command)
            if normalized and Path(normalized[0]).name.casefold().startswith("python"):
                normalized[0] = sys.executable
            # Windows needs its native runtime and writable temporary-directory
            # variables even in this credential-free unit runner. Without
            # them, pytest falls back to the read-only candidate snapshot and
            # exits before collecting the seeded regression.
            environment = {"PATH": os.defpath}
            for name in (
                "COMSPEC",
                "PATHEXT",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "TMPDIR",
                "WINDIR",
            ):
                if value := os.environ.get(name):
                    environment[name] = value
            completed = subprocess.run(  # noqa: S603  # nosec B603
                normalized,
                cwd=request.source_dir,
                env=environment,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
            return ContainerExecutionResult(
                success=completed.returncode == 0,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                content="Container execution completed.",
                error=None if completed.returncode == 0 else "container_nonzero_exit",
                tree_digest=request.expected_tree_digest,
                scope_digest=request.scopes.digest(),
            )

    def run_stub(
        *,
        workspace: Path,
        image: str | None,
        command: list[str],
        timeout_seconds: float,
        expected_repair_snapshot: dict[str, Any] | None = None,
        runner: object | None = None,
    ) -> IsolatedValidationResult:
        del image, runner
        return run_real_isolated_validation(
            workspace=workspace,
            image="example.invalid/kestrel-validation@sha256:" + "a" * 64,
            command=command,
            timeout_seconds=timeout_seconds,
            expected_repair_snapshot=expected_repair_snapshot,
            runner=LocalUnitRunner(),
        )

    monkeypatch.setattr(process_tools, "run_isolated_validation", run_stub)


def test_windows_process_tree_uses_absolute_system_taskkill(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    windows_root = tmp_path / "Windows"
    taskkill = windows_root / "System32" / "taskkill.exe"
    taskkill.parent.mkdir(parents=True)
    taskkill.write_text("", encoding="utf-8")
    monkeypatch.setenv("SystemRoot", str(windows_root))
    monkeypatch.delenv("WINDIR", raising=False)
    commands: list[list[str]] = []

    class FakeProcess:
        pid = 4242
        killed = False

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            self.killed = True

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    process = FakeProcess()
    monkeypatch.setattr(process_tools.subprocess, "run", fake_run)

    verified = process_tools._terminate_windows_process_tree(  # noqa: SLF001
        cast(Any, process)
    )

    assert commands == [[str(taskkill.resolve()), "/PID", "4242", "/T", "/F"]]
    assert Path(commands[0][0]).is_absolute()
    assert process.killed is False
    assert verified is True


def test_windows_process_tree_falls_back_when_system_taskkill_is_unavailable(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("WINDIR", raising=False)

    class FakeProcess:
        pid = 4242
        killed = False

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            self.killed = True

    process = FakeProcess()
    verified = process_tools._terminate_windows_process_tree(  # noqa: SLF001
        cast(Any, process)
    )

    assert process.killed is True
    assert verified is False


def test_windows_process_tree_rejects_nonzero_taskkill_as_verified(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    windows_root = tmp_path / "Windows"
    taskkill = windows_root / "System32" / "taskkill.exe"
    taskkill.parent.mkdir(parents=True)
    taskkill.write_text("", encoding="utf-8")
    monkeypatch.setenv("SystemRoot", str(windows_root))

    class FakeProcess:
        pid = 4242
        killed = False

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            self.killed = True

    monkeypatch.setattr(
        process_tools.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 128),
    )
    process = FakeProcess()

    verified = process_tools._terminate_windows_process_tree(  # noqa: SLF001
        cast(Any, process)
    )

    assert verified is False
    assert process.killed is True


def test_windows_taskkill_cannot_verify_descendants_after_leader_exit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    windows_root = tmp_path / "Windows"
    taskkill = windows_root / "System32" / "taskkill.exe"
    taskkill.parent.mkdir(parents=True)
    taskkill.write_text("", encoding="utf-8")
    monkeypatch.setenv("SystemRoot", str(windows_root))

    class DeadLeader:
        pid = 4242
        killed = False

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            self.killed = True

    monkeypatch.setattr(
        process_tools.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    verified = process_tools._terminate_windows_process_tree(  # noqa: SLF001
        cast(Any, DeadLeader())
    )

    assert verified is False


def test_windows_popen_is_suspended_until_job_assignment(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    memory = build_memory_system("memory", tmp_path / "memory")

    def fake_popen(command: list[str], **kwargs: Any) -> object:
        observed["command"] = command
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(process_tools.sys, "platform", "win32")
    monkeypatch.setattr(process_tools.subprocess, "Popen", fake_popen)

    process_tools._start_subprocess(  # noqa: SLF001
        ["example.exe"],
        context=ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
        ),
    )

    assert int(observed["creationflags"]) & 0x00000004
    assert int(observed["creationflags"]) & 0x00000200


@pytest.mark.parametrize("mode", ["success", "timeout", "cancel"])
def test_windows_unverified_job_cleanup_is_indeterminate(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    mode: str,
) -> None:
    events: list[str] = []
    memory = build_memory_system("memory", tmp_path / "memory")

    class FakeJob:
        def assign(self, process_id: int) -> bool:
            events.append(f"assign:{process_id}")
            return True

        def resume(self, process_id: int) -> bool:
            events.append(f"resume:{process_id}")
            return True

        def terminate_and_wait(self, *, timeout_seconds: float = 2.0) -> bool:
            del timeout_seconds
            events.append("terminate")
            return False

        def close(self) -> bool:
            events.append("close")
            return True

    class FakeProcess:
        pid = 4242
        returncode = 0

        def communicate(self, *args: object, **kwargs: object) -> tuple[str, str]:
            del args, kwargs
            events.append("communicate")
            if mode == "timeout":
                raise subprocess.TimeoutExpired(["example.exe"], 0.1)
            if mode == "cancel":
                process_tools._cancel_running_subprocess("windows-job-test")  # noqa: SLF001
            return "", ""

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            events.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode

    monkeypatch.setattr(process_tools.sys, "platform", "win32")
    monkeypatch.setattr(process_tools, "_create_windows_process_job", FakeJob)
    monkeypatch.setattr(
        process_tools,
        "_start_subprocess",
        lambda *args, **kwargs: cast(Any, FakeProcess()),
    )
    context = ToolContext(
        memory=memory,
        config=AgentConfig(tool_timeout_seconds=0.1),
        workspace=tmp_path,
        run_id="run-windows-job",
    )

    with pytest.raises(_SubprocessToolOutcomeIndeterminate):
        _run_subprocess(
            ["example.exe"],
            context=context,
            arguments={"_tool_execution_id": "windows-job-test"},
            default_timeout=1,
        )

    assert events[:2] == ["assign:4242", "resume:4242"]
    assert "terminate" in events
    assert events[-1] == "close"


def test_windows_job_quiesces_detached_descendants_after_dead_leader(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[str] = []
    memory = build_memory_system("memory", tmp_path / "memory")

    class FakeJob:
        def assign(self, process_id: int) -> bool:
            events.append("assign")
            return process_id == 4242

        def resume(self, process_id: int) -> bool:
            events.append("resume")
            return process_id == 4242

        def terminate_and_wait(self, *, timeout_seconds: float = 2.0) -> bool:
            del timeout_seconds
            events.append("job-tree-quiesced")
            return True

        def close(self) -> bool:
            events.append("close")
            return True

    class DeadLeaderWithDetachedChild:
        pid = 4242
        returncode = 0

        def communicate(self, *args: object, **kwargs: object) -> tuple[str, str]:
            del args, kwargs
            events.append("leader-exited")
            return "ok", ""

        def poll(self) -> int | None:
            return 0

    monkeypatch.setattr(process_tools.sys, "platform", "win32")
    monkeypatch.setattr(process_tools, "_create_windows_process_job", FakeJob)
    monkeypatch.setattr(
        process_tools,
        "_start_subprocess",
        lambda *args, **kwargs: cast(Any, DeadLeaderWithDetachedChild()),
    )

    completed = _run_subprocess(
        ["example.exe"],
        context=ToolContext(
            memory=memory,
            config=AgentConfig(tool_timeout_seconds=1),
            workspace=tmp_path,
        ),
        arguments={},
        default_timeout=1,
    )

    assert completed.returncode == 0
    assert events == [
        "assign",
        "resume",
        "leader-exited",
        "job-tree-quiesced",
        "close",
    ]


def test_windows_unverified_job_handle_close_is_indeterminate(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")

    class FakeJob:
        def assign(self, process_id: int) -> bool:
            return process_id == 4242

        def resume(self, process_id: int) -> bool:
            return process_id == 4242

        def terminate_and_wait(self, *, timeout_seconds: float = 2.0) -> bool:
            del timeout_seconds
            return True

        def close(self) -> bool:
            return False

    class FakeProcess:
        pid = 4242
        returncode = 0

        def communicate(self, *args: object, **kwargs: object) -> tuple[str, str]:
            del args, kwargs
            return "ok", ""

        def poll(self) -> int | None:
            return 0

    monkeypatch.setattr(process_tools.sys, "platform", "win32")
    monkeypatch.setattr(process_tools, "_create_windows_process_job", FakeJob)
    monkeypatch.setattr(
        process_tools,
        "_start_subprocess",
        lambda *args, **kwargs: cast(Any, FakeProcess()),
    )

    with pytest.raises(
        _SubprocessToolOutcomeIndeterminate,
        match="could not close the kill-on-close Job Object",
    ):
        _run_subprocess(
            ["example.exe"],
            context=ToolContext(
                memory=memory,
                config=AgentConfig(tool_timeout_seconds=1),
                workspace=tmp_path,
            ),
            arguments={},
            default_timeout=1,
        )


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


class SystemExitTool(AgentTool):
    spec = ToolSpec(
        name="system-exit.tool",
        description="Raises a BaseException at the tool boundary.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        del arguments, context
        raise SystemExit(17)


class FlakyRetryTool(AgentTool):
    def __init__(
        self,
        *,
        name: str,
        risk: Literal["low", "medium", "high", "critical"],
        requires_approval: bool = False,
    ) -> None:
        self.spec = ToolSpec(
            name=name,
            description="Returns one transient failure before succeeding.",
            parameters={"type": "object", "properties": {}},
            risk=risk,
            requires_approval=requires_approval,
        )
        self.calls = 0

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        del context
        self.calls += 1
        call = ToolCall(name=self.spec.name, arguments=arguments)
        if self.calls == 1:
            return ToolExecution(
                call=call,
                success=False,
                content="temporary failure",
                error="transient_error",
            )
        return ToolExecution(call=call, success=True, content="ok")


@pytest.mark.parametrize(
    "executable",
    ["python.exe", "python3.exe", "python3.11.exe", "python", "python3.13"],
)
def test_python_executable_allowlist_accepts_cross_platform_names(executable: str) -> None:
    assert _is_python_executable_name(executable) is True


@pytest.mark.parametrize("executable", ["pythonw.exe", "pytest.exe", "python-launcher.exe"])
def test_python_executable_allowlist_rejects_non_interpreters(executable: str) -> None:
    assert _is_python_executable_name(executable) is False


def test_build_default_tools_can_be_limited_to_named_subset() -> None:
    registry = build_default_tools(("memory.search", "file.read"))

    names = [spec.name for spec in registry.specs()]

    assert names == ["memory.search", "file.read"]
    assert registry.spec_for("memory.search") is not None
    assert registry.spec_for("shell.run") is None


def test_legacy_config_cannot_disable_exact_call_approval() -> None:
    config = AgentConfig.from_mapping({"require_approval_for_high_risk_tools": False})

    assert config.require_approval_for_high_risk_tools is True


def test_registry_requires_approval_for_high_risk_spec_even_when_flag_is_false(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    tool = FlakyRetryTool(name="unsafe.manifest", risk="high", requires_approval=False)
    registry = ToolRegistry()
    registry.register(tool)

    result = registry.execute(
        ToolCall(name=tool.spec.name, arguments={}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success is False
    assert result.error == "approval_required"
    assert tool.calls == 0


def test_transparent_retries_never_repeat_high_risk_or_approved_tools(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    inner = ToolRegistry()
    read_only = FlakyRetryTool(name="safe.read", risk="low")
    mutating = FlakyRetryTool(name="unsafe.write", risk="high", requires_approval=True)
    inner.register(read_only)
    inner.register(mutating)
    registry = RetryingRegistry(inner, max_attempts=3, backoff_base_seconds=0)
    context = ToolContext(
        memory=memory,
        config=AgentConfig(require_approval_for_high_risk_tools=False),
        workspace=tmp_path,
        approved_tool_call_ids=frozenset({"approved_mutation"}),
        approved_tool_call_arguments={"approved_mutation": {}},
    )

    safe_result = registry.execute(ToolCall(name="safe.read", arguments={}), context)
    unsafe_result = registry.execute(
        ToolCall(name="unsafe.write", arguments={}, id="approved_mutation"), context
    )

    assert safe_result.success is True
    assert read_only.calls == 2
    assert unsafe_result.success is False
    assert unsafe_result.error == "transient_error"
    assert mutating.calls == 1


def test_transparent_retry_never_repeats_indeterminate_mcp_outcome(tmp_path: Path) -> None:
    class IndeterminateMCPTool(AgentTool):
        spec = ToolSpec(
            name="mcp.test.commit",
            description="Models a remote operation with unknown timeout outcome.",
            parameters={"type": "object", "properties": {}},
        )

        def __init__(self) -> None:
            self.calls = 0

        def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
            del context
            self.calls += 1
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments=arguments),
                success=False,
                content="Remote outcome is indeterminate.",
                data={"outcome_indeterminate": True, "retryable": False},
                error="mcp_tool_outcome_indeterminate",
            )

    memory = build_memory_system("memory", tmp_path / "memory")
    tool = IndeterminateMCPTool()
    inner = ToolRegistry()
    inner.register(tool)
    registry = RetryingRegistry(inner, max_attempts=3, backoff_base_seconds=0)

    result = registry.execute(
        ToolCall(name=tool.spec.name, arguments={}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.error == "mcp_tool_outcome_indeterminate"
    assert tool.calls == 1


@pytest.mark.parametrize(
    "error",
    ["extension_cleanup_pending", "extension_cleanup_unverified"],
)
def test_transparent_retry_never_repeats_unverified_oci_cleanup(
    tmp_path: Path,
    error: str,
) -> None:
    class CleanupFailureTool(AgentTool):
        spec = ToolSpec(
            name="test.oci-cleanup",
            description="Models a retained OCI cleanup identity.",
            parameters={"type": "object", "properties": {}},
        )

        def __init__(self) -> None:
            self.calls = 0

        def run(
            self,
            arguments: dict[str, Any],
            context: ToolContext,
        ) -> ToolExecution:
            del context
            self.calls += 1
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments=arguments),
                success=False,
                content="OCI cleanup requires operator reconciliation.",
                error=error,
            )

    memory = build_memory_system("memory", tmp_path / "memory")
    tool = CleanupFailureTool()
    inner = ToolRegistry()
    inner.register(tool)
    registry = RetryingRegistry(inner, max_attempts=3, backoff_base_seconds=0)

    result = registry.execute(
        ToolCall(name=tool.spec.name, arguments={}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.error == error
    assert tool.calls == 1


def test_file_write_is_atomic_and_rejects_secret_or_symlink_targets(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools(("file.write",))
    approved_arguments = {
        "write_safe": {"path": "nested/result.txt", "content": "safe"},
        "write_secret": {"path": ".nest/secrets/token", "content": "no"},
        "write_symlink": {"path": "linked/escape.txt", "content": "no"},
    }
    context = ToolContext(
        memory=memory,
        config=AgentConfig(
            allow_file_write=True,
            require_approval_for_high_risk_tools=False,
        ),
        workspace=tmp_path,
        approved_tool_call_ids=frozenset(approved_arguments),
        approved_tool_call_arguments=approved_arguments,
    )

    written = registry.execute(
        ToolCall(
            name="file.write",
            arguments={"path": "nested/result.txt", "content": "safe"},
            id="write_safe",
        ),
        context,
    )
    assert written.success is True
    assert (tmp_path / "nested" / "result.txt").read_text() == "safe"
    assert not list((tmp_path / "nested").glob(".kestrel-write-*"))

    secret = registry.execute(
        ToolCall(
            name="file.write",
            arguments={"path": ".nest/secrets/token", "content": "no"},
            id="write_secret",
        ),
        context,
    )
    assert secret.success is False
    assert not (tmp_path / ".nest" / "secrets" / "token").exists()

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    linked = registry.execute(
        ToolCall(
            name="file.write",
            arguments={"path": "linked/escape.txt", "content": "no"},
            id="write_symlink",
        ),
        context,
    )
    assert linked.success is False
    assert not (outside / "escape.txt").exists()


def test_repo_search_does_not_follow_workspace_file_symlinks(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools(("repo.search",))
    context = ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path)
    outside = tmp_path.parent / f"outside-search-{tmp_path.name}.txt"
    outside.write_text("do-not-leak-this-value", encoding="utf-8")
    (tmp_path / "leak.txt").symlink_to(outside)

    try:
        result = registry.execute(
            ToolCall(name="repo.search", arguments={"query": "do-not-leak"}),
            context,
        )
    finally:
        outside.unlink(missing_ok=True)

    assert result.success is True
    assert "do-not-leak-this-value" not in result.content
    assert result.data == {"matches": []}


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks may require privileges on Windows")
def test_workspace_enumeration_skips_symlink_loops(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools(("file.list", "file.find", "repo.map"))
    context = ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path)
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    (tmp_path / "loop").symlink_to("loop")

    results = (
        registry.execute(ToolCall(name="file.list", arguments={}), context),
        registry.execute(
            ToolCall(name="file.find", arguments={"pattern": "*"}),
            context,
        ),
        registry.execute(ToolCall(name="repo.map", arguments={}), context),
    )

    assert all(result.success for result in results)
    payload = json.dumps([result.data for result in results])
    assert "safe.txt" in payload
    assert "loop" not in payload


def test_run_manager_registry_honors_enabled_tools_config(tmp_path: Path) -> None:
    class _Events:
        pass

    class _Plugins:
        def sync_all(self) -> None:
            pass

    class _Skills:
        def discover(self) -> None:
            pass

        def tool_adapters(self, *, include_disabled: bool = False) -> list[AgentTool]:
            del include_disabled
            return []

    class _MCP:
        def tool_adapters(self, *, include_disabled: bool = False) -> list[AgentTool]:
            del include_disabled
            return []

    manager = RunManager(
        config=AgentConfig(
            memory_dir=tmp_path / "memory",
            state_path=tmp_path / "state.db",
            enabled_tools=("memory.search", "file.read"),
        ),
        state=AgentStateStore(tmp_path / "state.db"),
        events=cast(Any, _Events()),
        mcp=cast(Any, _MCP()),
        skills=cast(Any, _Skills()),
        plugins=cast(Any, _Plugins()),
    )

    names = [spec.name for spec in manager.build_registry().specs()]

    assert names == ["memory.search", "file.read"]


def test_tool_registry_reports_actual_quiesced_outcome_after_deadline(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = ToolRegistry()
    registry.register(SlowTool())
    config = AgentConfig(tool_timeout_seconds=0.01)

    result = registry.execute(
        ToolCall(name="slow.tool", arguments={}),
        ToolContext(memory=memory, config=config, workspace=tmp_path),
    )

    assert result.success is True
    assert result.error is None
    assert result.data["tool_deadline_exceeded"] is True


def test_tool_registry_system_exit_always_signals_worker_completion(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(SystemExitTool())

    result = registry.execute(
        ToolCall(name="system-exit.tool", arguments={}),
        ToolContext(
            memory=build_memory_system("memory", tmp_path / "memory"),
            config=AgentConfig(tool_timeout_seconds=0.05),
            workspace=tmp_path,
        ),
    )

    assert result.success is False
    assert result.error == "tool_execution_failed"
    assert "SystemExit" in result.content


def test_low_risk_memory_timeout_quiesces_before_memory_close(tmp_path: Path) -> None:
    calls: list[str] = []

    class SlowRetrievalMemory:
        closed = False

        def retrieve(self, _query: RetrievalQuery) -> list[object]:
            for layer in ("working", "episodic", "semantic"):
                sleep(0.04)
                assert self.closed is False
                calls.append(layer)
            # Model retrieval metadata write-back as part of the same owned
            # operation: it too must settle before registry terminality.
            calls.append("retrieval_metadata_upsert")
            return []

        def close_all(self) -> None:
            self.closed = True
            calls.append("closed")

    memory = SlowRetrievalMemory()
    registry = build_default_tools(("memory.search",))
    result = registry.execute(
        ToolCall(name="memory.search", arguments={"query": "slow"}),
        ToolContext(
            memory=cast(Any, memory),
            config=AgentConfig(tool_timeout_seconds=0.01),
            workspace=tmp_path,
        ),
    )
    memory.close_all()

    assert result.success is True
    assert result.data["tool_deadline_exceeded"] is True
    assert calls == [
        "working",
        "episodic",
        "semantic",
        "retrieval_metadata_upsert",
        "closed",
    ]


def test_high_risk_file_write_never_mutates_after_terminal_result(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    import nested_memvid_agent.tools.workspace_tools as workspace_tools

    original_write = workspace_tools._atomic_workspace_write
    write_started = False

    def delayed_write(workspace: Path, path: Path, text: str) -> None:
        nonlocal write_started
        write_started = True
        sleep(0.1)
        original_write(workspace, path, text)

    monkeypatch.setattr(workspace_tools, "_atomic_workspace_write", delayed_write)
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        name="file.write",
        arguments={"path": "delayed.txt", "content": "durable"},
        id="delayed-file-write",
    )

    result = build_default_tools(("file.write",)).execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_file_write=True, tool_timeout_seconds=0.01),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )

    assert write_started is True
    assert result.success is True
    assert (tmp_path / "delayed.txt").read_text(encoding="utf-8") == "durable"
    snapshot = (tmp_path / "delayed.txt").stat().st_mtime_ns
    sleep(0.05)
    assert (tmp_path / "delayed.txt").stat().st_mtime_ns == snapshot


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_subprocess_timeout_kills_term_ignoring_descendants(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    code = (
        "import signal,subprocess,sys,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);time.sleep(30)']);"
        f"open({str(child_pid_path)!r},'w').write(str(child.pid));"
        "time.sleep(30)"
    )
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / "memory"),
        config=AgentConfig(tool_timeout_seconds=0.2),
        workspace=tmp_path,
    )

    with pytest.raises(_SubprocessToolTimeout):
        _run_subprocess(
            [sys.executable, "-c", code],
            context=context,
            arguments={"timeout": 0.2, "_tool_call_id": "timeout-tree"},
            default_timeout=1,
        )

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(20):
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(child_pid)],
            check=False,
            text=True,
            capture_output=True,
        )
        process_state = status.stdout.strip()
        if status.returncode != 0 or not process_state or process_state.startswith("Z"):
            break
        sleep(0.05)
    else:
        pytest.fail(f"TERM-ignoring descendant survived tool timeout in state {process_state}")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_subprocess_success_quiesces_redirected_background_descendants(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "success-child.pid"
    late_marker = tmp_path / "late-success-mutation.txt"
    child_code = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(0.8);"
        f"pathlib.Path({str(late_marker)!r}).write_text('late')"
    )
    parent_code = (
        "import pathlib,subprocess,sys;"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))"
    )
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / "memory"),
        config=AgentConfig(tool_timeout_seconds=2),
        workspace=tmp_path,
    )

    completed = _run_subprocess(
        [sys.executable, "-c", parent_code],
        context=context,
        arguments={"_tool_call_id": "normal-exit-tree"},
        default_timeout=2,
    )

    assert completed.returncode == 0
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    _assert_pid_gone_or_zombie(child_pid)
    sleep(1.0)
    assert late_marker.exists() is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_subprocess_communicate_failure_still_quiesces_process_group(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    late_marker = tmp_path / "communicate-failure-late.txt"
    code = (
        f"import pathlib,time;time.sleep(0.8);pathlib.Path({str(late_marker)!r}).write_text('late')"
    )

    class CommunicateFailureProxy:
        def __init__(self, process: subprocess.Popen[str]) -> None:
            self._process = process
            self.pid = process.pid

        def communicate(self, *args: object, **kwargs: object) -> tuple[str, str]:
            del args, kwargs
            raise OSError("injected communicate failure")

        def poll(self) -> int | None:
            return self._process.poll()

        def terminate(self) -> None:
            self._process.terminate()

        def kill(self) -> None:
            self._process.kill()

        def wait(self, *args: object, **kwargs: object) -> int:
            return self._process.wait(*args, **kwargs)

    def failing_start(
        command: list[str],
        *,
        context: ToolContext,
        environment: dict[str, str] | None = None,
        pipe_stdin: bool = False,
    ) -> Any:
        del context, pipe_stdin
        process = subprocess.Popen(
            command,
            cwd=tmp_path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        return CommunicateFailureProxy(process)

    monkeypatch.setattr(process_tools, "_start_subprocess", failing_start)
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / "memory"),
        config=AgentConfig(tool_timeout_seconds=2),
        workspace=tmp_path,
    )

    with pytest.raises(OSError, match="injected communicate failure"):
        _run_subprocess(
            [sys.executable, "-c", code],
            context=context,
            arguments={"_tool_execution_id": "communicate-failure"},
            default_timeout=2,
        )

    sleep(1.0)
    assert late_marker.exists() is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_same_public_call_id_across_runs_keeps_process_tracking_isolated(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools(("test.run",))

    def host_supervised_run(command: list[str], **kwargs: Any) -> Any:
        kwargs["require_container_isolation"] = False
        return _run_subprocess(command, **kwargs)

    monkeypatch.setattr(
        "nested_memvid_agent.tools.command_tools._run_subprocess",
        host_supervised_run,
    )

    def invocation(run_id: str) -> tuple[ToolCall, ToolContext, Path]:
        pid_path = tmp_path / f"{run_id}.pid"
        code = (
            "import os,pathlib,signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()));"
            "time.sleep(30)"
        )
        call = ToolCall(
            name="test.run",
            arguments={"command": [sys.executable, "-c", code]},
            id="call_1",
        )
        context = ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True, tool_timeout_seconds=10),
            workspace=tmp_path,
            run_id=run_id,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        )
        return call, context, pid_path

    call_a, context_a, pid_a_path = invocation("run-a")
    call_b, context_b, pid_b_path = invocation("run-b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(registry.execute, call_a, context_a)
        future_b = pool.submit(registry.execute, call_b, context_b)
        for _ in range(200):
            if pid_a_path.is_file() and pid_b_path.is_file():
                break
            sleep(0.01)
        assert pid_a_path.is_file() and pid_b_path.is_file()
        pid_a = int(pid_a_path.read_text(encoding="utf-8"))
        pid_b = int(pid_b_path.read_text(encoding="utf-8"))

        assert cancel_subprocesses_for_run("run-a") == 1
        result_a = future_a.result(timeout=3)
        _assert_pid_gone_or_zombie(pid_a)
        status_b = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid_b)],
            check=False,
            text=True,
            capture_output=True,
        )
        assert status_b.returncode == 0
        assert status_b.stdout.strip() and not status_b.stdout.strip().startswith("Z")

        assert cancel_subprocesses_for_run("run-b") == 1
        result_b = future_b.result(timeout=3)

    assert result_a.success is False
    assert result_b.success is False
    _assert_pid_gone_or_zombie(pid_b)


def test_subprocess_tool_timeout_kills_child_process_and_caps_requested_timeout(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
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

    def host_supervised_run(command: list[str], **kwargs: Any) -> Any:
        kwargs["require_container_isolation"] = False
        return _run_subprocess(command, **kwargs)

    monkeypatch.setattr(
        "nested_memvid_agent.tools.command_tools._run_subprocess",
        host_supervised_run,
    )
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
    memory = build_memory_system(
        "memory", tmp_path / "memory", enforce_stable_write_integrity=False
    )
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


def test_low_risk_memvid_doctor_never_runs_mutating_repairs(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    calls: list[bool] = []

    def doctor(*, dry_run: bool) -> dict[str, bool]:
        calls.append(dry_run)
        return {"ok": True}

    for backend in memory.backends.values():
        monkeypatch.setattr(backend, "doctor", doctor, raising=False)
    registry = build_default_tools(("memvid.doctor",))
    context = ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path)

    rejected = registry.execute(
        ToolCall(name="memvid.doctor", arguments={"dry_run": False}),
        context,
    )
    checked = registry.execute(
        ToolCall(name="memvid.doctor", arguments={}),
        context,
    )

    assert rejected.success is False
    assert rejected.error == "mutating_doctor_disabled"
    assert checked.success is True
    assert calls == [True] * len(memory.backends)


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


def test_tool_registry_tool_filters_active_specs_and_reports_enablement(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    specs = registry.specs()

    result = registry.execute(
        ToolCall(
            name="tool.registry",
            arguments={"query": "shell.run", "enabled": False, "include_parameters": False},
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            tool_specs=tuple(specs),
        ),
    )

    assert result.success is True
    assert result.data["count"] == 1
    assert result.data["tools"][0]["name"] == "shell.run"
    assert result.data["tools"][0]["enabled"] is False
    assert result.data["tools"][0]["enablement_flag"] == "allow_shell"
    assert "parameters" not in result.data["tools"][0]


def test_skill_discover_tool_reports_empty_directory_and_validation_errors(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    skills_dir = tmp_path / "skills"
    state_path = tmp_path / "state.db"
    config = AgentConfig(skills_dir=skills_dir, state_path=state_path)

    empty = registry.execute(
        ToolCall(name="skill.discover", arguments={}),
        ToolContext(memory=memory, config=config, workspace=tmp_path),
    )

    assert empty.success is True
    assert empty.data["discovered_count"] == 0
    assert empty.data["skills_dir"] == str(skills_dir)
    assert "No skill capsules" in empty.data["message"]

    invalid_dir = skills_dir / "invalid"
    invalid_dir.mkdir(parents=True)
    (invalid_dir / "skill.json").write_text(
        json.dumps({"id": "invalid", "risk": "spicy"}),
        encoding="utf-8",
    )
    (invalid_dir / "SKILL.md").write_text("No description.", encoding="utf-8")

    invalid = registry.execute(
        ToolCall(name="skill.discover", arguments={}),
        ToolContext(memory=memory, config=config, workspace=tmp_path),
    )

    assert invalid.success is True
    assert invalid.data["discovered_count"] == 0
    assert invalid.data["validation_errors"][0]["errors"] == ["missing_description", "invalid_risk"]
    assert "Validation rejected" in invalid.data["message"]


def test_skill_inspect_tool_returns_persisted_validation_and_provenance(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    config = AgentConfig(skills_dir=tmp_path / "skills", state_path=tmp_path / "state.db")
    skill_dir = config.skills_dir / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": "review",
                "name": "Review",
                "description": "Review code with memory.",
                "risk": "low",
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("Review with Kestrel memory.", encoding="utf-8")
    registry.execute(
        ToolCall(name="skill.discover", arguments={}),
        ToolContext(memory=memory, config=config, workspace=tmp_path),
    )

    result = registry.execute(
        ToolCall(name="skill.inspect", arguments={"skill_id": "review"}),
        ToolContext(memory=memory, config=config, workspace=tmp_path),
    )

    assert result.success is True
    assert result.data["skill"]["id"] == "review"
    assert result.data["skill"]["manifest"]["validation"]["ok"] is True
    assert len(result.data["skill"]["manifest"]["provenance"]["manifest_sha256"]) == 64


def test_plugin_and_mcp_registry_tools_redact_and_count_materialized_state(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    state = AgentStateStore(tmp_path / "state.db")
    state.upsert_plugin(
        {
            "id": "demo",
            "name": "Demo Plugin",
            "description": "Demo plugin.",
            "source_url": "https://github.com/acme/demo",
            "commit_sha": "a" * 40,
            "install_path": str(tmp_path / "plugins" / "demo"),
            "manifest": {"id": "demo"},
            "capabilities": ["skill", "mcp"],
            "enabled": True,
            "risk_report": {"risk": "medium"},
            "install_status": "installed",
            "format": "kestrel",
        }
    )
    state.upsert_skill(
        {
            "id": "plugin.demo.review",
            "name": "Plugin Review",
            "description": "Plugin skill.",
            "path": str(tmp_path / "plugins" / "demo" / "skills" / "review"),
            "manifest": {"id": "plugin.demo.review"},
            "enabled": True,
        }
    )
    state.upsert_mcp_server(
        {
            "id": "plugin.demo.static",
            "name": "Plugin Static",
            "transport": "stdio",
            "tools": [{"name": "echo", "description": "Echo"}],
            "enabled": True,
            "secret_env": {"API_TOKEN": "secret://missing"},
            "session_state": "disconnected",
        }
    )
    context = ToolContext(
        memory=memory,
        config=AgentConfig(
            state_path=tmp_path / "state.db", secret_store_path=tmp_path / "secrets.json"
        ),
        workspace=tmp_path,
    )

    plugins = registry.execute(ToolCall(name="plugin.registry", arguments={}), context)
    mcp = registry.execute(ToolCall(name="mcp.registry", arguments={}), context)

    assert plugins.success is True
    assert plugins.data["plugins"][0]["materialized_skill_count"] == 1
    assert plugins.data["plugins"][0]["materialized_mcp_count"] == 1
    assert mcp.success is True
    assert "secret_env" not in mcp.data["servers"][0]
    assert mcp.data["servers"][0]["secret_env_status"]["API_TOKEN"]["configured"] is False


def test_file_find_stat_git_log_show_and_project_scripts_are_bounded(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "kestrel@example.test"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Kestrel Test"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n[tool.pytest.ini_options]\naddopts = "-q"\n',
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "agent.py").write_text("print('kestrel')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True, text=True
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    context = ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path)

    found = registry.execute(ToolCall(name="file.find", arguments={"pattern": "*.py"}), context)
    stat = registry.execute(ToolCall(name="file.stat", arguments={"path": "src/agent.py"}), context)
    log = registry.execute(ToolCall(name="git.log", arguments={"max_count": 1}), context)
    shown = registry.execute(
        ToolCall(name="git.show", arguments={"rev": "HEAD", "max_chars": 2000}), context
    )
    scripts = registry.execute(ToolCall(name="project.scripts", arguments={}), context)

    assert found.success is True
    assert found.data["matches"] == [{"path": "src/agent.py", "type": "file"}]
    assert stat.success is True
    assert stat.data["path"] == "src/agent.py"
    assert log.success is True
    assert log.data["commits"][0]["subject"] == "initial"
    assert shown.success is True
    assert "initial" in shown.content
    assert scripts.success is True
    assert "pytest -q" in scripts.data["suggested_commands"]


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


def test_file_read_rejects_secret_store_path(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    secrets_dir = tmp_path / ".nest" / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "local_vault.json").write_text('{"secrets": {"token": {"value": "raw"}}}')
    result = registry.execute(
        ToolCall(name="file.read", arguments={"path": ".nest/secrets/local_vault.json"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "file_read_failed"
    assert "not allowed" in result.content.lower()


@pytest.mark.skipif(
    sys.platform == "win32", reason="hard-link identity semantics differ on Windows"
)
def test_workspace_tools_hide_custom_secret_store_and_inode_aliases(tmp_path: Path) -> None:
    raw_secret = "opaque-custom-vault-sentinel-7e18b9"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    vault_path = config_dir / "runtime-vault.json"
    vault_path.write_text(
        json.dumps(
            {
                "secrets": {
                    "provider": {
                        "id": "provider",
                        "name": "provider",
                        "value": raw_secret,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    vault_path.chmod(0o600)
    # Simulate a restart: the broker sees the pre-existing file but never resolves
    # or registers its raw value with the process-wide redactor.
    SecretBroker(vault_path)
    temporary_path = config_dir / ".runtime-vault.json.interrupted.tmp"
    temporary_path.write_text(raw_secret, encoding="utf-8")
    temporary_path.chmod(0o600)
    alias_path = tmp_path / "innocent-notes.json"
    os.link(vault_path, alias_path)
    (config_dir / "safe.txt").write_text("safe content", encoding="utf-8")

    memory = build_memory_system("memory", tmp_path / "memory")
    config = AgentConfig(
        workspace=tmp_path,
        secret_store_path=vault_path,
        allow_file_write=True,
    )
    read_paths = (
        "config/runtime-vault.json",
        "config/.runtime-vault.json.lock",
        "config/.runtime-vault.json.interrupted.tmp",
        "innocent-notes.json",
    )
    write_calls = (
        ToolCall(
            name="file.write",
            arguments={"path": "config/runtime-vault.json", "content": "replacement"},
            id="write-vault",
        ),
        ToolCall(
            name="file.write",
            arguments={"path": "innocent-notes.json", "content": "replacement"},
            id="write-alias",
        ),
    )
    context = ToolContext(
        memory=memory,
        config=config,
        workspace=tmp_path,
        approved_tool_call_ids=frozenset(call.id for call in write_calls),
        approved_tool_call_arguments={call.id: dict(call.arguments) for call in write_calls},
    )
    registry = build_default_tools()

    direct_results = [
        registry.execute(ToolCall(name="file.read", arguments={"path": path}), context)
        for path in read_paths
    ]
    stat_results = [
        registry.execute(
            ToolCall(name="file.stat", arguments={"path": path, "hash": True}),
            context,
        )
        for path in ("config/runtime-vault.json", "innocent-notes.json")
    ]
    write_results = [registry.execute(call, context) for call in write_calls]
    search = registry.execute(
        ToolCall(name="repo.search", arguments={"query": raw_secret}),
        context,
    )
    listed = registry.execute(
        ToolCall(name="file.list", arguments={"path": "config"}),
        context,
    )
    found = registry.execute(
        ToolCall(name="file.find", arguments={"path": ".", "pattern": "*", "type": "file"}),
        context,
    )
    mapped = registry.execute(
        ToolCall(name="repo.map", arguments={"path": ".", "max_depth": 3}),
        context,
    )

    assert all(not result.success for result in (*direct_results, *stat_results, *write_results))
    assert all(
        raw_secret not in result.content
        for result in (*direct_results, *stat_results, *write_results)
    )
    assert search.success is True
    assert search.data["matches"] == []
    visible_payload = json.dumps(
        {
            "list": listed.data,
            "find": found.data,
            "map": mapped.data,
            "search": search.data,
        }
    )
    assert "safe.txt" in visible_payload
    assert "runtime-vault" not in visible_payload
    assert "innocent-notes.json" not in visible_payload
    assert raw_secret not in visible_payload
    assert vault_path.read_text(encoding="utf-8").find(raw_secret) >= 0
    assert alias_path.read_text(encoding="utf-8").find(raw_secret) >= 0


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env.production",
        ".npmrc",
        ".pypirc",
        ".git/config",
        "nested/secrets/provider.json",
        "nested/credentials/cloud.json",
        "keys/release.pem",
        "client_secret-production.json",
    ],
)
def test_file_read_rejects_sensitive_credential_paths(
    tmp_path: Path,
    relative_path: str,
) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("raw-sensitive-value", encoding="utf-8")
    memory = build_memory_system("memory", tmp_path / "memory")

    result = build_default_tools().execute(
        ToolCall(name="file.read", arguments={"path": relative_path}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success is False
    assert result.error == "file_read_failed"
    assert "raw-sensitive-value" not in result.content
    assert "not allowed" in result.content.lower()


def test_file_read_rejects_symlink_and_traversal_aliases_for_sensitive_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.local").write_text("OPENAI_API_KEY=raw-secret", encoding="utf-8")
    (tmp_path / "README.md").write_text("safe", encoding="utf-8")
    (tmp_path / "visible.txt").symlink_to(tmp_path / ".env.local")
    (tmp_path / ".env.alias").symlink_to(tmp_path / "README.md")
    (tmp_path / "nested").mkdir()
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    context = ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path)

    for requested in ("visible.txt", ".env.alias", "nested/../.env.local"):
        result = registry.execute(
            ToolCall(name="file.read", arguments={"path": requested}),
            context,
        )
        assert result.success is False, requested
        assert result.error == "file_read_failed", requested
        assert "raw-secret" not in result.content


def test_file_read_redacts_secret_content_in_an_allowed_file(tmp_path: Path) -> None:
    secret = "opaque-provider-secret-12345"
    (tmp_path / "debug.txt").write_text(
        f"status=failed\nOPENAI_API_KEY={secret}\n",
        encoding="utf-8",
    )
    memory = build_memory_system("memory", tmp_path / "memory")

    result = build_default_tools().execute(
        ToolCall(name="file.read", arguments={"path": "debug.txt"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success is True
    assert "status=failed" in result.content
    assert secret not in result.content
    assert "<redacted>" in result.content


def test_repo_search_skips_sensitive_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("needle = 'safe'\n", encoding="utf-8")
    (tmp_path / "credentials.json").write_text(
        '{"token": "needle-sensitive-value"}',
        encoding="utf-8",
    )
    memory = build_memory_system("memory", tmp_path / "memory")

    result = build_default_tools().execute(
        ToolCall(name="repo.search", arguments={"query": "needle"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success is True
    assert "app.py" in result.content
    assert "credentials.json" not in result.content
    assert "needle-sensitive-value" not in result.content


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


@pytest.mark.parametrize(
    "executable",
    ("./echo", "/tmp/echo", "nested/ls", "..\\cat"),
)
def test_shell_run_rejects_caller_selected_allowlist_executables(
    tmp_path: Path,
    executable: str,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        name="shell.run",
        arguments={"command": [executable, "must-not-run"]},
        id="caller-selected-utility",
    )

    result = build_default_tools().execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )

    assert result.success is False
    assert result.error == "command_not_allowlisted"


def test_shell_run_uses_bounded_utility_instead_of_path_executable(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    marker = tmp_path / "path-executable-ran"
    fake_echo = tmp_path / "echo"
    fake_echo.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n",
        encoding="utf-8",
    )
    fake_echo.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.defpath}")

    def forbidden_launch(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("bounded shell utilities must not launch a host process")

    monkeypatch.setattr(
        "nested_memvid_agent.tools.process_tools._start_subprocess",
        forbidden_launch,
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        name="shell.run",
        arguments={"command": ["echo", "safe"]},
        id="bounded-echo",
    )

    result = build_default_tools().execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )

    assert result.success is True
    assert result.data["execution_mode"] == "bounded_utility"
    assert "safe" in result.content
    assert marker.exists() is False


@pytest.mark.parametrize(
    "command",
    (
        ["cat", "../outside.txt"],
        ["cat", ".env"],
        ["cat", "-"],
        ["cat"],
        ["ls", "/tmp"],
    ),
)
def test_shell_run_file_commands_cannot_escape_or_read_sensitive_paths(
    tmp_path: Path,
    command: list[str],
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(name="shell.run", arguments={"command": command}, id="bounded_shell_path")

    result = build_default_tools().execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )

    assert result.success is False
    assert result.error == "path_not_allowed"


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


def test_self_remember_rejects_caller_claimed_confirmation(tmp_path: Path) -> None:
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

    assert result.success is False
    assert result.error == "self_memory_rejected"
    assert not memory.retrieve(
        RetrievalQuery(
            query="implementation over analysis", layers=(MemoryLayer.SELF,), k_per_layer=3
        )
    )


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


def test_plugin_review_requires_enablement_and_exact_approval(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    plugin_repo = tmp_path / "plugin-repo"
    plugin_repo.mkdir()
    (plugin_repo / "kestrel.plugin.json").write_text(
        json.dumps(
            {
                "id": "reviewplug",
                "name": "Review Plugin",
                "description": "Reviewed through the high-risk plugin tool.",
                "dependencies": {"python": ["requests>=2"]},
                "skills": [
                    {"id": "hello", "description": "Say hello.", "instructions": "Return hello."}
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
    arguments = {"source": "owner/repo", "ref": "main"}
    call = ToolCall(name="plugin.review", arguments=arguments, id="plugin_review_exact")

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
            approved_tool_call_ids=frozenset({"plugin_review_exact"}),
            approved_tool_call_arguments={"plugin_review_exact": arguments},
        ),
    )

    assert approved.success is True
    assert approved.data["manifest"]["id"] == "reviewplug"
    assert approved.data["dependency_review"]["declared"]["python"] == ["requests>=2"]
    with pytest.raises(KeyError):
        AgentStateStore(tmp_path / "state.db").get_plugin("reviewplug")


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
    assert approved.call == call
    assert all(not key.startswith("_") for key in approved.call.arguments)

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
    memory = build_memory_system(
        "memory", tmp_path / "memory", enforce_stable_write_integrity=False
    )
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
    subprocess.run(
        ["git", "config", "user.name", "Kestrel Test"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "kestrel-test@example.invalid"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("seed\n")
    # Git-focused tests keep the memory backend inside the synthetic workspace.
    # Model the runtime-artifact ignores expected in an actual Kestrel workspace
    # so durable in-memory snapshots do not masquerade as user source changes.
    (path / ".gitignore").write_text(".nest/\nmemory/\n")
    subprocess.run(
        ["git", "add", "README.md", ".gitignore"],
        cwd=path,
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


def _successful_repair_validation_id(
    registry: ToolRegistry,
    memory: object,
    workspace: Path,
    *,
    call_id: str,
) -> str:
    call = ToolCall(
        name="repair.validate",
        arguments={"command": ["python", "-c", "print('repair validation passed')"]},
        id=call_id,
    )
    result = registry.execute(
        call,
        _approved_context(memory, workspace, call, allow_shell=True),
    )
    assert result.success
    return str(result.data["validation_id"])


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

    rollback_call = ToolCall(
        name="repair.rollback",
        arguments={
            "validation_id": validated.data["validation_id"],
            "expected_current_diff_digest": repair_snapshot(tmp_path)["diff_digest"],
        },
        id="repair_rollback",
    )
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
    memory = build_memory_system(
        "memory", tmp_path / "memory", enforce_stable_write_integrity=False
    )
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
    memory = build_memory_system(
        "memory", tmp_path / "memory", enforce_stable_write_integrity=False
    )
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


def test_codex_workspace_write_is_never_run_as_uncontained_host_process(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        name="codex.exec",
        arguments={"prompt": "edit a file", "sandbox": "workspace-write"},
        id="codex-workspace-write",
    )

    result = build_default_tools(("codex.exec",)).execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_codex_cli=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )

    assert result.success is False
    assert result.error == "codex_workspace_write_uncontained"


def test_codex_exec_runs_when_enabled(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="codex done", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.command_tools._run_subprocess", fake_run)

    result = registry.execute(
        ToolCall(
            name="codex.exec",
            arguments={
                "prompt": "summarize this repo",
                "model": "gpt-test",
                "sandbox": "read-only",
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
                    "sandbox": "read-only",
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
    assert ["--cd", "/extension"] == command[2:4]
    assert ["--sandbox", "read-only"] == command[4:6]
    assert "--ephemeral" in command
    assert command[-1] == "summarize this repo"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["sanitize_environment"] is True
    assert kwargs["require_container_isolation"] is True
    assert kwargs["default_timeout"] == 45


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_codex_exec_timeout_kills_term_ignoring_descendants(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "codex-child.pid"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "(trap '' TERM; sleep 30) &\n"
        f"echo $! > {child_pid_path}\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    def host_supervised_run(command: list[str], **kwargs: Any) -> Any:
        kwargs["require_container_isolation"] = False
        return _run_subprocess(command, **kwargs)

    monkeypatch.setattr(
        "nested_memvid_agent.tools.command_tools._run_subprocess",
        host_supervised_run,
    )
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / "memory"),
        config=AgentConfig(allow_codex_cli=True, tool_timeout_seconds=1.0),
        workspace=tmp_path,
        run_id="run-codex-timeout",
    )

    result = CodexExecTool().run(
        {
            "prompt": "bounded timeout",
            "timeout": 30,
            "_tool_call_id": "codex-timeout-tree",
        },
        context,
    )

    assert result.success is False
    assert result.error == "codex_timeout"
    child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
    _assert_pid_gone_or_zombie(child_pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_codex_exec_cancel_kills_term_ignoring_descendants(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "codex-cancel-child.pid"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "(trap '' TERM; sleep 30) &\n"
        f"echo $! > {child_pid_path}\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    def host_supervised_run(command: list[str], **kwargs: Any) -> Any:
        kwargs["require_container_isolation"] = False
        return _run_subprocess(command, **kwargs)

    monkeypatch.setattr(
        "nested_memvid_agent.tools.command_tools._run_subprocess",
        host_supervised_run,
    )
    tool = CodexExecTool()
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / "memory"),
        config=AgentConfig(allow_codex_cli=True, tool_timeout_seconds=30),
        workspace=tmp_path,
        run_id="run-codex-cancel",
    )
    arguments = {
        "prompt": "bounded cancellation",
        "timeout": 30,
        "_tool_call_id": "codex-cancel-tree",
    }

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tool.run, arguments, context)
        for _ in range(100):
            if child_pid_path.is_file():
                break
            sleep(0.01)
        assert child_pid_path.is_file()
        tool.cancel("codex-cancel-tree")
        result = future.result(timeout=3.0)

    assert result.success is False
    child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
    _assert_pid_gone_or_zombie(child_pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_patch_apply_timeout_cannot_mutate_after_terminal_result(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "patch-child.pid"
    late_marker = tmp_path / "late-patch-mutation.txt"
    executable = tmp_path / "git"
    child_code = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(1.0);"
        f"pathlib.Path({str(late_marker)!r}).write_text('late')"
    )
    executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"child_code = {child_code!r}\n"
        "child = subprocess.Popen([sys.executable, '-c', child_code])\n"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        name="patch.apply",
        arguments={
            "patch": (
                "diff --git a/safe.txt b/safe.txt\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/safe.txt\n"
                "@@ -0,0 +1 @@\n"
                "+safe\n"
            )
        },
        id="patch-timeout-tree",
    )

    result = build_default_tools().execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_file_write=True, tool_timeout_seconds=0.4),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )

    assert result.success is False
    assert result.error == "tool_timeout"
    assert child_pid_path.is_file()
    child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
    _assert_pid_gone_or_zombie(child_pid)
    sleep(1.2)
    assert late_marker.exists() is False
    assert (tmp_path / "safe.txt").exists() is False


def _assert_pid_gone_or_zombie(pid: int) -> None:
    for _ in range(40):
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            check=False,
            text=True,
            capture_output=True,
        )
        process_state = status.stdout.strip()
        if status.returncode != 0 or not process_state or process_state.startswith("Z"):
            return
        sleep(0.05)
    pytest.fail(f"Codex descendant survived in state {process_state}")


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
    _init_git_repo(tmp_path)
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


@pytest.mark.parametrize(
    ("tool_name", "command", "config"),
    (
        (
            "test.run",
            [sys.executable, "-c", "print('no host fallback')"],
            AgentConfig(allow_shell=True),
        ),
        (
            "lint.run",
            [sys.executable, "-m", "compileall", "-q", "."],
            AgentConfig(allow_shell=True),
        ),
        ("codex.exec", None, AgentConfig(allow_codex_cli=True)),
    ),
)
def test_arbitrary_code_tools_require_digest_pinned_container_by_default(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    tool_name: str,
    command: list[str] | None,
    config: AgentConfig,
) -> None:
    monkeypatch.setattr(process_tools, "run_isolated_validation", run_real_isolated_validation)
    arguments = {"prompt": "inspect only"} if command is None else {"command": command}
    call = ToolCall(name=tool_name, arguments=arguments, id=f"contained-{tool_name}")

    result = build_default_tools((tool_name,)).execute(
        call,
        ToolContext(
            memory=build_memory_system("memory", tmp_path / f"memory-{tool_name}"),
            config=config,
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )

    assert result.success is False
    assert result.error == "validation_container_required"


@pytest.mark.parametrize("tool_name", ["test.run", "lint.run"])
def test_validation_subprocesses_do_not_inherit_credentials(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    tool_name: str,
) -> None:
    _init_git_repo(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "opaque-provider-secret-12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:telegram-secret-material-value")
    monkeypatch.setenv("NEST_AGENT_API_TOKEN", "opaque-server-secret-12345")
    monkeypatch.setenv("SAFE_VALIDATION_FLAG", "present")
    script = tmp_path / "inspect_environment.py"
    script.write_text(
        "import json, os\n"
        "names = ('OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', "
        "'NEST_AGENT_API_TOKEN', 'SAFE_VALIDATION_FLAG')\n"
        "print(json.dumps({name: os.environ.get(name) for name in names}))\n",
        encoding="utf-8",
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        name=tool_name,
        arguments={"command": [sys.executable, str(script)]},
        id=f"sanitize-{tool_name}",
    )

    result = build_default_tools().execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_shell=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )

    assert result.success is True
    assert '"SAFE_VALIDATION_FLAG": null' in result.content
    assert '"OPENAI_API_KEY": null' in result.content
    assert '"TELEGRAM_BOT_TOKEN": null' in result.content
    assert '"NEST_AGENT_API_TOKEN": null' in result.content
    assert "opaque-provider-secret" not in result.content


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

    review_call = ToolCall(
        name="repair.review",
        arguments={
            "validation_id": validation.data["validation"]["validation_id"],
            "summary": "Calculator repair validated by targeted pytest.",
        },
        id="review_e2e",
    )
    review = registry.execute(review_call, _approved_context(memory, tmp_path, review_call))
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
    validation_id = _successful_repair_validation_id(
        registry,
        memory,
        tmp_path,
        call_id="validate_stale",
    )
    review_call = ToolCall(
        name="repair.review",
        arguments={"validation_id": validation_id, "summary": "Calculator fix reviewed."},
        id="review_stale",
    )
    review = registry.execute(review_call, _approved_context(memory, tmp_path, review_call))
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
        arguments={
            "reason": "stale_repair_review",
            "review_id": review.data["review_id"],
            "expected_current_diff_digest": repair_snapshot(tmp_path)["diff_digest"],
        },
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
    validation_id = _successful_repair_validation_id(
        registry,
        memory,
        tmp_path,
        call_id="validate_review_gate",
    )
    call = ToolCall(
        name="repair.review",
        arguments={
            "validation_id": validation_id,
            "summary": "README patch validated with tests.",
        },
        id="review_gate",
    )

    result = registry.execute(call, _approved_context(memory, tmp_path, call))

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
    validation_id = _successful_repair_validation_id(
        registry,
        memory,
        tmp_path,
        call_id="validate_for_commit",
    )
    review_call = ToolCall(
        name="repair.review",
        arguments={
            "validation_id": validation_id,
            "summary": "validated",
        },
        id="review_for_commit",
    )
    review = registry.execute(review_call, _approved_context(memory, tmp_path, review_call))
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
    _init_git_repo(tmp_path)
    subprocess.run(
        ["git", "switch", "-c", "topic/exact-commit"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "README.md").write_text("exact staged candidate\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    expected_tree = subprocess.run(
        ["git", "write-tree"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    status = registry.execute(
        ToolCall(name="git.status", arguments={}, id="commit_preview_status"),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert status.success
    preview = cast(dict[str, str], status.data["commit_preview"])
    assert preview == {
        "expected_branch": "topic/exact-commit",
        "expected_head_sha": status.data["head_sha"],
        "expected_tree_sha": expected_tree,
    }
    commit_spec = registry.spec_for("git.commit")
    assert commit_spec is not None
    assert {"expected_branch", "expected_head_sha", "expected_tree_sha"} <= set(
        commit_spec.parameters["properties"]
    )
    assert {
        "required": ["expected_branch", "expected_head_sha", "expected_tree_sha"]
    } in commit_spec.parameters["anyOf"]
    call = ToolCall(
        name="git.commit",
        arguments={"message": "test commit", **preview},
        id="commit1",
    )

    blocked = registry.execute(
        call,
        ToolContext(memory=memory, config=AgentConfig(allow_git_commit=True), workspace=tmp_path),
    )
    assert blocked.error == "approval_required"

    captured: dict[str, object] = {"commands": []}

    real_run = subprocess.run

    def recording_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        captured["commands"].append(command)  # type: ignore[union-attr]
        return real_run(command, **kwargs)

    monkeypatch.setattr("nested_memvid_agent.tools.git_tools.subprocess.run", recording_run)
    approved = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_git_commit=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"commit1"}),
            approved_tool_call_arguments={"commit1": call.arguments},
        ),
    )

    assert approved.success
    commands = captured["commands"]
    assert isinstance(commands, list)
    assert any("commit-tree" in command for command in commands)
    assert any("update-ref" in command for command in commands)
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

    def fake_output(workspace: Path, command: list[str]) -> str:
        del workspace
        calls.append(command)
        return "feature"

    def fake_supervised(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Switched to a new branch", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.git_tools._git_output", fake_output)
    monkeypatch.setattr("nested_memvid_agent.tools.git_tools._run_subprocess", fake_supervised)
    result = registry.execute(call, _approved_context(memory, tmp_path, call))

    assert result.success
    assert any(command[-3:] == ["switch", "-c", "kestrel/self-improve/test"] for command in calls)
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

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[Any]:
        del kwargs
        if "config" in command and "--get-regexp" in command:
            return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"")
        if command[-7:] == [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--ignore-cr-at-eol",
            "--numstat",
            "-z",
        ]:
            return subprocess.CompletedProcess(command, 0, stdout=b"1\t0\ta.txt\0", stderr=b"")
        raise AssertionError(f"unexpected direct Git command: {command}")

    def fake_supervised(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert command[-4:] == [
            "diff",
            "--ignore-cr-at-eol",
            "--no-ext-diff",
            "--no-textconv",
        ]
        return subprocess.CompletedProcess(command, 0, stdout=patch_text, stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.git_tools.subprocess.run", fake_run)
    monkeypatch.setattr("nested_memvid_agent.tools.git_tools._run_subprocess", fake_supervised)
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
        if command[-2:] == ["branch", "--show-current"]:
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
    assert not any(command[-3:] == ["commit", "-m", "test commit"] for command in calls)


def test_git_commit_requires_branch_and_head_preview_for_nonrepair_call(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    call = ToolCall(
        name="git.commit",
        arguments={"message": "incomplete preview", "expected_tree_sha": "a" * 40},
        id="commit_incomplete_preview",
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="topic/incomplete\n", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.git_tools.subprocess.run", fake_run)

    result = registry.execute(call, _approved_context(memory, tmp_path, call))

    assert result.error == "commit_preview_required"
    assert "expected_branch, expected_head_sha" in result.content
    operational_calls = [command for command in calls if "--get-regexp" not in command]
    assert len(operational_calls) == 1
    assert operational_calls[0][-2:] == ["branch", "--show-current"]


def test_memory_inspect_export_and_import_are_structured_and_gated(tmp_path: Path) -> None:
    memory = build_memory_system(
        "memory", tmp_path / "memory", enforce_stable_write_integrity=False
    )
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
    export_payload = json.loads(exported.content)
    assert export_payload["records"][0]["title"] == "Structured export fact"
    assert export_payload["total"] == 1
    assert export_payload["truncated"] is False
    assert export_payload["complete_export"] is True
    assert export_payload["records"][0]["created_at"]

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
    assert not memory.retrieve(
        RetrievalQuery(query="Approved import", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3)
    )
    staged = memory.retrieve(
        RetrievalQuery(query="Approved import", layers=(MemoryLayer.EPISODIC,), k_per_layer=3)
    )
    assert staged
    assert staged[0].record.metadata["import_requested_layer"] == "semantic"
    assert staged[0].record.metadata["stable_recall_eligible"] is False


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


def test_memory_consolidate_rejects_legacy_score_and_repeat_claims(tmp_path: Path) -> None:
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
    assert result.data["promoted"] is False
    assert not memory.retrieve(
        RetrievalQuery(
            query="Repeatable test recipe", layers=(MemoryLayer.PROCEDURAL,), k_per_layer=3
        )
    )


def test_memory_learn_rejects_legacy_validation_score(tmp_path: Path) -> None:
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
    assert result.data["accepted"] is False
    assert result.data["target_layer"] is None
    assert result.data["record_id"] is None
    assert not memory.retrieve(
        RetrievalQuery(
            query=".mv2 file per memory layer", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3
        )
    )


def test_memory_learn_scores_but_rejects_unresolved_structured_evidence(tmp_path: Path) -> None:
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
    assert result.data["accepted"] is False
    assert result.data["validation_evidence"]["resolved"] is False
    assert not memory.retrieve(
        RetrievalQuery(query="Structured validation evidence", layers=(MemoryLayer.SEMANTIC,))
    )


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


def test_policy_promote_requires_durable_receipt_and_staged_source(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    arguments = {
        "title": "Approval-bound policy",
        "content": "Policy promotion must retain authenticated approval provenance.",
        "validation_evidence": {
            "test_refs": [{"source": "test.run", "locator": "policy-tests"}],
            "lint_refs": [{"source": "lint.run", "locator": "policy-lint"}],
            "repair_refs": [{"source": "repair.validate", "locator": "policy-repair"}],
            "review_refs": [{"source": "repair.review", "locator": "policy-review"}],
            "task_refs": [{"source": "task", "locator": "only-one-event"}],
            "human_explicit": True,
        },
    }
    call = ToolCall(name="memory.policy_promote", arguments=arguments, id="policy-receipt")
    base_context = {
        "memory": memory,
        "config": AgentConfig(allow_policy_writes=True),
        "workspace": tmp_path,
        "session_id": "policy-test",
        "run_id": "run-policy-test",
        "approved_tool_call_ids": frozenset({call.id}),
        "approved_tool_call_arguments": {call.id: arguments},
    }

    no_receipt = registry.execute(call, ToolContext(**base_context))

    assert not no_receipt.success
    assert no_receipt.error == "approval_provenance_required"
    receipt = {
        "approval_id": "approval-policy-test",
        "run_id": "run-policy-test",
        "tool_call_id": call.id,
        "tool_name": "memory.policy_promote",
        "arguments": arguments,
        "status": "approved",
        "principal": "owner",
        "decision": {
            "approved": True,
            "arguments": arguments,
            "principal": "owner",
        },
    }
    single_event = registry.execute(
        call,
        ToolContext(**base_context, approval_receipts={call.id: receipt}),
    )

    assert not single_event.success
    assert single_event.error == "policy_source_record_required"
    assert not memory.retrieve(
        RetrievalQuery(query="authenticated approval provenance", layers=(MemoryLayer.POLICY,))
    )


def test_memory_correct_writes_correction_and_hides_superseded_target(tmp_path: Path) -> None:
    memory = build_memory_system(
        "memory", tmp_path / "memory", enforce_stable_write_integrity=False
    )
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

    call = ToolCall(
        name="memory.correct",
        id="correct_alpha",
        arguments={
            "target_record_id": target_id,
            "correction_text": "Feature alpha is not enabled.",
            "evidence": [
                {"source": "user", "locator": "turn-1", "quote": "actually, alpha is off"}
            ],
        },
    )
    registry = build_default_tools()
    disabled = registry.execute(
        call,
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert not disabled.success
    assert disabled.error == "tool_disabled"

    blocked = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_memory_import=True),
            workspace=tmp_path,
        ),
    )
    assert not blocked.success
    assert blocked.error == "approval_required"

    result = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_memory_import=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"correct_alpha"}),
            approved_tool_call_arguments={"correct_alpha": call.arguments},
        ),
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
