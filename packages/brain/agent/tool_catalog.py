from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from agent.types import ToolDefinition

_WORD_RE = re.compile(r"[a-zA-Z0-9]{2,}")

_CATEGORY_USE_CASES: dict[str, tuple[str, ...]] = {
    "analysis": ("inspect outputs", "review code or data", "diagnose failures"),
    "automation": ("schedule jobs", "manage recurring tasks", "trigger workflows"),
    "code": ("run code", "generate charts", "execute scripts"),
    "computer_use": ("operate the browser", "click through UIs", "capture screenshots"),
    "control": ("ask the user", "finish the task", "pause for approval"),
    "data": ("analyze CSV or JSON", "summarize tables", "compute statistics"),
    "development": ("repair the codebase", "scan architecture", "propose patches"),
    "file": ("read files", "write files", "inspect project structure"),
    "general": ("inspect system state", "check health", "gather diagnostics"),
    "host_file": ("inspect host files", "search the host filesystem", "edit host paths"),
    "infrastructure": ("control containers", "restart services", "manage system state"),
    "mcp": ("use connected integrations", "call external MCP servers", "discover tools"),
    "media": ("generate images", "generate videos", "render visuals"),
    "memory": ("store long-term facts", "search past context", "retrieve knowledge"),
    "sessions": ("inspect sessions", "message sessions", "review session history"),
    "skill": ("create skills", "manage reusable tools", "improve capabilities"),
    "social": ("send notifications", "post activity", "publish updates"),
    "system": ("inspect runtime health", "list processes", "check capacity"),
    "ui": ("build UI artifacts", "update components", "list workspace UIs"),
    "web": ("search the web", "browse pages", "fetch online content"),
}


def _tokenize(text: str) -> set[str]:
    normalized = re.sub(r"[_\-./]+", " ", text or "").lower()
    return {match.group(0) for match in _WORD_RE.finditer(normalized)}


def _default_catalog_dir() -> Path:
    return Path(
        os.getenv(
            "KESTREL_CATALOG_DIR",
            str(Path(__file__).resolve().parents[3] / ".kestrel"),
        )
    )


@dataclass(frozen=True)
class ToolCatalogEntry:
    name: str
    aliases: tuple[str, ...]
    category: str
    risk_level: str
    description: str
    use_cases: tuple[str, ...]
    source: str
    scope: str
    lifecycle_state: str
    available: bool
    availability_reason: str
    markdown: str
    search_text: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "category": self.category,
            "risk_level": self.risk_level,
            "description": self.description,
            "use_cases": list(self.use_cases),
            "source": self.source,
            "scope": self.scope,
            "state": self.lifecycle_state,
            "available": self.available,
            "availability_reason": self.availability_reason,
        }


class ToolCatalogIndex:
    """Live searchable catalog rendered to Markdown and JSON."""

    def __init__(
        self,
        tools: Iterable[ToolDefinition],
        *,
        markdown_path: str | None = None,
        json_path: str | None = None,
        availability_resolver: Callable[[ToolDefinition], tuple[bool, str]] | None = None,
    ) -> None:
        base_dir = _default_catalog_dir()
        self.markdown_path = str(Path(markdown_path) if markdown_path else base_dir / "tool-catalog.md")
        self.json_path = str(Path(json_path) if json_path else base_dir / "tool-catalog.json")
        self._availability_resolver = availability_resolver
        self.entries = self._build_entries(list(tools))
        self._write_catalog_files()

    def search(
        self,
        query: str,
        *,
        expected_tools: list[str] | None = None,
        limit: int = 12,
        include_unavailable: bool = True,
    ) -> list[ToolCatalogEntry]:
        tokens = _tokenize(query)
        query_lower = (query or "").lower()
        expected = {name.strip() for name in (expected_tools or []) if name and name.strip()}
        scored: list[tuple[float, ToolCatalogEntry]] = []

        for entry in self.entries:
            if not include_unavailable and not entry.available:
                continue

            score = 0.0
            if entry.name in expected:
                score += 200.0

            if entry.name.lower() in query_lower:
                score += 40.0

            for alias in entry.aliases:
                if alias.lower() in query_lower:
                    score += 24.0

            entry_tokens = _tokenize(entry.search_text)
            overlap = tokens & entry_tokens
            score += 5.0 * len(overlap)

            if entry.category in query_lower:
                score += 4.0

            if entry.available:
                score += 3.0

            if entry.lifecycle_state == "approved":
                score += 2.0

            if any(token in entry.description.lower() for token in tokens):
                score += 1.5

            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [entry for _, entry in scored[:limit]]

    def capability_summary(self) -> str:
        categories = sorted({entry.category for entry in self.entries if entry.category})
        return ", ".join(categories)

    def prompt_capability_summary(self, *, limit: int = 8) -> str:
        categories = sorted({entry.category for entry in self.entries if entry.category})
        return ", ".join(categories[:limit])

    def to_dict(self) -> list[dict]:
        return [entry.to_dict() for entry in self.entries]

    def _build_entries(self, tools: list[ToolDefinition]) -> list[ToolCatalogEntry]:
        entries: list[ToolCatalogEntry] = []
        for tool in sorted(tools, key=lambda item: item.name):
            use_cases = tuple(tool.use_cases or _CATEGORY_USE_CASES.get(tool.category, ("use the tool directly",)))
            available, availability_reason = self._resolve_availability(tool)
            markdown = self._render_entry(tool, use_cases, available, availability_reason)
            search_text = "\n".join(
                (
                    tool.name,
                    " ".join(tool.aliases),
                    tool.category,
                    tool.description,
                    " ".join(use_cases),
                    tool.source,
                    tool.scope,
                    tool.lifecycle_state,
                    " ".join(tool.availability_requirements),
                )
            ).lower()
            entries.append(
                ToolCatalogEntry(
                    name=tool.name,
                    aliases=tuple(tool.aliases),
                    category=tool.category,
                    risk_level=tool.risk_level.value,
                    description=tool.description.strip(),
                    use_cases=use_cases,
                    source=tool.source,
                    scope=tool.scope,
                    lifecycle_state=tool.lifecycle_state,
                    available=available,
                    availability_reason=availability_reason,
                    markdown=markdown,
                    search_text=search_text,
                )
            )
        return entries

    def _resolve_availability(self, tool: ToolDefinition) -> tuple[bool, str]:
        if tool.lifecycle_state != "approved":
            return False, f"Tool state is {tool.lifecycle_state}"
        if self._availability_resolver:
            try:
                return self._availability_resolver(tool)
            except Exception:
                pass
        return True, "Available"

    def _render_entry(
        self,
        tool: ToolDefinition,
        use_cases: tuple[str, ...],
        available: bool,
        availability_reason: str,
    ) -> str:
        alias_text = ", ".join(tool.aliases) if tool.aliases else "(none)"
        use_case_lines = "\n".join(f"- {item}" for item in use_cases)
        requirements = ", ".join(tool.availability_requirements) if tool.availability_requirements else "(none)"
        return (
            f"## {tool.name}\n"
            f"- Aliases: {alias_text}\n"
            f"- Category: {tool.category}\n"
            f"- Risk: {tool.risk_level.value}\n"
            f"- Source: {tool.source}\n"
            f"- Scope: {tool.scope}\n"
            f"- State: {tool.lifecycle_state}\n"
            f"- Available: {'yes' if available else 'no'}\n"
            f"- Availability reason: {availability_reason}\n"
            f"- Requirements: {requirements}\n"
            f"- Description: {tool.description.strip()}\n"
            f"- Use cases:\n{use_case_lines}\n"
        )

    def _write_catalog_files(self) -> None:
        markdown_path = Path(self.markdown_path)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        json_path = Path(self.json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)

        markdown_body = "# Kestrel Tool Catalog\n\n" + "\n".join(entry.markdown for entry in self.entries)
        markdown_path.write_text(markdown_body, encoding="utf-8")
        json_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
