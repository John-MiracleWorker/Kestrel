from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import AgentConfig
from .runtime_models import ToolSpec
from .state_store import AgentStateStore

TOOL_ENABLEMENT_FLAGS: dict[str, str] = {
    "file.write": "allow_file_write",
    "patch.apply": "allow_file_write",
    "shell.run": "allow_shell",
    "test.run": "allow_shell",
    "lint.run": "allow_shell",
    "repair.prepare": "allow_file_write",
    "repair.apply_patch": "allow_file_write",
    "repair.validate": "allow_shell",
    "repair.orchestrate_validate": "allow_shell",
    "repair.review": "allow_file_write",
    "repair.rollback": "allow_file_write",
    "codex.exec": "allow_codex_cli",
    "skill.install": "allow_file_write",
    "plugin.review": "allow_plugin_install",
    "plugin.install": "allow_plugin_install",
    "git.commit": "allow_git_commit",
    "memory.import": "allow_memory_import",
    "memory.correct": "allow_memory_import",
    "web.search": "allow_web",
    "web.fetch": "allow_web",
    "self.propose_change": "allow_self_modification",
}


@dataclass(frozen=True)
class CapabilityDecision:
    default_enabled: bool
    configured_enabled: bool
    effective_enabled: bool
    blocked_by: tuple[str, ...]
    revision: int
    updated_at: str | None
    enablement_flag: str | None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "default_enabled": self.default_enabled,
            "configured_enabled": self.configured_enabled,
            "effective_enabled": self.effective_enabled,
            "blocked_by": list(self.blocked_by),
            "revision": self.revision,
            "updated_at": self.updated_at,
            "enablement_flag": self.enablement_flag,
        }


class CapabilityPolicy:
    """Resolve durable owner choices against non-bypassable runtime gates."""

    def __init__(
        self,
        state: AgentStateStore,
        config: AgentConfig | Callable[[], AgentConfig],
    ) -> None:
        self.state = state
        self._config = config

    def tool_decision(self, spec: ToolSpec) -> CapabilityDecision:
        enablement_flag = enablement_flag_for_tool(spec)
        default_enabled = spec.source == "builtin" and not (
            spec.risk in {"high", "critical"} and enablement_flag is None
        )
        override = self.state.get_capability_override(
            "tool",
            spec.name,
            default_enabled=default_enabled,
        )
        configured_enabled = bool(override["enabled"])
        blocked_by: list[str] = []
        if not configured_enabled:
            blocked_by.append(f"tool:{spec.name}")
        stored_digest = _optional_text(override.get("resource_digest"))
        if stored_digest and stored_digest != tool_spec_digest(spec):
            blocked_by.append("resource_changed")

        config = self._active_config()
        if (
            spec.source == "builtin"
            and config.enabled_tools
            and spec.name not in config.enabled_tools
        ):
            blocked_by.append("config:enabled_tools")
        if enablement_flag is not None and not bool(getattr(config, enablement_flag, False)):
            blocked_by.append(f"config:{enablement_flag}")

        if spec.source == "skill":
            self._append_parent_blocker(
                blocked_by,
                kind="skill",
                capability_id=spec.skill_id,
            )
        elif spec.source == "mcp":
            self._append_parent_blocker(
                blocked_by,
                kind="mcp_server",
                capability_id=spec.server_id,
            )

        return CapabilityDecision(
            default_enabled=default_enabled,
            configured_enabled=configured_enabled,
            effective_enabled=not blocked_by,
            blocked_by=tuple(blocked_by),
            revision=int(override["revision"]),
            updated_at=_optional_text(override.get("updated_at")),
            enablement_flag=enablement_flag,
        )

    def parent_decision(
        self,
        kind: str,
        capability_id: str,
        *,
        entity_enabled: bool,
    ) -> CapabilityDecision:
        if kind not in {"mcp_server", "skill"}:
            raise ValueError("parent capability kind must be mcp_server or skill")
        override = self.state.get_capability_override(
            kind,
            capability_id,
            default_enabled=entity_enabled,
        )
        configured_enabled = bool(override["enabled"])
        blocked_by: list[str] = []
        if not configured_enabled:
            blocked_by.append(f"{kind}:{capability_id}")
        stored_digest = _optional_text(override.get("resource_digest"))
        if stored_digest and stored_digest != parent_resource_digest(
            self.state, kind, capability_id
        ):
            blocked_by.append("resource_changed")
        plugin_id = self._plugin_owner(kind, capability_id)
        if plugin_id is not None:
            try:
                plugin = self.state.get_plugin(plugin_id)
            except KeyError:
                blocked_by.append(f"plugin_missing:{plugin_id}")
            else:
                if not bool(plugin.get("enabled", False)):
                    blocked_by.append(f"plugin:{plugin_id}")
        return CapabilityDecision(
            default_enabled=entity_enabled,
            configured_enabled=configured_enabled,
            effective_enabled=not blocked_by,
            blocked_by=tuple(blocked_by),
            revision=int(override["revision"]),
            updated_at=_optional_text(override.get("updated_at")),
            enablement_flag=None,
        )

    def _plugin_owner(self, kind: str, capability_id: str) -> str | None:
        if kind == "skill":
            try:
                skill = self.state.get_skill(capability_id)
            except KeyError:
                return None
            provenance = dict(skill.get("manifest", {}) or {}).get("provenance")
            if isinstance(provenance, dict) and provenance.get("plugin_id"):
                return str(provenance["plugin_id"])
        prefix = "plugin."
        if capability_id.startswith(prefix):
            remainder = capability_id[len(prefix) :]
            plugin_id, separator, _child = remainder.partition(".")
            if separator and plugin_id:
                return plugin_id
        return None

    def _append_parent_blocker(
        self,
        blocked_by: list[str],
        *,
        kind: str,
        capability_id: str | None,
    ) -> None:
        if not capability_id:
            blocked_by.append(f"{kind}_missing:<unspecified>")
            return
        try:
            entity = (
                self.state.get_skill(capability_id)
                if kind == "skill"
                else self.state.get_mcp_server(capability_id)
            )
        except KeyError:
            blocked_by.append(f"{kind}_missing:{capability_id}")
            return
        parent = self.parent_decision(
            kind,
            capability_id,
            entity_enabled=bool(entity.get("enabled", False)),
        )
        if not parent.effective_enabled:
            for blocker in parent.blocked_by or (f"{kind}:{capability_id}",):
                if blocker not in blocked_by:
                    blocked_by.append(blocker)

    def _active_config(self) -> AgentConfig:
        return self._config() if callable(self._config) else self._config


def enablement_flag_for_tool(spec: ToolSpec) -> str | None:
    if spec.source == "skill" and "executable-skill" in spec.capabilities:
        return "allow_executable_skills"
    return TOOL_ENABLEMENT_FLAGS.get(spec.name)


def tool_spec_digest(spec: ToolSpec) -> str:
    payload = {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
        "risk": spec.risk,
        "requires_approval": spec.requires_approval,
        "source": spec.source,
        "server_id": spec.server_id,
        "skill_id": spec.skill_id,
        "capabilities": list(spec.capabilities),
        "produces_validation": spec.produces_validation,
        "aliases": list(spec.aliases),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parent_resource_digest(
    state: AgentStateStore,
    kind: str,
    capability_id: str,
) -> str:
    if kind == "mcp_server":
        row = state.get_mcp_server(capability_id)
        payload = {
            key: row.get(key)
            for key in (
                "id",
                "transport",
                "command",
                "args",
                "env",
                "secret_env",
                "url",
                "risk_policy",
                "tools",
            )
        }
    elif kind == "skill":
        row = state.get_skill(capability_id)
        payload = {
            "id": row["id"],
            "manifest": row.get("manifest", {}),
            "path": row.get("path", ""),
        }
    else:
        raise ValueError("parent capability kind must be mcp_server or skill")
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)
