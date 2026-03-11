"""Build AgentTask from a chat request with guardrails, profile, and tool registry."""

from __future__ import annotations

from core.config import logger
from core.feature_mode import FeatureMode, parse_feature_mode
from core import runtime

from agent.execution_context import ExecutionContext
from agent.task_profiles import filter_registry_for_profile, infer_task_profile
from agent.tools import build_tool_registry
from agent.types import (
    AgentTask,
    GuardrailConfig as GCfg,
    TaskPlan,
    TaskStep,
    StepStatus,
    RiskLevel as _RL,
    AutonomyLevel as _AL,
)

_SIMPLE_PATTERNS = [
    "hello", "hey", "hi", "yo", "sup", "howdy",
    "good morning", "good afternoon", "good evening",
    "thanks", "thank you", "thx", "ty",
    "ok", "okay", "cool", "nice", "great", "awesome",
    "yes", "no", "yeah", "nah", "yep", "nope",
    "bye", "goodbye", "see you", "later",
    "what's up", "how are you", "what are you",
    "who are you", "what can you do",
]


def is_simple_message(user_content: str) -> bool:
    """Classify message as simple conversational vs complex agent-worthy."""
    user_lower = user_content.lower().strip()
    word_count = len(user_content.split())
    return (
        word_count <= 6
        and any(user_lower.startswith(p) or user_lower == p for p in _SIMPLE_PATTERNS)
    )


def enrich_goal(user_content: str, messages: list[dict]) -> str:
    """Prepend conversation context for complex messages."""
    history_turns = [
        m for m in messages
        if m.get("role") in ("user", "assistant")
        and m.get("content") != user_content
    ]
    recent = history_turns[-6:]
    if recent:
        history_block = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Kestrel'}: {m['content'][:300]}"
            for m in recent
        )
        return (
            f"[Recent conversation context]\n{history_block}\n\n"
            f"[Current message]\n{user_content}"
        )
    return user_content


async def create_chat_task(request, ctx, workspace_id: str) -> AgentTask:
    """Build AgentTask with guardrails, profile, tool registry, execution context.

    Args:
        request: gRPC StreamChatRequest.
        ctx: ChatRequestContext from request_context.build_request_context.
        workspace_id: Workspace identifier.

    Returns:
        Fully configured AgentTask ready for the agent loop.
    """
    pool = ctx.pool
    user_content = ctx.user_content
    messages = ctx.messages
    feature_mode = parse_feature_mode(getattr(runtime, "feature_mode", "core"))

    # Read workspace guardrail settings from DB (user-configured via Settings UI)
    ws_guardrails = {}
    try:
        ws_row = await pool.fetchrow(
            "SELECT settings FROM workspaces WHERE id = $1",
            workspace_id,
        )
        if ws_row and ws_row["settings"]:
            import json as _json
            ws_settings = ws_row["settings"] if isinstance(ws_row["settings"], dict) else _json.loads(ws_row["settings"])
            ws_guardrails = ws_settings.get("guardrails", {})
    except Exception as e:
        logger.warning(f"Failed to read workspace guardrails, using defaults: {e}")

    simple = is_simple_message(user_content)
    planner_goal = user_content if simple else enrich_goal(user_content, messages)

    chat_task = AgentTask(
        user_id=request.user_id,
        workspace_id=workspace_id,
        conversation_id=request.conversation_id,
        goal=planner_goal,
        config=GCfg(
            max_iterations=ws_guardrails.get("maxIterations", 40),
            max_tool_calls=ws_guardrails.get("maxToolCalls", 80),
            max_tokens=ws_guardrails.get("maxTokens", 100_000),
            max_wall_time_seconds=ws_guardrails.get("maxWallTime", 600),
            auto_approve_risk=_RL(
                ws_guardrails.get("autoApproveRisk", "medium")
            ),
            autonomy_level=_AL(
                ws_guardrails.get("autonomyLevel", "balanced")
            ),
        ),
    )

    if simple:
        chat_task.plan = TaskPlan(
            goal=user_content,
            steps=[TaskStep(
                index=0,
                description=f"Respond to the user: {user_content[:100]}",
                status=StepStatus.PENDING,
            )],
        )
    else:
        chat_task.plan = None

    task_profile = infer_task_profile(planner_goal, feature_mode)
    chat_task.task_profile = task_profile.value

    # Build tool registry filtered by mode and profile
    tool_registry = build_tool_registry(
        hands_client=runtime.hands_client,
        vector_store=runtime.vector_store,
        pool=pool,
        runtime_policy=runtime.execution_runtime,
        enabled_bundles=tuple(getattr(runtime, "enabled_tool_bundles", [])),
        feature_mode=feature_mode.value,
    )
    tool_registry = filter_registry_for_profile(tool_registry, task_profile, feature_mode)

    agent_profile = await runtime.workspace_agent_store.ensure_profile(workspace_id)
    chat_task.execution_context = ExecutionContext.create(
        task_id=chat_task.id,
        queue_id=chat_task.id,
        agent_profile_id=agent_profile.id,
        workspace_id=workspace_id,
        user_id=request.user_id,
        session_id=request.conversation_id or chat_task.id,
        source="chat",
        budgets=chat_task.config.to_dict(),
        permissions={"tool_policy_bundle": list(agent_profile.tool_policy_bundle)},
        autonomy_policy=agent_profile.autonomy_policy,
        kernel_preset=agent_profile.kernel_preset,
        services={
            "cron_scheduler": runtime.cron_scheduler,
            "automation_builder": getattr(runtime, "automation_builder", None),
            "daemon_manager": getattr(runtime, "daemon_manager", None),
            "policy_engine": getattr(runtime, "policy_engine", None),
            "ui_manager": getattr(runtime, "ui_artifact_manager", None),
            "ui_artifact_manager": getattr(runtime, "ui_artifact_manager", None),
            "subsystem_bootstrapper": getattr(runtime, "subsystem_bootstrapper", None),
        },
    )

    # Attach computed metadata for the orchestrator
    chat_task._tool_registry = tool_registry
    chat_task._feature_mode = feature_mode
    chat_task._task_profile = task_profile

    return chat_task
