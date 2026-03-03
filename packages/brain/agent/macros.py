"""
Macro Registry — reusable tool sequences for common operations.

Macros are named sequences of tool calls with template variables.
They can be:
  - Created manually by users
  - Auto-detected from repeated tool patterns by the learner
  - Shared across workspaces

Example macro:
  Name: "analyze_repo"
  Steps:
    1. host_tree(path="{{repo_root}}")
    2. host_search(query="README", path="{{repo_root}}")
    3. host_batch_read(paths=["{{repo_root}}/README.md", "{{repo_root}}/package.json"])
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("brain.agent.macros")


@dataclass
class Macro:
    """A reusable sequence of tool calls."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workspace_id: str = ""
    name: str = ""
    description: str = ""
    steps: list[dict] = field(default_factory=list)  # [{tool: "...", args: {...}}]
    variables: list[str] = field(default_factory=list)  # Template variable names
    version: int = 1
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "description": self.description,
            "steps": self.steps,
            "variables": self.variables,
            "version": self.version,
            "enabled": self.enabled,
        }


class MacroRegistry:
    """
    Manages macro definitions and execution.

    Macros are persisted to PostgreSQL and cached in memory.
    """

    def __init__(self, pool=None):
        self._pool = pool
        self._macros: dict[str, Macro] = {}  # name → Macro

    async def load_macros(self, workspace_id: str) -> None:
        """Load macros from the database for a workspace."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM macros WHERE workspace_id = $1 AND enabled = true",
                    workspace_id,
                )
            for row in rows:
                macro = Macro(
                    id=str(row["id"]),
                    workspace_id=str(row["workspace_id"]),
                    name=row["name"],
                    description=row.get("description", ""),
                    steps=json.loads(row["steps_json"]) if row["steps_json"] else [],
                    variables=json.loads(row["variables"]) if row.get("variables") else [],
                    version=row.get("version", 1),
                    enabled=row.get("enabled", True),
                )
                self._macros[macro.name] = macro
        except Exception as e:
            logger.debug(f"Macro loading skipped: {e}")

    async def create_macro(
        self,
        workspace_id: str,
        name: str,
        description: str,
        steps: list[dict],
        variables: list[str] = None,
    ) -> Macro:
        """Create a new macro."""
        macro = Macro(
            workspace_id=workspace_id,
            name=name,
            description=description,
            steps=steps,
            variables=variables or [],
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        self._macros[name] = macro

        # Persist
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO macros (id, workspace_id, name, description,
                            steps_json, variables, version, enabled)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (workspace_id, name) DO UPDATE SET
                            description = EXCLUDED.description,
                            steps_json = EXCLUDED.steps_json,
                            variables = EXCLUDED.variables,
                            version = macros.version + 1,
                            updated_at = now()
                        """,
                        macro.id, workspace_id, name, description,
                        json.dumps(steps), json.dumps(variables or []),
                        1, True,
                    )
            except Exception as e:
                logger.warning(f"Macro persistence failed: {e}")

        logger.info(f"Macro created: {name} ({len(steps)} steps)")
        return macro

    async def execute(
        self,
        name: str,
        variables: dict[str, str],
        tool_registry,
        context: dict,
    ) -> list[dict]:
        """Execute a macro by running its steps in sequence."""
        macro = self._macros.get(name)
        if not macro:
            return [{"error": f"Macro '{name}' not found"}]

        results = []
        for step in macro.steps:
            tool_name = step.get("tool", "")
            raw_args = step.get("args", {})

            # Resolve template variables
            resolved_args = self._resolve_variables(raw_args, variables)

            from agent.types import ToolCall
            tool_call = ToolCall(name=tool_name, arguments=resolved_args)

            try:
                result = await tool_registry.execute(tool_call, context=context)
                results.append({
                    "tool": tool_name,
                    "success": result.success,
                    "output": result.output[:500],
                })
                if not result.success:
                    # Stop on first failure
                    results.append({"error": f"Step failed: {result.error}"})
                    break
            except Exception as e:
                results.append({"tool": tool_name, "error": str(e)})
                break

        return results

    def _resolve_variables(self, args: dict, variables: dict) -> dict:
        """Replace {{var}} placeholders in arguments with actual values."""
        resolved = {}
        for key, value in args.items():
            if isinstance(value, str):
                for var_name, var_value in variables.items():
                    value = value.replace(f"{{{{{var_name}}}}}", var_value)
                resolved[key] = value
            elif isinstance(value, list):
                resolved[key] = [
                    self._resolve_str(item, variables) if isinstance(item, str) else item
                    for item in value
                ]
            else:
                resolved[key] = value
        return resolved

    def _resolve_str(self, s: str, variables: dict) -> str:
        for var_name, var_value in variables.items():
            s = s.replace(f"{{{{{var_name}}}}}", var_value)
        return s

    def list_macros(self, workspace_id: str = "") -> list[dict]:
        """List all available macros."""
        macros = []
        for macro in self._macros.values():
            if workspace_id and macro.workspace_id != workspace_id:
                continue
            macros.append(macro.to_dict())
        return macros

    async def delete_macro(self, name: str) -> bool:
        """Delete a macro by name."""
        if name not in self._macros:
            return False
        macro = self._macros.pop(name)
        if self._pool:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM macros WHERE id = $1", macro.id
                    )
            except Exception as e:
                logger.warning(f"Macro deletion from DB failed: {e}")
        return True
