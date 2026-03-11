from agent.execution_context import ExecutionContext
from agent.policy_engine import PolicyEngine
from agent.types import RiskLevel, ToolDefinition


def _execution_context(policy: str = "moderate") -> ExecutionContext:
    return ExecutionContext.create(
        task_id="task-1",
        queue_id="queue-1",
        agent_profile_id="agent-1",
        workspace_id="workspace-1",
        user_id="user-1",
        source="user",
        autonomy_policy=policy,
    )


def test_policy_engine_allows_internal_autonomy_without_approval():
    engine = PolicyEngine()

    decision = engine.decide(
        tool_name="build_automation",
        tool_args={"goal": "send a digest"},
        tool_definition=ToolDefinition(
            name="build_automation",
            description="",
            parameters={},
            risk_level=RiskLevel.MEDIUM,
        ),
        execution_context=_execution_context(),
    )

    assert decision.allowed is True
    assert decision.approval_required is False
    assert decision.scope == "internal"


def test_policy_engine_gates_mutating_workspace_actions():
    engine = PolicyEngine()

    decision = engine.decide(
        tool_name="host_write",
        tool_args={"path": "README.md", "content": "updated"},
        tool_definition=ToolDefinition(
            name="host_write",
            description="",
            parameters={},
            risk_level=RiskLevel.HIGH,
        ),
        execution_context=_execution_context(),
    )

    assert decision.allowed is True
    assert decision.approval_required is True
    assert decision.risk == RiskLevel.HIGH.value


def test_policy_engine_allows_read_only_git_without_approval():
    engine = PolicyEngine()

    decision = engine.decide(
        tool_name="git",
        tool_args={"action": "status"},
        tool_definition=None,
        execution_context=_execution_context(),
    )

    assert decision.allowed is True
    assert decision.approval_required is False


def test_policy_engine_always_gates_mcp_install():
    engine = PolicyEngine()

    decision = engine.decide(
        tool_name="mcp_install",
        tool_args={"server_name": "gmail"},
        tool_definition=None,
        execution_context=_execution_context(policy="full"),
    )

    assert decision.allowed is True
    assert decision.approval_required is True
