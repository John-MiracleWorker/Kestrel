from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from agent.tool_catalog import ToolCatalogIndex
from agent.types import ToolDefinition

logger = logging.getLogger("brain.agent.tool_selector")

_ALWAYS_AVAILABLE = {
    "ask_human",
    "task_complete",
    "search_tool_catalog",
}

_PROFILE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "media": ("image", "video", "render", "visual", "photo", "dalle", "illustration", "animation"),
    "coding": ("code", "refactor", "bug", "patch", "repo", "repository", "python", "typescript"),
    "research": ("search", "research", "browse", "look up", "find out", "summarize"),
    "ops": ("deploy", "automation", "mcp", "telegram", "cron", "integration", "container", "daemon"),
    "repair": ("fix kestrel", "repair", "self improve", "self-improve", "diagnose"),
}

_PROFILE_CATEGORY_BIASES: dict[str, tuple[str, ...]] = {
    "media": ("media", "computer_use", "ui"),
    "coding": ("code", "file", "analysis", "development", "host_file"),
    "research": ("web", "data", "memory"),
    "ops": ("automation", "mcp", "social", "infrastructure", "sessions"),
    "repair": ("development", "host_file", "skill", "analysis"),
}

_DESKTOP_TASK_KEYWORDS = {
    "desktop", "gui", "screen", "window", "finder", "spotlight",
    "dock", "click", "drag", "type", "native app", "open app",
}

_RISKY_NATIVE_TOOLS = {"host_shell", "host_python", "host_exec"}
_NATIVE_MODE_PREFERRED = (
    "computer_use",
    "host_tree",
    "host_find",
    "host_search",
    "host_batch_read",
    "host_read",
    "host_list",
    "host_write",
    "file_list",
    "file_read",
    "file_write",
)

_APPROVED_STATES = {"approved", "auto_approved", "granted", "confirmed"}
_HIGH_RISK_INTENT_TAGS = {
    "allow_high_risk_tools",
    "allow_host_execution",
    "host_execution",
    "intent_host_shell",
    "intent_host_python",
}

MAX_LOCAL_TOOLS = 20
MAX_CLOUD_TOOLS = 45


class ToolSelector:
    """Local tool ranking over the live capability catalog."""

    def __init__(self, tools_or_registry: Any):
        if hasattr(tools_or_registry, "list_tools"):
            tools = list(tools_or_registry.list_tools())
            self._catalog = tools_or_registry.catalog()
        else:
            tools = list(tools_or_registry)
            self._catalog = ToolCatalogIndex(tools)
        self._tools: dict[str, ToolDefinition] = {tool.name: tool for tool in tools}

    def _resolve_runtime_mode(self, runtime_mode: Optional[str]) -> str:
        mode = (runtime_mode or os.getenv("KESTREL_RUNTIME_MODE", "docker")).strip().lower()
        if mode not in {"native", "hybrid", "docker", "container"}:
            return "docker"
        if mode == "container":
            return "docker"
        return mode

    def _extract_intent_tags(self, step_description: str, intent_tags: Optional[list[str]]) -> set[str]:
        provided = {t.strip().lower() for t in (intent_tags or []) if t and t.strip()}
        inline = set(re.findall(r"(?:#|\[)intent:([a-zA-Z0-9_\-]+)", step_description.lower()))
        return provided | inline

    def _is_desktop_task(self, step_description: str) -> bool:
        text = step_description.lower()
        return any(keyword in text for keyword in _DESKTOP_TASK_KEYWORDS)

    def _risky_tool_allowed(self, tool_name: str, approval_state: str, intent_tags: set[str]) -> bool:
        if tool_name not in _RISKY_NATIVE_TOOLS:
            return True
        if (approval_state or "").strip().lower() in _APPROVED_STATES:
            return True
        return bool(_HIGH_RISK_INTENT_TAGS.intersection(intent_tags))

    def _profile_biases(self, description: str) -> tuple[str, ...]:
        text = description.lower()
        for profile_name, keywords in _PROFILE_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return _PROFILE_CATEGORY_BIASES.get(profile_name, ())
        return ()

    def _apply_runtime_scoring(
        self,
        selected: dict[str, ToolDefinition],
        step_description: str,
        limit: int,
        runtime_mode: str,
    ) -> None:
        if runtime_mode not in {"native", "hybrid"} or not self._is_desktop_task(step_description):
            return

        for name in _NATIVE_MODE_PREFERRED:
            if len(selected) >= limit:
                break
            if name in self._tools and name not in selected:
                selected[name] = self._tools[name]

    def _apply_risk_guardrails(
        self,
        selected: dict[str, ToolDefinition],
        step_description: str,
        intent_tags: Optional[list[str]],
        approval_state: str,
    ) -> None:
        tags = self._extract_intent_tags(step_description, intent_tags)
        blocked = [
            name for name in selected
            if not self._risky_tool_allowed(name, approval_state=approval_state, intent_tags=tags)
        ]
        for name in blocked:
            selected.pop(name, None)
            logger.info(
                "Tool selector guardrail blocked risky tool '%s' (approval_state=%s, tags=%s)",
                name,
                approval_state,
                sorted(tags),
            )

    def _rank_tools(
        self,
        step_description: str,
        *,
        expected_tools: Optional[list[str]] = None,
        limit: int,
    ) -> list[ToolDefinition]:
        biases = set(self._profile_biases(step_description))
        matches = self._catalog.search(
            step_description,
            expected_tools=expected_tools,
            limit=max(limit * 3, 12),
            include_unavailable=True,
        )
        ranked: list[tuple[float, ToolDefinition]] = []
        text = step_description.lower()

        for entry in matches:
            tool = self._tools.get(entry.name)
            if not tool:
                continue

            score = 0.0
            if entry.name in (expected_tools or []):
                score += 100.0
            if entry.available:
                score += 15.0
            else:
                score -= 30.0
            if entry.category in biases:
                score += 12.0
            if "dalle" in text and entry.category == "media":
                score += 18.0
            if "web" in text and entry.category == "web":
                score += 6.0
            if entry.lifecycle_state == "approved":
                score += 5.0
            if entry.scope == "global":
                score += 2.0
            score += min(len(entry.use_cases), 3)

            ranked.append((score, tool))

        ranked.sort(key=lambda item: (-item[0], item[1].name))
        deduped: list[ToolDefinition] = []
        seen: set[str] = set()
        for _, tool in ranked:
            if tool.name in seen:
                continue
            deduped.append(tool)
            seen.add(tool.name)
            if len(deduped) >= limit:
                break
        return deduped

    async def select_with_llm(
        self,
        step_description: str,
        provider: Any,
        model: str = "",
        api_key: str = "",
        expected_tools: Optional[list[str]] = None,
        runtime_mode: Optional[str] = None,
        intent_tags: Optional[list[str]] = None,
        approval_state: str = "pending",
    ) -> list[ToolDefinition]:
        del provider, model, api_key
        return self.select(
            step_description=step_description,
            expected_tools=expected_tools,
            provider="google",
            runtime_mode=runtime_mode,
            intent_tags=intent_tags,
            approval_state=approval_state,
        )

    def select(
        self,
        step_description: str = "",
        expected_tools: Optional[list[str]] = None,
        provider: str = "google",
        max_tools: Optional[int] = None,
        runtime_mode: Optional[str] = None,
        intent_tags: Optional[list[str]] = None,
        approval_state: str = "pending",
    ) -> list[ToolDefinition]:
        is_local = provider in ("ollama", "local", "lmstudio")
        limit = max_tools or (MAX_LOCAL_TOOLS if is_local else MAX_CLOUD_TOOLS)

        selected: dict[str, ToolDefinition] = {}
        for name in _ALWAYS_AVAILABLE:
            if name in self._tools:
                selected[name] = self._tools[name]

        for tool in self._rank_tools(step_description, expected_tools=expected_tools, limit=limit):
            if len(selected) >= limit:
                break
            selected.setdefault(tool.name, tool)

        self._apply_runtime_scoring(
            selected=selected,
            step_description=step_description,
            limit=limit,
            runtime_mode=self._resolve_runtime_mode(runtime_mode),
        )
        self._apply_risk_guardrails(
            selected=selected,
            step_description=step_description,
            intent_tags=intent_tags,
            approval_state=approval_state,
        )

        tools_list = list(selected.values())
        logger.info(
            "Tool selector (local-rank): %s/%s tools for '%s...' (provider=%s)",
            len(tools_list),
            len(self._tools),
            step_description[:60],
            provider,
        )
        return tools_list

    def get_all(self) -> list[ToolDefinition]:
        return list(self._tools.values())
