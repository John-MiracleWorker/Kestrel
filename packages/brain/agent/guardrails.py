"""
Guardrails — multi-layer safety system for agent tool execution.

Layers:
  1. Risk classification — each tool has a risk level
  2. Budget enforcement — max tokens, API calls, wall-clock time
  3. Pattern detection — block known-dangerous command patterns
  4. Rate limiting — prevent infinite loops
  5. Approval gates — require human approval for high-risk actions
"""

import logging
import re
from typing import Optional

from agent.types import (
    AgentTask,
    GuardrailConfig,
    RiskLevel,
)

logger = logging.getLogger("brain.agent.guardrails")


# ── Dangerous Patterns ───────────────────────────────────────────────

# Patterns that should always be blocked, regardless of risk level
BLOCKED_PATTERNS = [
    # Destructive file operations
    r"rm\s+-rf\s+/",
    r"rm\s+-rf\s+\*",
    r"rmdir\s+/s\s+/q",
    r"del\s+/f\s+/s\s+/q",
    r"format\s+[a-zA-Z]:",
    # Database destruction
    r"DROP\s+DATABASE",
    r"DROP\s+SCHEMA.*CASCADE",
    r"TRUNCATE\s+.*CASCADE",
    # System damage
    r"shutdown\s+(-h|/s)",
    r"mkfs\.",
    r"dd\s+if=.*of=/dev/",
    r":[(][)]\s*[{]\s*:[|]:&\s*[}]",  # Fork bomb
    # Credential exfiltration
    r"curl.*[-]d.*password",
    r"wget.*password",
    r"cat\s+/etc/(passwd|shadow)",
    r"cat\s+.*\.env",
    # Network exfiltration
    r"nc\s+-e",
    r"ncat\s+-e",
    r"bash\s+-i\s+>&\s+/dev/tcp",
]

# Compile patterns for performance
_compiled_patterns = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]


class Guardrails:
    """
    Multi-layer guardrail system for agent safety.

    Checks tool calls against risk levels, budgets, blocklists,
    and rate limits before allowing execution.
    """

    def __init__(self):
        self._tool_call_timestamps: dict[str, list[float]] = {}  # task_id → timestamps

    def check_budget(self, task: AgentTask) -> Optional[str]:
        """
        Check if the task has exceeded its resource budget.
        Returns an error message if exceeded, None if OK.
        """
        config = task.config

        if task.iterations >= config.max_iterations:
            return (
                f"Iteration limit reached ({task.iterations}/{config.max_iterations}). "
                "The task has been running too long."
            )

        if task.tool_calls_count >= config.max_tool_calls:
            return (
                f"Tool call limit reached ({task.tool_calls_count}/{config.max_tool_calls}). "
                "Too many tools have been invoked."
            )

        if task.token_usage >= config.max_tokens:
            return (
                f"Token budget exhausted ({task.token_usage:,}/{config.max_tokens:,}). "
                "The task has consumed too many tokens."
            )

        return None

    def needs_approval(
        self,
        tool_name: str,
        tool_args: dict,
        config: GuardrailConfig,
    ) -> Optional[str]:
        """
        Check if a tool call requires human approval.
        Returns a reason string if approval needed, None if auto-approved.
        """
        # 1. Check blocked patterns first
        block_reason = self._check_blocked_patterns(tool_name, tool_args, config)
        if block_reason:
            return f"BLOCKED: {block_reason}"

        # 2. Check if tool is in the always-approve list
        if tool_name in config.require_approval_tools:
            return f"Tool '{tool_name}' is configured to always require approval"

        # 3. Check risk level against auto-approve threshold
        from agent.tools import ToolRegistry
        # The registry is passed at a higher level; here we use the
        # risk level from the tool definition
        risk = self._get_tool_risk(tool_name)

        if risk == RiskLevel.CRITICAL:
            return f"Tool '{tool_name}' has CRITICAL risk level — always requires approval"

        if risk == RiskLevel.HIGH and config.auto_approve_risk.value < "high":
            return f"Tool '{tool_name}' has HIGH risk — exceeds auto-approve threshold"

        # 4. Rate limiting
        rate_issue = self._check_rate_limit(tool_name)
        if rate_issue:
            return rate_issue

        return None  # Auto-approved

    def _check_blocked_patterns(
        self,
        tool_name: str,
        tool_args: dict,
        config: GuardrailConfig,
    ) -> Optional[str]:
        """Check if tool arguments contain blocked patterns."""
        # Serialize args to string for pattern matching
        args_str = str(tool_args)

        # Check built-in patterns
        for pattern in _compiled_patterns:
            if pattern.search(args_str):
                return f"Dangerous pattern detected: {pattern.pattern}"

        # Check user-configured patterns
        for pattern_str in config.blocked_patterns:
            try:
                if re.search(pattern_str, args_str, re.IGNORECASE):
                    return f"Custom blocked pattern matched: {pattern_str}"
            except re.error:
                pass  # Invalid regex, skip

        return None

    def _check_rate_limit(self, tool_name: str) -> Optional[str]:
        """Check if tool calls are happening too fast (possible infinite loop)."""
        import time

        now = time.monotonic()
        key = tool_name

        if key not in self._tool_call_timestamps:
            self._tool_call_timestamps[key] = []

        timestamps = self._tool_call_timestamps[key]

        # Remove timestamps older than 60 seconds
        timestamps[:] = [t for t in timestamps if now - t < 60]
        timestamps.append(now)

        # More than 20 calls to the same tool in 60 seconds
        if len(timestamps) > 20:
            return (
                f"Rate limit: '{tool_name}' called {len(timestamps)} times in 60s. "
                "Possible infinite loop detected."
            )

        return None

    def _get_tool_risk(self, tool_name: str) -> RiskLevel:
        """Get the risk level for a tool (fallback mapping)."""
        # Hardcoded fallback mapping for when we don't have the registry
        risk_map = {
            "code_execute": RiskLevel.MEDIUM,
            "web_search": RiskLevel.LOW,
            "web_browse": RiskLevel.MEDIUM,
            "file_read": RiskLevel.LOW,
            "file_write": RiskLevel.MEDIUM,
            "file_list": RiskLevel.LOW,
            "database_query": RiskLevel.MEDIUM,
            "database_mutate": RiskLevel.HIGH,
            "memory_search": RiskLevel.LOW,
            "memory_store": RiskLevel.LOW,
            "ask_human": RiskLevel.LOW,
            "task_complete": RiskLevel.LOW,
            "api_call": RiskLevel.MEDIUM,
        }
        return risk_map.get(tool_name, RiskLevel.HIGH)  # Unknown = HIGH

    def validate_config(self, config: GuardrailConfig) -> list[str]:
        """
        Validate a guardrail config for sanity.
        Returns a list of warnings (not errors — configs are always accepted).
        """
        warnings = []

        if config.max_iterations > 100:
            warnings.append("max_iterations > 100 may lead to very long-running tasks")

        if config.max_tokens > 500_000:
            warnings.append("max_tokens > 500k may incur significant LLM costs")

        if config.max_wall_time_seconds > 3600:
            warnings.append("max_wall_time > 1 hour — consider breaking into smaller tasks")

        if config.auto_approve_risk == RiskLevel.CRITICAL:
            warnings.append(
                "auto_approve_risk=CRITICAL will auto-approve ALL tool calls, "
                "including destructive operations"
            )

        return warnings
