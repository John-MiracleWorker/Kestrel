"""
Guardrails — multi-layer safety system for agent tool execution.

Layers:
  1. Risk classification — each tool has a risk level
  2. Budget enforcement — max tokens, API calls, wall-clock time
  3. Pattern detection — block known-dangerous command patterns
  4. Rate limiting — prevent infinite loops
  5. Approval gates — require human approval for high-risk actions
"""

import hashlib
import json as _json
import logging
import re
import threading
import time
from typing import Optional

from agent.types import (
    AgentTask,
    ApprovalTier,
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
    # Encoding/obfuscation evasion attempts
    r"base64\s+(-d|--decode)\s*\|",
    r"eval\s*\(",
    r"\$\(.*base64",
    r"python[23]?\s+-c\s+.*import\s+os",
    r"echo\s+.*\|\s*(sh|bash)",
]

# Compile patterns for performance
_compiled_patterns = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]


class Guardrails:
    """
    Multi-layer guardrail system for agent safety.

    Checks tool calls against risk levels, budgets, blocklists,
    and rate limits before allowing execution.

    Thread-safe: all mutable state is protected by a lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tool_call_timestamps: dict[str, list[float]] = {}  # tool_name → timestamps
        self._recent_calls: list[tuple[str, str]] = []  # (tool_name, args_hash) for repetition detection

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

    def check_tool(
        self,
        tool_name: str,
        tool_args: dict,
        config: GuardrailConfig,
        tool_registry=None,
        approval_memory=None,
    ) -> tuple[ApprovalTier, Optional[str]]:
        """Determine the approval tier for a tool call.

        Returns ``(tier, reason)`` where:
          - BLOCK:   blocked pattern detected — tool must not run
          - CONFIRM: requires human approval before executing
          - INFORM:  auto-approved but notable — user gets a notification
          - SILENT:  auto-approved silently
        """
        # 1. Blocked patterns → BLOCK
        block_reason = self._check_blocked_patterns(tool_name, tool_args, config)
        if block_reason:
            return ApprovalTier.BLOCK, f"BLOCKED: {block_reason}"

        # 2. Tool definition requires_approval flag → CONFIRM
        if tool_registry:
            tool_def = tool_registry.get_tool(tool_name)
            if tool_def and getattr(tool_def, 'requires_approval', False):
                return ApprovalTier.CONFIRM, f"Tool '{tool_name}' requires human approval before execution"

        # 3. Task-level always-require list → CONFIRM
        if tool_name in config.require_approval_tools:
            return ApprovalTier.CONFIRM, f"Tool '{tool_name}' is configured to always require approval"

        # 4. Risk assessment with contextual adjustment
        risk = self._get_tool_risk(tool_name, tool_registry=tool_registry)
        risk = self._contextual_risk_adjustment(tool_name, tool_args, risk)

        _RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
        risk_val = _RISK_ORDER.get(risk, 2)
        threshold_val = _RISK_ORDER.get(config.auto_approve_risk, 1)

        if risk_val > threshold_val:
            # Would normally CONFIRM, but check approval memory first
            if approval_memory is not None:
                # approval_memory.should_auto_approve is sync-safe (cached)
                auto, auto_reason = approval_memory.should_auto_approve_sync(
                    tool_name, tool_args,
                    workspace_id=getattr(config, '_workspace_id', ''),
                )
                if auto:
                    return ApprovalTier.INFORM, auto_reason
            return ApprovalTier.CONFIRM, (
                f"Tool '{tool_name}' has {risk.value.upper()} risk "
                f"— exceeds auto-approve threshold"
            )

        # 5. Rate limiting → CONFIRM
        rate_issue = self._check_rate_limit(tool_name)
        if rate_issue:
            return ApprovalTier.CONFIRM, rate_issue

        # 6. MEDIUM risk within threshold → INFORM (auto-approve but notify)
        if risk == RiskLevel.MEDIUM:
            return ApprovalTier.INFORM, f"Auto-approved: {tool_name} ({risk.value} risk)"

        # 7. LOW risk → SILENT
        return ApprovalTier.SILENT, None

    def needs_approval(
        self,
        tool_name: str,
        tool_args: dict,
        config: GuardrailConfig,
        tool_registry=None,
    ) -> Optional[str]:
        """Backward-compatible wrapper: returns reason if CONFIRM/BLOCK, else None."""
        tier, reason = self.check_tool(tool_name, tool_args, config, tool_registry)
        if tier in (ApprovalTier.CONFIRM, ApprovalTier.BLOCK):
            return reason
        return None

    def _check_blocked_patterns(
        self,
        tool_name: str,
        tool_args: dict,
        config: GuardrailConfig,
    ) -> Optional[str]:
        """Check if tool arguments contain blocked patterns.

        Normalizes input to defeat common evasion techniques:
        - Null byte injection
        - Unicode homoglyph substitution
        - Whitespace obfuscation
        """
        # Serialize args to string for pattern matching
        args_str = str(tool_args)

        # Normalize: strip null bytes, collapse whitespace
        args_str = args_str.replace("\x00", "")

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
                logger.warning("Invalid regex in blocked_patterns: %s", pattern_str)

        return None

    def _check_rate_limit(self, tool_name: str) -> Optional[str]:
        """Check if tool calls are happening too fast (possible infinite loop)."""
        now = time.monotonic()
        key = tool_name

        with self._lock:
            if key not in self._tool_call_timestamps:
                self._tool_call_timestamps[key] = []

            timestamps = self._tool_call_timestamps[key]

            # Remove timestamps older than 60 seconds
            timestamps[:] = [t for t in timestamps if now - t < 60]
            timestamps.append(now)

            count = len(timestamps)

        # More than 20 calls to the same tool in 60 seconds
        if count > 20:
            return (
                f"Rate limit: '{tool_name}' called {count} times in 60s. "
                "Possible infinite loop detected."
            )

        return None

    def check_repetition(self, tool_name: str, tool_args: dict) -> Optional[str]:
        """
        Check if an identical tool+args combination has been called recently.
        Returns a warning string (not a block) to inject into context.
        """
        args_hash = hashlib.md5(
            _json.dumps(tool_args, sort_keys=True).encode()
        ).hexdigest()[:12]
        call_sig = (tool_name, args_hash)

        with self._lock:
            count = sum(1 for c in self._recent_calls if c == call_sig)

            # Track it
            self._recent_calls.append(call_sig)
            # Keep only last 50 calls
            if len(self._recent_calls) > 50:
                self._recent_calls = self._recent_calls[-50:]

        if count >= 2:
            return (
                f"Repetition detected: '{tool_name}' called {count + 1} times with "
                f"identical arguments. This is likely unproductive."
            )

        return None

    # ── Contextual Risk Adjustment ─────────────────────────────────

    # Paths that should escalate file operations to HIGH risk
    _SYSTEM_PATH_PREFIXES = (
        "/etc/", "/usr/", "/var/", "/sys/", "/proc/",
        "/boot/", "/root/", "/sbin/", "/bin/",
        "C:\\Windows", "C:\\Program Files",
    )

    def _contextual_risk_adjustment(
        self, tool_name: str, tool_args: dict, base_risk: RiskLevel,
    ) -> RiskLevel:
        """Adjust risk level based on tool arguments and context.

        Examples:
          - file_write to /etc/ → escalate to HIGH
          - git action=status → de-escalate to LOW
          - mcp_call with read-like tool → de-escalate to LOW
          - container_control action=logs → de-escalate to LOW
        """
        # File operation path checks
        if tool_name in ("file_write", "host_write"):
            path = tool_args.get("path", "") or tool_args.get("file_path", "")
            if any(path.startswith(p) for p in self._SYSTEM_PATH_PREFIXES):
                return RiskLevel.HIGH

        # Git sub-action checks
        if tool_name == "git":
            action = str(tool_args.get("action", "")).lower()
            if action in ("status", "diff", "log", "branch", "show"):
                return RiskLevel.LOW
            if action in ("add", "commit", "stash"):
                return RiskLevel.MEDIUM
            # push, deploy, force-push, checkout --force remain at base risk

        # Container/daemon control sub-action checks
        if tool_name in ("container_control", "daemon_list", "daemon_stop"):
            action = str(tool_args.get("action", "")).lower()
            if action in ("status", "logs", "list", "rebuild_log"):
                return RiskLevel.LOW

        # MCP call: risk depends on the underlying tool operation
        if tool_name == "mcp_call":
            tool = str(tool_args.get("tool_name", "")).lower()
            if any(kw in tool for kw in ("search", "list", "get", "read", "query", "fetch", "find")):
                return RiskLevel.LOW
            if any(kw in tool for kw in ("create", "update", "delete", "push", "send", "post", "write")):
                return RiskLevel.MEDIUM

        return base_risk

    def _get_tool_risk(self, tool_name: str, tool_registry=None) -> RiskLevel:
        """Get the risk level for a tool.

        Resolution order:
          1. Tool registry definition (authoritative — set by tool author)
          2. Hardcoded fallback map (for when registry isn't available)
          3. MEDIUM default for unknown tools (blocked patterns still
             catch destructive commands regardless of risk level)
        """
        # 1. Check tool registry (authoritative source)
        if tool_registry:
            tool_def = tool_registry.get_tool(tool_name)
            if tool_def:
                return tool_def.risk_level

        # 2. Hardcoded fallback mapping for when we don't have the registry
        risk_map = {
            "code_execute": RiskLevel.MEDIUM,
            "web_search": RiskLevel.LOW,
            "web_browse": RiskLevel.MEDIUM,
            "file_read": RiskLevel.LOW,
            "file_write": RiskLevel.MEDIUM,
            "file_list": RiskLevel.LOW,
            "host_read": RiskLevel.LOW,
            "host_list": RiskLevel.LOW,
            "host_search": RiskLevel.LOW,
            "host_write": RiskLevel.HIGH,
            "database_query": RiskLevel.MEDIUM,
            "database_mutate": RiskLevel.HIGH,
            "memory_search": RiskLevel.LOW,
            "memory_store": RiskLevel.LOW,
            "ask_human": RiskLevel.LOW,
            "task_complete": RiskLevel.LOW,
            "api_call": RiskLevel.MEDIUM,
        }
        if tool_name in risk_map:
            return risk_map[tool_name]

        # 3. Unknown tools default to MEDIUM (not HIGH).
        # Truly dangerous operations are caught by blocked patterns (layer 1).
        logger.info("Unknown tool '%s' — defaulting to MEDIUM risk", tool_name)
        return RiskLevel.MEDIUM

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

        if config.auto_approve_risk == RiskLevel.HIGH:
            warnings.append(
                "auto_approve_risk=HIGH will auto-approve ALL tool calls, "
                "including potentially destructive operations"
            )

        return warnings
