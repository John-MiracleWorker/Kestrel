from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from ..secret_broker import build_secret_broker
from ..server_mcp_routes import mcp_public
from ..skill_manager import SkillManager
from ..state_store import AgentStateStore
from .base import AgentTool, ToolContext
from .registry import tool_enablement_status
from .workspace_tools import _safe_path


class ToolRegistryTool(AgentTool):
    spec = ToolSpec(
        name="tool.registry",
        description="List active Kestrel tools with risk, source, capabilities, and enablement metadata.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source": {"type": "string"},
                "risk": {"type": "string"},
                "capability": {"type": "string"},
                "enabled": {"type": "boolean"},
                "include_parameters": {"type": "boolean"},
            },
        },
        capabilities=("introspection", "tools", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        include_parameters = bool(arguments.get("include_parameters", True))
        query = str(arguments.get("query", "")).strip().lower()
        source = str(arguments.get("source", "")).strip().lower()
        risk = str(arguments.get("risk", "")).strip().lower()
        capability = str(arguments.get("capability", "")).strip().lower()
        enabled_filter = arguments.get("enabled")
        rows = []
        for spec in context.tool_specs:
            payload = spec.to_public_dict()
            payload.update(tool_enablement_status(spec, context.config))
            if not include_parameters:
                payload.pop("parameters", None)
            haystack = " ".join(
                [
                    str(payload.get("name", "")),
                    str(payload.get("description", "")),
                    " ".join(str(item) for item in payload.get("capabilities", [])),
                ]
            ).lower()
            if query and query not in haystack:
                continue
            if source and str(payload.get("source", "")).lower() != source:
                continue
            if risk and str(payload.get("risk", "")).lower() != risk:
                continue
            if capability and capability not in [str(item).lower() for item in payload.get("capabilities", [])]:
                continue
            if isinstance(enabled_filter, bool) and bool(payload.get("enabled", False)) is not enabled_filter:
                continue
            rows.append(payload)
        payload = {"count": len(rows), "tools": rows}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class SkillDiscoverTool(AgentTool):
    spec = ToolSpec(
        name="skill.discover",
        description="Discover local skill capsules from the configured skills directory and report validation errors.",
        parameters={"type": "object", "properties": {}},
        risk="medium",
        capabilities=("skills", "introspection", "registry-write"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del arguments
        call = ToolCall(name=self.spec.name, arguments={})
        state = AgentStateStore(context.config.state_path)
        report = SkillManager(context.config.skills_dir, state).discover_report()
        return self._result(call, success=True, content=json.dumps(report, indent=2), data=report)


class SkillInspectTool(AgentTool):
    spec = ToolSpec(
        name="skill.inspect",
        description="Inspect a persisted skill capsule manifest, validation metadata, provenance, and enabled state.",
        parameters={
            "type": "object",
            "properties": {"skill_id": {"type": "string"}},
            "required": ["skill_id"],
        },
        capabilities=("skills", "introspection", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        skill_id = str(arguments.get("skill_id", "")).strip()
        if not skill_id:
            return self._result(call, success=False, content="Missing skill_id", error="missing_skill_id")
        state = AgentStateStore(context.config.state_path)
        try:
            skill = state.get_skill(skill_id)
        except KeyError as exc:
            return self._result(call, success=False, content=str(exc), error="skill_not_found")
        payload = {"skill": skill}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class PluginRegistryTool(AgentTool):
    spec = ToolSpec(
        name="plugin.registry",
        description="List installed plugins with materialized skill and MCP counts. Does not sync or install plugins.",
        parameters={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "query": {"type": "string"},
            },
        },
        capabilities=("plugins", "introspection", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        state = AgentStateStore(context.config.state_path)
        enabled_filter = arguments.get("enabled")
        query = str(arguments.get("query", "")).strip().lower()
        skills = state.list_skills()
        servers = state.list_mcp_servers()
        rows = []
        for plugin in state.list_plugins():
            plugin_id = str(plugin["id"])
            if isinstance(enabled_filter, bool) and bool(plugin.get("enabled")) is not enabled_filter:
                continue
            haystack = f"{plugin_id} {plugin.get('name', '')} {plugin.get('description', '')}".lower()
            if query and query not in haystack:
                continue
            row = dict(plugin)
            row["materialized_skill_count"] = sum(
                1 for skill in skills if str(skill.get("id", "")).startswith(f"plugin.{plugin_id}.")
            )
            row["materialized_mcp_count"] = sum(
                1 for server in servers if str(server.get("id", "")).startswith(f"plugin.{plugin_id}.")
            )
            rows.append(row)
        payload = {"count": len(rows), "plugins": rows}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class McpRegistryTool(AgentTool):
    spec = ToolSpec(
        name="mcp.registry",
        description="List configured MCP servers and tools with redacted secret status. Does not connect or sync servers.",
        parameters={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "query": {"type": "string"},
            },
        },
        capabilities=("mcp", "introspection", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        state = AgentStateStore(context.config.state_path)
        secret_broker = build_secret_broker(
            context.config.secret_store_path, backend=context.config.secret_backend
        )
        enabled_filter = arguments.get("enabled")
        query = str(arguments.get("query", "")).strip().lower()
        rows = []
        for server in state.list_mcp_servers():
            if isinstance(enabled_filter, bool) and bool(server.get("enabled")) is not enabled_filter:
                continue
            haystack = f"{server.get('id', '')} {server.get('name', '')} {server.get('status', '')}".lower()
            if query and query not in haystack:
                continue
            rows.append(mcp_public(server, secret_broker))
        payload = {"count": len(rows), "servers": rows}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class ProjectScriptsTool(AgentTool):
    spec = ToolSpec(
        name="project.scripts",
        description="Inspect local project manifests and suggest common run/test/build commands without executing them.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
        capabilities=("workspace", "project", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            root = _safe_path(context.workspace, str(arguments.get("path", ".")))
            payload = _project_scripts(root)
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="project_scripts_failed")


def _project_scripts(root: Path) -> dict[str, Any]:
    manifests: dict[str, Any] = {}
    suggested: list[str] = []
    package_json = root / "package.json"
    if package_json.exists():
        package = json.loads(package_json.read_text(encoding="utf-8"))
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        if isinstance(scripts, dict):
            manifests["package.json"] = {"scripts": scripts}
            for name in sorted(scripts):
                suggested.append(f"npm run {name}")
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        manifests["pyproject.toml"] = {
            "project": data.get("project", {}) if isinstance(data, dict) else {},
            "tool_keys": sorted((data.get("tool", {}) or {}).keys()) if isinstance(data, dict) else [],
        }
        suggested.append("pytest -q")
        if "ruff" in (data.get("tool", {}) or {}):
            suggested.append("ruff check .")
        if "mypy" in (data.get("tool", {}) or {}):
            suggested.append("mypy src")
    makefile = root / "Makefile"
    if makefile.exists():
        targets = _makefile_targets(makefile)
        manifests["Makefile"] = {"targets": targets}
        suggested.extend(f"make {target}" for target in targets)
    return {
        "path": str(root),
        "manifests": manifests,
        "suggested_commands": sorted(dict.fromkeys(suggested)),
    }


def _makefile_targets(path: Path) -> list[str]:
    targets: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+):(?:\s|$)", line)
        if match and not match.group(1).startswith("."):
            targets.append(match.group(1))
    return sorted(dict.fromkeys(targets))
