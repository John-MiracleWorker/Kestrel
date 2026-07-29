from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools


def test_github_pr_publication_requires_exact_call_approval(tmp_path: Path) -> None:
    registry = build_default_tools(("github.pr.create",))
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        id="github_call",
        name="github.pr.create",
        arguments={
            "request_id": "github_request",
            "expected_request_digest": "a" * 64,
        },
    )

    result = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(
                allow_git_push=True,
                allow_remote_mutation=True,
            ),
            workspace=tmp_path,
            run_id="run_github",
        ),
    )

    assert result.success is False
    assert result.error == "approval_required"


def test_github_sync_is_disabled_until_integration_is_explicitly_enabled(
    tmp_path: Path,
) -> None:
    registry = build_default_tools(("github.pr.sync",))
    memory = build_memory_system("memory", tmp_path / "memory")

    result = registry.execute(
        ToolCall(
            name="github.pr.sync",
            arguments={
                "request_id": "github_request",
                "expected_request_digest": "a" * 64,
            },
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(),
            workspace=tmp_path,
            run_id="run_github",
        ),
    )

    assert result.success is False
    assert result.error == "tool_disabled"
