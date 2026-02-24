from __future__ import annotations
"""
Dynamic Skill Creation — agents create and register new tools at runtime.

A skill is a user-defined Python function that the agent can invoke
just like a built-in tool. Skills are:
  - Sandboxed (restricted globals — no os, subprocess, sys)
  - Persisted to PostgreSQL per-workspace
  - Loaded on startup and available across sessions
  - Versioned with usage tracking
"""

import ast
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from agent.types import RiskLevel, ToolDefinition, ToolResult

logger = logging.getLogger("brain.agent.skills")

# Modules explicitly blocked in skill code
BLOCKED_MODULES = {
    "os", "sys", "subprocess", "shutil", "importlib",
    "ctypes", "signal", "socket", "http", "urllib",
    "requests", "pathlib", "io", "builtins",
}

# Allowed globals for sandboxed execution
SAFE_GLOBALS = {
    "__builtins__": {
        "len": len, "range": range, "enumerate": enumerate,
        "zip": zip, "map": map, "filter": filter,
        "sorted": sorted, "reversed": reversed,
        "min": min, "max": max, "sum": sum, "abs": abs,
        "round": round, "pow": pow,
        "int": int, "float": float, "str": str, "bool": bool,
        "list": list, "dict": dict, "set": set, "tuple": tuple,
        "isinstance": isinstance, "type": type,
        "print": lambda *a, **kw: None,  # No-op print
        "True": True, "False": False, "None": None,
    },
    "json": json,
    "math": __import__("math"),
    "re": __import__("re"),
    "datetime": __import__("datetime"),
}


@dataclass
class Skill:
    """A user-created tool backed by Python code."""
    id: str
    workspace_id: str
    name: str
    description: str
    python_code: str
    parameters: dict[str, Any]
    risk_level: RiskLevel = RiskLevel.MEDIUM
    created_by: str = ""
    enabled: bool = True
    usage_count: int = 0
    created_at: str = ""

    def to_tool_definition(self) -> ToolDefinition:
        """Convert to a standard ToolDefinition for the registry."""
        return ToolDefinition(
            name=f"skill_{self.name}",
            description=f"[Custom Skill] {self.description}",
            parameters=self.parameters,
            risk_level=self.risk_level,
            requires_approval=self.risk_level == RiskLevel.HIGH,
            timeout_seconds=30,
            category="skill",
        )


class SkillManager:
    """
    Manages dynamic skill creation, validation, and execution.

    Skills are persisted in PostgreSQL and loaded into the tool registry
    at startup and when new skills are created.
    """

    def __init__(self, pool, tool_registry=None):
        self._pool = pool
        self._registry = tool_registry
        self._loaded_skills: dict[str, Skill] = {}

    async def create_skill(
        self,
        workspace_id: str,
        name: str,
        description: str,
        python_code: str,
        parameters: dict,
        created_by: str,
    ) -> tuple[bool, str]:
        """
        Create and register a new skill.
        Returns (success, message).
        """
        # 1. Validate the name
        if not name.isidentifier():
            return False, f"Invalid skill name: '{name}'. Must be a valid Python identifier."

        # 2. Validate the code for safety
        is_safe, reason = self._validate_code(python_code)
        if not is_safe:
            return False, f"Code validation failed: {reason}"

        # 3. Test-compile the code
        try:
            compile(python_code, f"skill_{name}", "exec")
        except SyntaxError as e:
            return False, f"Syntax error in skill code: {e}"

        # 4. Persist to database
        skill_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO agent_skills (id, workspace_id, name, description,
                        python_code, parameters, risk_level, created_by, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
                    ON CONFLICT (workspace_id, name) DO UPDATE SET
                        description = EXCLUDED.description,
                        python_code = EXCLUDED.python_code,
                        parameters = EXCLUDED.parameters,
                        updated_at = now()
                    """,
                    skill_id, workspace_id, name, description,
                    python_code, json.dumps(parameters),
                    RiskLevel.MEDIUM.value, created_by, now,
                )
        except Exception as e:
            return False, f"Failed to persist skill: {e}"

        # 5. Register in the tool registry
        skill = Skill(
            id=skill_id,
            workspace_id=workspace_id,
            name=name,
            description=description,
            python_code=python_code,
            parameters=parameters,
            created_by=created_by,
            created_at=now,
        )
        self._loaded_skills[f"{workspace_id}:{name}"] = skill

        if self._registry:
            self._registry.register_dynamic(skill.to_tool_definition(), self)

        logger.info(f"Skill created: {name} in workspace {workspace_id}")
        return True, f"Skill '{name}' created and registered successfully."

    async def execute_skill(
        self,
        skill_name: str,
        args: dict,
        workspace_id: str,
    ) -> ToolResult:
        """Execute a skill in a sandboxed environment."""
        key = f"{workspace_id}:{skill_name}"
        skill = self._loaded_skills.get(key)

        if not skill or not skill.enabled:
            return ToolResult(
                tool_call_id="",
                success=False,
                error=f"Skill '{skill_name}' not found or disabled",
            )

        import time
        start = time.monotonic()

        try:
            # Build sandboxed namespace
            namespace = dict(SAFE_GLOBALS)
            namespace["args"] = args

            # Execute the skill code
            exec(skill.python_code, namespace)

            # The skill should define a `run(args)` function
            if "run" not in namespace:
                return ToolResult(
                    tool_call_id="",
                    success=False,
                    error="Skill must define a `run(args)` function",
                )

            result = namespace["run"](args)
            elapsed = int((time.monotonic() - start) * 1000)

            # Update usage count
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE agent_skills SET usage_count = usage_count + 1 WHERE id = $1",
                        skill.id,
                    )
            except Exception:
                pass

            return ToolResult(
                tool_call_id="",
                success=True,
                output=str(result) if result is not None else "Skill executed successfully.",
                execution_time_ms=elapsed,
            )

        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            return ToolResult(
                tool_call_id="",
                success=False,
                error=f"Skill execution error: {e}",
                execution_time_ms=elapsed,
            )

    async def load_workspace_skills(self, workspace_id: str) -> int:
        """Load all skills for a workspace from the database."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM agent_skills WHERE workspace_id = $1 AND enabled = true",
                    workspace_id,
                )

            count = 0
            for row in rows:
                skill = Skill(
                    id=row["id"],
                    workspace_id=row["workspace_id"],
                    name=row["name"],
                    description=row["description"],
                    python_code=row["python_code"],
                    parameters=json.loads(row["parameters"]) if isinstance(row["parameters"], str) else row["parameters"],
                    created_by=row["created_by"],
                    usage_count=row["usage_count"],
                    enabled=row["enabled"],
                    created_at=str(row["created_at"]),
                )
                self._loaded_skills[f"{workspace_id}:{skill.name}"] = skill

                if self._registry:
                    self._registry.register_dynamic(skill.to_tool_definition(), self)
                count += 1

            logger.info(f"Loaded {count} skills for workspace {workspace_id}")
            return count

        except Exception as e:
            logger.error(f"Failed to load skills: {e}")
            return 0

    async def list_skills(self, workspace_id: str) -> list[dict]:
        """List all skills for a workspace."""
        return [
            {"name": s.name, "description": s.description, "usage_count": s.usage_count}
            for key, s in self._loaded_skills.items()
            if key.startswith(f"{workspace_id}:")
        ]

    def _validate_code(self, code: str) -> tuple[bool, str]:
        """
        Static analysis of skill code for safety.
        Checks for import of blocked modules, dangerous calls, etc.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"Syntax error: {e}"

        for node in ast.walk(tree):
            # Block imports of dangerous modules
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.Import):
                    module = node.names[0].name.split(".")[0]
                elif node.module:
                    module = node.module.split(".")[0]

                if module in BLOCKED_MODULES:
                    return False, f"Import of '{module}' is not allowed in skills"

            # Block exec/eval inside skills (meta-exec)
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in ("exec", "eval", "compile", "__import__"):
                    return False, f"Call to '{func.id}' is not allowed in skills"

            # Block attribute access to dangerous things
            if isinstance(node, ast.Attribute):
                if node.attr in ("__subclasses__", "__bases__", "__class__", "__globals__"):
                    return False, f"Access to '{node.attr}' is not allowed"

        # Check that a `run` function is defined
        function_names = [
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        ]
        if "run" not in function_names:
            return False, "Skill must define a `run(args)` function"

        return True, "OK"
