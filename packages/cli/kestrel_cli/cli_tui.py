from __future__ import annotations

from typing import Any

from .tui import KestrelTextualApp, launch_tui
from .tui.store import component_summary, summarize_event


def summarize_task_events(events: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for event in events:
        summary = summarize_event(event)
        if summary.startswith(("STEP:", "DONE:", "COMPLETE:", "FAILED:", "ERROR:", "STATUS:")):
            lines.append(summary)
    return lines[-12:] or ["Task finished without streamed events."]


def build_skill_detail_lines(pack: dict[str, Any]) -> list[str]:
    if not pack:
        return ["No skill pack selected."]
    lines = [
        f"ID: {pack.get('pack_id', '')}",
        f"Name: {pack.get('name') or pack.get('pack_id') or ''}",
        f"Version: {pack.get('version') or '0.0.0'}",
        f"Source: {pack.get('source_type') or pack.get('root_kind') or 'unknown'}",
        f"Scope: {pack.get('scope') or pack.get('root_kind') or 'unknown'}",
        f"Enabled: {'yes' if pack.get('enabled') else 'no'}",
        f"Trusted: {'yes' if pack.get('trusted') else 'no'}",
        f"Components: {component_summary(pack)}",
    ]
    dependencies = pack.get("dependencies") or []
    dependency_ids = [
        str(item.get("pack_id") or "")
        for item in dependencies
        if isinstance(item, dict) and str(item.get("pack_id") or "").strip()
    ]
    if dependency_ids:
        lines.append(f"Depends on: {', '.join(dependency_ids)}")
    description = str(pack.get("description") or "").strip()
    if description:
        lines.extend(["", "Description:", description])
    prompt_preview = str(pack.get("prompt_preview") or "").strip()
    if prompt_preview:
        lines.extend(["", "Prompt preview:", prompt_preview])
    return lines


__all__ = [
    "KestrelTextualApp",
    "launch_tui",
    "summarize_task_events",
    "build_skill_detail_lines",
]
