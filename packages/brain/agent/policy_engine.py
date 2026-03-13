from __future__ import annotations

"""
Central autonomy policy evaluation for tool side effects.
"""

from agent.execution_context import ExecutionContext, PolicyDecision
from agent.types import RiskLevel, ToolDefinition


class PolicyEngine:
    """Evaluate whether a tool action is allowed, gated, or blocked."""

    _INTERNAL_AUTONOMOUS_TOOLS = {
        "memory_store",
        "schedule",
        "build_automation",
        "daemon_create",
        "daemon_stop",
        "telegram_notify",
        "model_swap",
    }
    _READONLY_GIT_ACTIONS = {"status", "diff", "log", "branch", "show"}

    _CAPABILITY_BYPASS_TOOLS = {
        "task_complete",
        "ask_human",
        "mcp_status",
        "mcp_connect",
        "mcp_disconnect",
        "memory_query",
        "memory_search",
        "web_search",
        "read_file",
        "list_directory",
        "search_code",
    }

    def decide(
        self,
        *,
        tool_name: str,
        tool_args: dict,
        tool_definition: ToolDefinition | None,
        execution_context: ExecutionContext | None,
    ) -> PolicyDecision:
        policy_name = (
            execution_context.autonomy_policy
            if execution_context else "moderate"
        ).lower()

        if policy_name not in {"moderate", "conservative", "full"}:
            policy_name = "moderate"

        if tool_name in self._CAPABILITY_BYPASS_TOOLS:
            return PolicyDecision(True, False, "low", "control", "Safe read-only tool")

        if execution_context and execution_context.capability_grants:
            matched_grants = execution_context.grants_for(
                action_name=tool_name,
                tool_name=tool_name,
                channel=execution_context.source,
            )
            if not matched_grants:
                return PolicyDecision(
                    False,
                    False,
                    "high",
                    "capability",
                    "No capability grant matched this action.",
                )
            approval_states = {
                str(grant.get("approval_state") or "").lower() for grant in matched_grants
            }
            if approval_states & {"denied", "blocked"}:
                return PolicyDecision(
                    False,
                    False,
                    "high",
                    "capability",
                    "Capability grant explicitly denied this action.",
                )
            if approval_states & {"pending", "required"}:
                return PolicyDecision(
                    True,
                    True,
                    "high",
                    "capability",
                    "Capability grant requires an explicit approval decision.",
                )

        if tool_name == "git":
            action = str(tool_args.get("action", "")).lower()
            if action in self._READONLY_GIT_ACTIONS:
                return PolicyDecision(True, False, "low", "workspace", "Read-only git action")
            return PolicyDecision(
                True,
                policy_name != "full",
                "high",
                "workspace",
                "Git writes remain approval-gated under the active autonomy policy.",
            )

        if tool_name == "mcp_install":
            return PolicyDecision(
                True,
                True,
                "high",
                "workspace",
                "Tool installation always requires approval.",
            )

        if tool_name in self._INTERNAL_AUTONOMOUS_TOOLS:
            return PolicyDecision(
                True,
                False,
                "medium",
                "internal",
                "Allowed internal autonomy action.",
            )

        risk = (tool_definition.risk_level if tool_definition else RiskLevel.HIGH).value
        lowered_name = tool_name.lower()
        mutating_hint = any(
            keyword in lowered_name
            for keyword in ("write", "install", "deploy", "delete", "mutate", "commit", "push")
        )

        if risk == RiskLevel.HIGH.value or mutating_hint:
            return PolicyDecision(
                True,
                policy_name != "full",
                risk,
                "workspace",
                "Mutating or high-risk action requires approval.",
            )

        if tool_definition and tool_definition.requires_approval:
            return PolicyDecision(
                True,
                True,
                risk,
                "workspace",
                "Tool definition requires approval.",
            )

        if policy_name == "conservative" and risk != "low":
            return PolicyDecision(
                True,
                True,
                risk,
                "workspace",
                "Conservative autonomy gates medium-risk actions.",
            )

        return PolicyDecision(
            True,
            False,
            risk,
            "workspace",
            "Allowed by the active autonomy policy.",
        )
