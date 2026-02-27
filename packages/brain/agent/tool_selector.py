"""
Semantic Tool Selector — filters the full tool registry down to the
most relevant tools for a given step, instead of sending all 38 schemas
to the LLM.

Strategy (hybrid):
  1. Always-available tools — small core set (ask_human, task_complete)
  2. Step expected_tools — the planner already predicts which tools a step needs
  3. Category matching — map keywords in the step description to tool categories
  4. Keyword matching — match step description against tool descriptions

This keeps the LLM's tool context small (3-8 tools) which:
  - Makes ollama viable for tool calls (no 400 Bad Request on huge schemas)
  - Reduces cloud token usage / cost
  - Improves tool selection accuracy (less noise)
"""

import logging
import re
from typing import Optional

from agent.types import ToolDefinition

logger = logging.getLogger("brain.agent.tool_selector")

# ── Core tools that are ALWAYS available ─────────────────────────────
# These are lightweight control-flow tools the agent needs on every step.
_ALWAYS_AVAILABLE = {
    "ask_human",
    "task_complete",
}

# ── Category keyword mapping ────────────────────────────────────────
# Maps keywords found in the step description to tool categories.
# Tools have a `category` field (code, web, file, memory, control, etc.)
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "code": [
        "code", "script", "python", "javascript", "execute", "run",
        "program", "compile", "debug", "function", "class",
    ],
    "web": [
        "web", "search", "browse", "url", "http", "site", "page",
        "google", "internet", "online", "fetch", "scrape",
    ],
    "file": [
        "file", "read", "write", "directory", "folder", "path",
        "create", "delete", "list", "tree", "find", "workspace",
        "project", "codebase", "source",
    ],
    "memory": [
        "remember", "memory", "recall", "store", "knowledge",
        "learned", "past", "previous", "history",
    ],
    "data": [
        "data", "csv", "json", "analyze", "statistics", "chart",
        "table", "parse", "dataset",
    ],
    "social": [
        "post", "tweet", "social", "moltbook", "share", "publish",
        "feed", "timeline",
    ],
    "mcp": [
        "mcp", "server", "connect", "gmail", "email", "integration",
        "service", "api", "external", "plugin",
    ],
    "git": [
        "git", "commit", "push", "pull", "branch", "merge",
        "repository", "repo", "clone", "diff",
    ],
    "system": [
        "container", "docker", "rebuild", "restart", "deploy",
        "health", "status", "system", "server", "service",
    ],
    "skill": [
        "skill", "create_skill", "learn", "improve", "self_improve",
        "proposal",
    ],
    "delegation": [
        "delegate", "parallel", "sub-agent", "specialist",
        "research", "multi-agent",
    ],
    "computer": [
        "computer", "screen", "screenshot", "click", "type",
        "browser", "automation", "ui",
    ],
}

# ── Tool name → category overrides ──────────────────────────────────
# For tools whose names don't match their category cleanly.
_TOOL_CATEGORY_MAP: dict[str, str] = {
    "code_execute": "code",
    "web_search": "web",
    "web_browse": "web",
    "file_read": "file",
    "file_write": "file",
    "file_list": "file",
    "host_read": "file",
    "host_write": "file",
    "host_list": "file",
    "host_tree": "file",
    "host_find": "file",
    "host_exec": "code",
    "project_recall": "memory",
    "memory_store": "memory",
    "memory_search": "memory",
    "data_analyze": "data",
    "moltbook": "social",
    "mcp_connect": "mcp",
    "mcp_call": "mcp",
    "mcp_disconnect": "mcp",
    "mcp_status": "mcp",
    "git": "git",
    "system_health": "system",
    "container_control": "system",
    "self_improve": "skill",
    "create_skill": "skill",
    "delegate_task": "delegation",
    "delegate_parallel": "delegation",
    "computer_use": "computer",
    "ask_human": "control",
    "task_complete": "control",
}

# Maximum tools to send to a local model (ollama)
MAX_LOCAL_TOOLS = 8
# Maximum tools to send to a cloud model
MAX_CLOUD_TOOLS = 20


class ToolSelector:
    """
    Selects the most relevant tools for a given step.

    Usage:
        selector = ToolSelector(all_tool_definitions)
        filtered = selector.select(
            step_description="Search the web for...",
            expected_tools=["web_search"],
            provider="ollama",
        )
    """

    def __init__(self, tools: list[ToolDefinition]):
        self._tools: dict[str, ToolDefinition] = {t.name: t for t in tools}

        # Build reverse index: category → tool names
        self._category_index: dict[str, set[str]] = {}
        for name, tool in self._tools.items():
            cat = _TOOL_CATEGORY_MAP.get(name, tool.category)
            self._category_index.setdefault(cat, set()).add(name)

    def select(
        self,
        step_description: str = "",
        expected_tools: Optional[list[str]] = None,
        provider: str = "google",
        max_tools: Optional[int] = None,
    ) -> list[ToolDefinition]:
        """
        Select relevant tools for a step.

        Priority order:
          1. Always-available tools (ask_human, task_complete)
          2. Planner's expected_tools (already predicted)
          3. Category matches from step description keywords
          4. If still under max, add high-affinity keyword matches

        Returns list of ToolDefinition objects.
        """
        is_local = provider in ("ollama", "local")
        limit = max_tools or (MAX_LOCAL_TOOLS if is_local else MAX_CLOUD_TOOLS)

        selected: dict[str, ToolDefinition] = {}

        # 1. Always-available core tools
        for name in _ALWAYS_AVAILABLE:
            if name in self._tools:
                selected[name] = self._tools[name]

        # 2. Planner's expected tools (highest priority)
        if expected_tools:
            for name in expected_tools:
                if name in self._tools:
                    selected[name] = self._tools[name]

        # 3. Category matching from description keywords
        matched_categories = self._match_categories(step_description)
        for cat in matched_categories:
            for name in self._category_index.get(cat, []):
                if len(selected) >= limit:
                    break
                if name in self._tools:
                    selected[name] = self._tools[name]

        # 4. Keyword matching against tool descriptions (fill remaining slots)
        if len(selected) < limit:
            desc_lower = step_description.lower()
            for name, tool in self._tools.items():
                if name in selected:
                    continue
                if len(selected) >= limit:
                    break
                # Check if the tool name or key words appear in the step description
                name_parts = name.replace("_", " ").split()
                if any(part in desc_lower for part in name_parts if len(part) > 2):
                    selected[name] = tool

        tools_list = list(selected.values())
        logger.info(
            f"Tool selector: {len(tools_list)}/{len(self._tools)} tools "
            f"for '{step_description[:60]}...' "
            f"(provider={provider}, categories={matched_categories})"
        )
        return tools_list

    def _match_categories(self, description: str) -> list[str]:
        """Find tool categories that match the step description."""
        text = description.lower()
        matched = []
        for category, keywords in _CATEGORY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                matched.append((score, category))
        # Sort by relevance (higher score first)
        matched.sort(reverse=True)
        return [cat for _, cat in matched]

    def get_all(self) -> list[ToolDefinition]:
        """Return all tools (for cloud providers that can handle it)."""
        return list(self._tools.values())
