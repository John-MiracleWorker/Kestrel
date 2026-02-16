"""
Permission checker — validates workspace-level skill access.
"""

import logging
import os
import json
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

    def set_policy(self, workspace_id: str, policy: dict):
        """Set workspace permission policy."""
        self._workspace_policies[workspace_id] = policy
        logger.info(f"Policy updated for workspace {workspace_id}: {policy.get('mode')}")

    def get_policy(self, workspace_id: str) -> Optional[dict]:
        return self._workspace_policies.get(workspace_id)
