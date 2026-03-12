"""
Permission checker — validates workspace-level skill access.
"""

import logging
import os
import json
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("hands.security.allowlist")


class PermissionChecker:
    """
    Controls which skills are allowed in which workspaces.
    By default, all skills are allowed (open mode).
    Workspaces can configure allowlists/blocklists.
    """

    def __init__(self):
        self._workspace_policies: dict[str, dict] = {}
        self._default_blocked: set[str] = set()

        # Load global blocklist (e.g., known dangerous skills)
        blocklist_path = os.getenv("SKILLS_BLOCKLIST", "")
        if blocklist_path and os.path.isfile(blocklist_path):
            with open(blocklist_path) as f:
                self._default_blocked = set(json.load(f))

    def check(self, workspace_id: str, skill_name: str,
              function_name: str = None) -> bool:
        """Check if a skill/function is allowed in a workspace."""
        # Globally blocked skills
        if skill_name in self._default_blocked:
            logger.warning(f"Blocked skill: {skill_name} (global blocklist)")
            return False

        # Workspace-specific policy
        policy = self._workspace_policies.get(workspace_id)
        if not policy:
            return True  # No policy = allow all

        mode = policy.get("mode", "allow_all")

        if mode == "allowlist":
            allowed = policy.get("skills", [])
            return skill_name in allowed

        if mode == "blocklist":
            blocked = policy.get("skills", [])
            return skill_name not in blocked

        return True

    def evaluate_action(
        self,
        *,
        workspace_id: str,
        action_name: str,
        function_name: str = "",
        grants: list[dict] | None = None,
        mutating: bool = False,
    ) -> dict:
        """Evaluate workspace policy plus typed capability grants."""
        if not self.check(workspace_id, action_name, function_name):
            return {
                "allowed": False,
                "failure_class": "permission_denied",
                "reason": f"Action '{action_name}' is blocked by workspace policy.",
                "matched_grants": [],
            }

        normalized_grants = [self._normalize_grant(grant) for grant in (grants or [])]
        matched_grants = [
            grant for grant in normalized_grants if self._grant_matches(
                grant,
                workspace_id=workspace_id,
                action_name=action_name,
                function_name=function_name,
            )
        ]

        if matched_grants:
            approval_states = {
                str(grant.get("approval_state") or "").lower() for grant in matched_grants
            }
            if approval_states & {"denied", "blocked"}:
                return {
                    "allowed": False,
                    "failure_class": "permission_denied",
                    "reason": "Capability grant explicitly denied this action.",
                    "matched_grants": matched_grants,
                }
            if approval_states & {"pending", "required"}:
                return {
                    "allowed": False,
                    "failure_class": "escalation_required",
                    "reason": "Capability grant requires approval before execution.",
                    "matched_grants": matched_grants,
                }
            return {
                "allowed": True,
                "failure_class": "none",
                "reason": "Capability grant admitted the action.",
                "matched_grants": matched_grants,
            }

        if mutating:
            return {
                "allowed": False,
                "failure_class": "escalation_required",
                "reason": "Mutating actions require an explicit capability grant.",
                "matched_grants": [],
            }

        return {
            "allowed": True,
            "failure_class": "none",
            "reason": "No explicit grant was required for this non-mutating action.",
            "matched_grants": [],
        }

    def set_policy(self, workspace_id: str, policy: dict):
        """Set workspace permission policy."""
        self._workspace_policies[workspace_id] = policy
        logger.info(f"Policy updated for workspace {workspace_id}: {policy.get('mode')}")

    def get_policy(self, workspace_id: str) -> Optional[dict]:
        return self._workspace_policies.get(workspace_id)

    @staticmethod
    def _normalize_grant(grant: dict) -> dict:
        return {
            "grant_id": str(grant.get("grant_id") or ""),
            "workspace_id": str(grant.get("workspace_id") or ""),
            "action_selector": str(grant.get("action_selector") or ""),
            "tool_selector": str(grant.get("tool_selector") or ""),
            "approval_state": str(grant.get("approval_state") or ""),
            "expires_at": str(grant.get("expires_at") or ""),
            "metadata": grant.get("metadata") or {},
        }

    @staticmethod
    def _grant_matches(
        grant: dict,
        *,
        workspace_id: str,
        action_name: str,
        function_name: str,
    ) -> bool:
        if grant["workspace_id"] and grant["workspace_id"] != workspace_id:
            return False
        if grant["expires_at"]:
            try:
                expires_at = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
                if expires_at <= datetime.now(timezone.utc):
                    return False
            except ValueError:
                pass
        selector_candidates = {
            action_name,
            function_name,
            f"{action_name}.{function_name}" if function_name else action_name,
        }
        if grant["action_selector"] and grant["action_selector"] not in selector_candidates | {"*"}:
            return False
        if grant["tool_selector"] and grant["tool_selector"] not in selector_candidates | {"*"}:
            return False
        return True
