from __future__ import annotations

import subprocess
from pathlib import Path

from nested_memvid_agent.agent import (
    AgentDependencies,
    NestedMV2Agent,
    _validated_registry_discoveries,
)
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.base import LLMProvider, ProviderCapabilities
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import (
    ChatMessage,
    LLMOptions,
    LLMResponse,
    ToolCall,
    ToolExecution,
    ToolSpec,
)
from nested_memvid_agent.tools.builtin import build_default_tools


class _CapturingProvider(LLMProvider):
    def __init__(
        self,
        responses: list[LLMResponse],
        *,
        native: bool,
        native_tool_limit: int | None = None,
    ) -> None:
        self.responses = list(responses)
        self.native = native
        self.native_tool_limit = native_tool_limit
        self.requests: list[tuple[list[ChatMessage], list[ToolSpec]]] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="capturing-native" if self.native else "capturing-control",
            supports_native_tools=self.native,
            native_tool_limit=self.native_tool_limit,
        )

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del options
        self.requests.append((list(messages), list(tools)))
        return self.responses.pop(0)


def _agent(tmp_path: Path, provider: LLMProvider) -> NestedMV2Agent:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return NestedMV2Agent(
        AgentDependencies(
            memory=build_memory_system("memory", tmp_path / "memory"),
            llm=provider,
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                workspace=workspace,
                stream=False,
                max_retries=0,
            ),
        )
    )


def test_native_runtime_bounds_tools_without_duplicate_prompt_schemas_and_executes(
    tmp_path: Path,
) -> None:
    provider = _CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        name="diagnosis.classify",
                        arguments={
                            "failure_text": "TimeoutError from provider",
                            "source": "provider",
                        },
                        id="native-diagnosis-1",
                    ),
                ),
                finish_reason="tool_calls",
            ),
            LLMResponse(content="The failure is a provider timeout."),
        ],
        native=True,
        native_tool_limit=2,
    )

    result = _agent(tmp_path, provider).chat(
        "Use diagnosis.classify to classify this provider TimeoutError.",
        session_id="native-tool",
    )

    assert result.stop_reason == "complete"
    assert result.assistant_message == "The failure is a provider timeout."
    assert len(result.tool_executions) == 1
    assert result.tool_executions[0].success is True
    assert result.tool_executions[0].call.name == "diagnosis.classify"
    assert len(provider.requests) == 2
    second_messages = provider.requests[1][0]
    assistant_call_message = next(
        message for message in second_messages if message.role == "assistant" and message.tool_calls
    )
    tool_result_message = next(message for message in second_messages if message.role == "tool")
    assert assistant_call_message.tool_calls[0].id == "native-diagnosis-1"
    assert tool_result_message.tool_call_id == "native-diagnosis-1"
    assert tool_result_message.name == "diagnosis.classify"
    for messages, tools in provider.requests:
        assert [spec.name for spec in tools] == ["tool.registry", "diagnosis.classify"]
        system_text = "\n".join(message.content for message in messages if message.role == "system")
        assert "provider-native function-calling interface" in system_text
        assert "Parameters JSON schema" not in system_text
        assert "Available tools:" not in system_text


def test_native_runtime_carries_validated_registry_discovery_into_next_round(
    tmp_path: Path,
) -> None:
    provider = _CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        name="tool.registry",
                        arguments={"query": "git.status", "enabled": True},
                        id="discover-git-status",
                    ),
                ),
            ),
            LLMResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        name="git.status",
                        arguments={},
                        id="run-discovered-git-status",
                    ),
                ),
            ),
            LLMResponse(content="The repository status was inspected."),
        ],
        native=True,
        native_tool_limit=2,
    )

    result = _agent(tmp_path, provider).chat(
        "Discover the exact read-only repository inspection capability, use it, then summarize.",
        session_id="native-tool-discovery",
    )

    first_catalog = [spec.name for spec in provider.requests[0][1]]
    second_catalog = [spec.name for spec in provider.requests[1][1]]
    third_catalog = [spec.name for spec in provider.requests[2][1]]
    assert len(first_catalog) == 2
    assert "tool.registry" in first_catalog
    assert "git.status" not in first_catalog
    assert second_catalog == ["tool.registry", "git.status"]
    assert third_catalog == ["tool.registry", "git.status"]
    assert [execution.call.name for execution in result.tool_executions] == [
        "tool.registry",
        "git.status",
    ]
    assert all(execution.success for execution in result.tool_executions)
    assert result.assistant_message == "The repository status was inspected."


def test_registry_discovery_rejects_alias_unknown_and_disabled_rows() -> None:
    registry = build_default_tools()
    git_status = registry.spec_for("git.status")
    git_commit = registry.spec_for("git.commit")
    assert git_status is not None
    assert git_commit is not None
    enabled_row = {**git_status.to_public_dict(), "enabled": True, "enablement_flag": None}
    disabled_row = {
        **git_commit.to_public_dict(),
        "enabled": False,
        "enablement_flag": "allow_git_commit",
    }
    alias_row = {**enabled_row, "name": "status"}
    unknown_row = {**enabled_row, "name": "forged.tool"}
    call = ToolCall(name="tool.registry", arguments={"query": "git"})
    execution = ToolExecution(
        call=ToolCall(name="tool.registry", arguments={"query": "git"}),
        success=True,
        content="registry result",
        data={
            "count": 4,
            "tools": [enabled_row, disabled_row, alias_row, unknown_row],
        },
    )

    assert _validated_registry_discoveries(
        call=call,
        execution=execution,
        registry=registry,
    ) == ("git.status",)


def test_registry_discovery_rechecks_live_capability_gate() -> None:
    registry = build_default_tools()
    git_status = registry.spec_for("git.status")
    assert git_status is not None
    row = {**git_status.to_public_dict(), "enabled": True, "enablement_flag": None}
    call = ToolCall(name="tool.registry", arguments={"query": "git.status"})
    execution = ToolExecution(
        call=ToolCall(name="tool.registry", arguments={"query": "git.status"}),
        success=True,
        content="registry result",
        data={"count": 1, "tools": [row]},
    )
    registry.set_capability_gate(
        lambda spec: (spec.name != "git.status", "disabled during the turn")
    )

    assert (
        _validated_registry_discoveries(
            call=call,
            execution=execution,
            registry=registry,
        )
        == ()
    )


def test_malformed_native_tool_call_is_rejected_with_precise_nonretryable_taxonomy(
    tmp_path: Path,
) -> None:
    provider = _CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        name="diagnosis.classify",
                        arguments={},
                        id="malformed-diagnosis-1",
                    ),
                ),
            )
        ],
        native=True,
        native_tool_limit=2,
    )

    result = _agent(tmp_path, provider).chat(
        "Use diagnosis.classify on the failure.",
        session_id="malformed-native-tool",
    )

    assert result.stop_reason == "provider_error"
    assert result.tool_executions == ()
    assert result.error == {
        "message": "diagnosis.classify missing required arguments: ['failure_text']",
        "code": "missing_tool_arguments",
        "retryable": False,
        "error_type": "ProviderError",
    }


def test_native_runtime_rejects_call_outside_bounded_catalog_before_execution(
    tmp_path: Path,
) -> None:
    provider = _CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        name="shell.run",
                        arguments={"command": ["pwd"]},
                        id="unadvertised-shell-1",
                    ),
                ),
            )
        ],
        native=True,
        native_tool_limit=2,
    )

    result = _agent(tmp_path, provider).chat(
        "Use diagnosis.classify on the failure.",
        session_id="unadvertised-native-tool",
    )

    assert [spec.name for spec in provider.requests[0][1]] == [
        "tool.registry",
        "diagnosis.classify",
    ]
    assert result.stop_reason == "provider_error"
    assert result.tool_executions == ()
    assert result.error is not None
    assert result.error["code"] == "unknown_tool_call"
    assert result.error["retryable"] is False


def test_native_runtime_suppresses_successful_exact_call_with_new_provider_id(
    tmp_path: Path,
) -> None:
    arguments = {"failure_text": "TimeoutError from provider", "source": "provider"}
    provider = _CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        name="diagnosis.classify",
                        arguments=arguments,
                        id="diagnosis-first",
                    ),
                ),
            ),
            LLMResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        name="diagnosis.classify",
                        arguments=arguments,
                        id="diagnosis-repeated-with-new-id",
                    ),
                ),
            ),
            LLMResponse(content="The duplicate was suppressed safely."),
        ],
        native=True,
        native_tool_limit=2,
    )

    result = _agent(tmp_path, provider).chat(
        "Use diagnosis.classify once for this provider TimeoutError.",
        session_id="duplicate-native-tool",
    )

    assert [execution.success for execution in result.tool_executions] == [True, False]
    assert result.tool_executions[1].error == "duplicate_tool_call"
    assert result.tool_executions[1].data == {
        "suppressed": True,
        "reason": "successful_exact_call",
    }
    assert result.assistant_message == "The duplicate was suppressed safely."


def test_native_runtime_allows_same_tool_with_deliberately_changed_arguments(
    tmp_path: Path,
) -> None:
    timeout_call = ToolCall(
        name="diagnosis.classify",
        arguments={"failure_text": "TimeoutError", "source": "provider"},
    )
    permission_call = ToolCall(
        name="diagnosis.classify",
        arguments={"failure_text": "PermissionError", "source": "tool"},
    )
    assert timeout_call.id != permission_call.id
    provider = _CapturingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=(timeout_call,),
            ),
            LLMResponse(
                content="",
                tool_calls=(permission_call,),
            ),
            LLMResponse(content="Both distinct failures were classified."),
        ],
        native=True,
        native_tool_limit=2,
    )

    result = _agent(tmp_path, provider).chat(
        "Use diagnosis.classify for a timeout and a permission failure.",
        session_id="changed-native-tool",
    )

    assert [execution.success for execution in result.tool_executions] == [True, True]
    assert [execution.call.arguments for execution in result.tool_executions] == [
        {"failure_text": "TimeoutError", "source": "provider"},
        {"failure_text": "PermissionError", "source": "tool"},
    ]


def test_non_native_runtime_retains_control_envelope_and_prompt_schemas(tmp_path: Path) -> None:
    provider = _CapturingProvider([LLMResponse(content="No tool needed.")], native=False)

    result = _agent(tmp_path, provider).chat("hello", session_id="control-tools")

    assert result.stop_reason == "complete"
    messages, tools = provider.requests[0]
    assert len(tools) == len(build_default_tools().specs())
    system_text = "\n".join(message.content for message in messages if message.role == "system")
    assert "respond only with this JSON envelope" in system_text
    assert "Available tools:" in system_text
    assert "Parameters JSON schema" in system_text
