"""
Two-Stage Tool Selector — uses a lightweight LLM call to pick relevant
tools, then sends only those tools' full schemas for execution.

Stage 1 (Tool Picker):
  - Sends a compact 1-line-per-tool catalog (~600 tokens) to the LLM
  - LLM picks 1-5 tools needed for the task
  - Very fast — tiny prompt, tiny response

Stage 2 (Executor):
  - Only the picked tools' full OpenAI schemas are sent to the LLM
  - Keeps context small for local models
  - Eliminates tool hallucination (model only sees real tools)

Fallback:
  - If the LLM call fails, falls back to keyword-based selection
"""

import logging
import re
from typing import Optional, Any

from agent.types import ToolDefinition

logger = logging.getLogger("brain.agent.tool_selector")

# ── Core tools that are ALWAYS available ─────────────────────────────
_ALWAYS_AVAILABLE = {
    "ask_human",
    "task_complete",
}

# ── Category keyword mapping (fallback) ─────────────────────────────
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
        "project", "codebase", "source", "improve", "scan", "patch",
        "fix", "edit", "modify", "analyze",
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
        "cron", "job", "kill", "process", "stuck", "failing",
    ],
    "skill": [
        "skill", "create_skill", "learn", "improve", "self_improve",
        "proposal", "scan", "patch", "fix", "self-improvement",
        "codebase", "analyze",
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
    "host_exec": "system",
    "host_search": "file",
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

MAX_LOCAL_TOOLS = 20
MAX_CLOUD_TOOLS = 45

# ── Compact catalog for Stage 1 ─────────────────────────────────────
# One-line description per tool, ~40 chars each.

def _build_catalog(tools: dict[str, ToolDefinition]) -> str:
    """Build a compact tool catalog string for the LLM picker."""
    lines = []
    for name, tool in sorted(tools.items()):
        if name in _ALWAYS_AVAILABLE:
            continue  # Don't list core tools — they're always added
        desc = tool.description.split(".")[0].split("\n")[0][:60]
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


_PICKER_PROMPT = """You are a tool selector. Given a task, pick the tools needed from this catalog.

TOOLS:
{catalog}

TASK: {task}

Reply with ONLY the tool names needed (1-5 tools), one per line. No explanations."""


class ToolSelector:
    """
    Two-stage tool selector:
      1. LLM picks tools from a compact catalog (fast, cheap)
      2. Only those tools' full schemas are sent for execution

    Falls back to keyword matching if LLM call fails.
    """

    def __init__(self, tools: list[ToolDefinition]):
        self._tools: dict[str, ToolDefinition] = {t.name: t for t in tools}
        self._catalog = _build_catalog(self._tools)

        # Build reverse index for keyword fallback
        self._category_index: dict[str, set[str]] = {}
        for name, tool in self._tools.items():
            cat = _TOOL_CATEGORY_MAP.get(name, tool.category)
            self._category_index.setdefault(cat, set()).add(name)

    async def select_with_llm(
        self,
        step_description: str,
        provider: Any,
        model: str = "",
        api_key: str = "",
        expected_tools: Optional[list[str]] = None,
    ) -> list[ToolDefinition]:
        """
        Stage 1: Ask the LLM to pick relevant tools from the catalog.
        Returns the selected ToolDefinition objects (always includes core tools).
        Falls back to keyword-based selection on failure.
        """
        selected: dict[str, ToolDefinition] = {}

        # Always include core tools
        for name in _ALWAYS_AVAILABLE:
            if name in self._tools:
                selected[name] = self._tools[name]

        # Always include planner's expected tools
        if expected_tools:
            for name in expected_tools:
                if name in self._tools:
                    selected[name] = self._tools[name]

        try:
            prompt = _PICKER_PROMPT.format(
                catalog=self._catalog,
                task=step_description[:200],
            )

            # Use the provider's generate method (no tools, just text)
            # Most providers: generate() returns str
            # Some providers: generate() returns dict with "content"
            try:
                response = await provider.generate(
                    messages=[{"role": "user", "content": prompt}],
                    model=model,
                    temperature=0.0,
                    max_tokens=100,
                )
            except TypeError:
                # Some providers may have different signatures
                response = await provider.generate(
                    messages=[{"role": "user", "content": prompt}],
                    model=model,
                )

            # Parse the response — extract tool names
            content = ""
            if isinstance(response, dict):
                content = response.get("content", "")
            elif isinstance(response, str):
                content = response
            else:
                # Try async generator (streaming)
                chunks = []
                async for chunk in response:
                    if isinstance(chunk, dict):
                        chunks.append(chunk.get("content", ""))
                    elif isinstance(chunk, str):
                        chunks.append(chunk)
                content = "".join(chunks)

            # Strip <think> tags if present
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
            # Also strip unclosed <think> tags
            content = re.sub(r"<think>.*$", "", content, flags=re.DOTALL)

            # Extract tool names (one per line, possibly with bullet/dash prefix)
            for line in content.strip().splitlines():
                line = line.strip().lstrip("-•*123456789.)")
                tool_name = line.strip().split()[0] if line.strip() else ""
                # Remove any trailing punctuation
                tool_name = tool_name.rstrip(",:;.")
                if tool_name in self._tools:
                    selected[tool_name] = self._tools[tool_name]

            if len(selected) > len(_ALWAYS_AVAILABLE):
                logger.info(
                    f"Tool selector (LLM): picked {list(selected.keys())} "
                    f"for '{step_description[:50]}...'"
                )
                return list(selected.values())
            else:
                logger.warning(
                    f"LLM tool picker returned no valid tools from: "
                    f"{content[:100]!r} — falling back to keywords"
                )

        except Exception as e:
            logger.warning(f"LLM tool picker failed: {e} — falling back to keywords")

        # ── Keyword fallback ───────────────────────────────────────
        return self._select_by_keywords(
            step_description=step_description,
            expected_tools=expected_tools,
            provider_name="lmstudio",
        )

    def select(
        self,
        step_description: str = "",
        expected_tools: Optional[list[str]] = None,
        provider: str = "google",
        max_tools: Optional[int] = None,
    ) -> list[ToolDefinition]:
        """Synchronous keyword-based fallback (legacy)."""
        return self._select_by_keywords(
            step_description=step_description,
            expected_tools=expected_tools,
            provider_name=provider,
            max_tools=max_tools,
        )

    def _select_by_keywords(
        self,
        step_description: str = "",
        expected_tools: Optional[list[str]] = None,
        provider_name: str = "google",
        max_tools: Optional[int] = None,
    ) -> list[ToolDefinition]:
        """Keyword-based tool selection (fallback)."""
        is_local = provider_name in ("ollama", "local", "lmstudio")
        limit = max_tools or (MAX_LOCAL_TOOLS if is_local else MAX_CLOUD_TOOLS)

        selected: dict[str, ToolDefinition] = {}

        # 1. Core tools
        for name in _ALWAYS_AVAILABLE:
            if name in self._tools:
                selected[name] = self._tools[name]

        # 2. Expected tools
        if expected_tools:
            for name in expected_tools:
                if name in self._tools:
                    selected[name] = self._tools[name]

        # 3. Category matching
        matched_categories = self._match_categories(step_description)
        for cat in matched_categories:
            for name in self._category_index.get(cat, []):
                if len(selected) >= limit:
                    break
                if name in self._tools:
                    selected[name] = self._tools[name]

        # 4. Keyword matching
        if len(selected) < limit:
            desc_lower = step_description.lower()
            for name, tool in self._tools.items():
                if name in selected:
                    continue
                if len(selected) >= limit:
                    break
                name_parts = name.replace("_", " ").split()
                if any(part in desc_lower for part in name_parts if len(part) > 2):
                    selected[name] = tool

        tools_list = list(selected.values())
        logger.info(
            f"Tool selector (keywords): {len(tools_list)}/{len(self._tools)} tools "
            f"for '{step_description[:60]}...' "
            f"(provider={provider_name}, categories={matched_categories})"
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
        matched.sort(reverse=True)
        return [cat for _, cat in matched]

    def get_all(self) -> list[ToolDefinition]:
        """Return all tools."""
        return list(self._tools.values())
