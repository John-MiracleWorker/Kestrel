import pytest

from agent.security.native_policy import (
    NativeExecutionRequest,
    NativePolicyEvaluator,
    NonInteractiveApprovalProvider,
)


def test_classifies_commands():
    evaluator = NativePolicyEvaluator()

    assert evaluator.classify("host_shell", "ls -la") == "read_only"
    assert evaluator.classify("host_shell", "curl https://example.com") == "network"
    assert evaluator.classify("host_shell", "rm -rf /tmp/x") == "destructive"
    assert evaluator.classify("host_python", "print('x')") == "code"


def test_workspace_allowlist_command_classes():
    evaluator = NativePolicyEvaluator()
    evaluator.set_policy(
        "ws-1",
        {"mode": "allowlist", "command_classes": ["read_only"]},
    )

    read_req = NativeExecutionRequest(
        workspace_id="ws-1",
        tool_name="host_shell",
        function_name="execute",
        command="ls",
        command_class="read_only",
    )
    write_req = NativeExecutionRequest(
        workspace_id="ws-1",
        tool_name="host_shell",
        function_name="execute",
        command="echo hi > a.txt",
        command_class="shell",
    )

    assert evaluator.evaluate(read_req).allowed is True
    assert evaluator.evaluate(write_req).allowed is False


@pytest.mark.asyncio
async def test_non_interactive_provider_modes():
    req = NativeExecutionRequest(
        workspace_id="ws",
        tool_name="host_shell",
        function_name="execute",
        command="ls",
        command_class="read_only",
    )

    deny = await NonInteractiveApprovalProvider(mode="deny").approve(req)
    allow = await NonInteractiveApprovalProvider(mode="allow").approve(req)

    assert deny.approved is False
    assert allow.approved is True
