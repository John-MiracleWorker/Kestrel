"""Native execution policy evaluation and approval providers."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger("brain.agent.security.native_policy")


@dataclass(frozen=True)
class NativeExecutionRequest:
    workspace_id: str
    tool_name: str
    function_name: str
    command: str
    command_class: str
    interactive: bool = False


@dataclass(frozen=True)
class ApprovalResult:
    approved: bool
    reason: str
    provider: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    requires_approval: bool = False


class ApprovalProvider(Protocol):
    async def approve(self, request: NativeExecutionRequest) -> ApprovalResult:
        """Return an approval decision for this request."""


class NonInteractiveApprovalProvider:
    """Approval provider for headless execution environments."""

    def __init__(self, mode: str = "deny"):
        self.mode = mode

    async def approve(self, request: NativeExecutionRequest) -> ApprovalResult:
        approved = self.mode == "allow"
        reason = (
            "AUTO_APPROVED_NON_INTERACTIVE"
            if approved
            else "DENIED_NON_INTERACTIVE"
        )
        return ApprovalResult(
            approved=approved,
            reason=reason,
            provider=f"non_interactive:{self.mode}",
        )


class MacOSDialogApprovalProvider:
    """Interactive approval provider backed by osascript on macOS."""

    async def approve(self, request: NativeExecutionRequest) -> ApprovalResult:
        if platform.system().lower() != "darwin":
            return ApprovalResult(
                approved=False,
                reason="INTERACTIVE_PROVIDER_UNAVAILABLE",
                provider="macos_dialog",
            )

        safe_command = request.command.replace('"', '\\"')
        if len(safe_command) > 150:
            safe_command = safe_command[:147] + "..."

        script = f'''
        try
            set dialogResult to display dialog "Kestrel Agent OS is requesting to natively execute:\\n\\n{safe_command}" buttons {{"Deny", "Approve"}} default button "Deny" with icon caution with title "Security Authorization"
            if button returned of dialogResult is "Approve" then
                return "true"
            else
                return "false"
            end if
        on error number -128
            return "false"
        end try
        '''

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            approved = stdout.decode().strip() == "true"
            return ApprovalResult(
                approved=approved,
                reason=("APPROVED_INTERACTIVE" if approved else "DENIED_INTERACTIVE"),
                provider="macos_dialog",
            )
        except Exception as exc:  # pragma: no cover - subprocess failures are env-specific
            logger.error("macOS approval dialog failed: %s", exc)
            return ApprovalResult(
                approved=False,
                reason="INTERACTIVE_PROVIDER_ERROR",
                provider="macos_dialog",
            )


@dataclass
class WorkspaceNativePolicy:
    mode: str = "allow_all"
    command_classes: list[str] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)


class NativePolicyEvaluator:
    """Workspace-scoped policy evaluator for native execution requests."""

    _DANGEROUS_RE = re.compile(
        r"(\brm\b\s+-rf|\bshutdown\b|\breboot\b|\bmkfs\b|\bdd\b|\bchmod\b\s+777|\bsudo\b)",
        re.IGNORECASE,
    )
    _NETWORK_RE = re.compile(r"\b(curl|wget|nc|ncat|ssh|scp)\b", re.IGNORECASE)
    _READ_ONLY_RE = re.compile(r"^\s*(ls|cat|pwd|echo|which|whoami|date|id)\b", re.IGNORECASE)

    def __init__(self):
        self._workspace_policies: dict[str, WorkspaceNativePolicy] = {}

    def set_policy(self, workspace_id: str, policy: dict[str, Any]) -> None:
        self._workspace_policies[workspace_id] = WorkspaceNativePolicy(
            mode=policy.get("mode", "allow_all"),
            command_classes=policy.get("command_classes", []),
            rules=policy.get("rules", []),
        )

    def get_policy(self, workspace_id: str) -> WorkspaceNativePolicy | None:
        policy = self._workspace_policies.get(workspace_id)
        if policy:
            return policy

        allowed_patterns = self._load_legacy_allowlist_patterns()
        if not allowed_patterns:
            return None

        return WorkspaceNativePolicy(
            mode="allowlist",
            rules=[
                {"effect": "allow", "pattern": pattern, "command_classes": ["shell"]}
                for pattern in allowed_patterns
            ],
        )

    def classify(self, tool_name: str, payload: str) -> str:
        if tool_name == "host_python":
            return "code"

        if self._DANGEROUS_RE.search(payload):
            return "destructive"
        if self._NETWORK_RE.search(payload):
            return "network"
        if self._READ_ONLY_RE.search(payload):
            return "read_only"
        return "shell"

    def evaluate(self, request: NativeExecutionRequest) -> PolicyDecision:
        policy = self.get_policy(request.workspace_id)
        if policy is None:
            return PolicyDecision(
                allowed=True,
                reason="NO_WORKSPACE_POLICY",
                requires_approval=request.command_class != "read_only",
            )

        rule_decision = self._evaluate_rules(policy, request)
        if rule_decision is not None:
            return rule_decision

        if policy.command_classes:
            if policy.mode == "allowlist":
                allowed = request.command_class in policy.command_classes
                return PolicyDecision(
                    allowed=allowed,
                    reason="ALLOWLIST_COMMAND_CLASS",
                    requires_approval=allowed and request.command_class != "read_only",
                )

            if policy.mode == "blocklist":
                allowed = request.command_class not in policy.command_classes
                return PolicyDecision(
                    allowed=allowed,
                    reason="BLOCKLIST_COMMAND_CLASS",
                    requires_approval=allowed and request.command_class != "read_only",
                )

        return PolicyDecision(
            allowed=True,
            reason=f"POLICY_MODE_{policy.mode.upper()}",
            requires_approval=request.command_class != "read_only",
        )

    def _evaluate_rules(
        self,
        policy: WorkspaceNativePolicy,
        request: NativeExecutionRequest,
    ) -> PolicyDecision | None:
        for rule in policy.rules:
            pattern = rule.get("pattern")
            if not pattern:
                continue

            try:
                if not re.search(pattern, request.command):
                    continue
            except re.error:
                logger.warning("Invalid native policy regex pattern: %s", pattern)
                continue

            classes = rule.get("command_classes", [])
            if classes and request.command_class not in classes:
                continue

            effect = rule.get("effect", "deny")
            if effect == "allow":
                return PolicyDecision(
                    allowed=True,
                    reason="ALLOW_RULE_MATCH",
                    requires_approval=request.command_class != "read_only",
                )

            return PolicyDecision(allowed=False, reason="DENY_RULE_MATCH")

        return None

    @staticmethod
    def _load_legacy_allowlist_patterns() -> list[str]:
        allowlist_path = os.path.expanduser("~/.kestrel/allowlist.yml")
        if not os.path.exists(allowlist_path):
            return []

        try:
            import yaml

            with open(allowlist_path, "r", encoding="utf-8") as file_handle:
                config = yaml.safe_load(file_handle) or {}
                patterns = config.get("allowed_commands", [])
                return [pattern for pattern in patterns if isinstance(pattern, str)]
        except Exception as exc:
            logger.warning("Error reading native legacy allowlist: %s", exc)
            return []


def make_default_approval_provider(interactive: bool) -> ApprovalProvider:
    if interactive:
        return MacOSDialogApprovalProvider()

    mode = os.getenv("NATIVE_APPROVAL_NON_INTERACTIVE_MODE", "deny").strip().lower()
    if mode not in {"allow", "deny"}:
        mode = "deny"
    return NonInteractiveApprovalProvider(mode=mode)


DEFAULT_NATIVE_POLICY_EVALUATOR = NativePolicyEvaluator()


def set_workspace_native_policy(workspace_id: str, policy: dict[str, Any]) -> None:
    """Register/update workspace native execution policy."""
    DEFAULT_NATIVE_POLICY_EVALUATOR.set_policy(workspace_id, policy)


def get_workspace_native_policy(workspace_id: str) -> WorkspaceNativePolicy | None:
    """Get workspace native execution policy."""
    return DEFAULT_NATIVE_POLICY_EVALUATOR.get_policy(workspace_id)
