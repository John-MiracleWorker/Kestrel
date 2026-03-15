from __future__ import annotations

from typing import Any


def _agent_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    agent = (config or {}).get("agent") or {}
    return agent if isinstance(agent, dict) else {}


def personality_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    personality = _agent_settings(config).get("personality") or {}
    if not isinstance(personality, dict):
        personality = {}
    intensity = str(personality.get("intensity") or "high").strip().lower() or "high"
    if intensity not in {"low", "medium", "high"}:
        intensity = "high"
    profile = str(personality.get("profile") or "operator").strip().lower() or "operator"
    return {
        "enabled": bool(personality.get("enabled", True)),
        "profile": profile,
        "intensity": intensity,
    }


def proactivity_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    proactivity = _agent_settings(config).get("proactivity") or {}
    if not isinstance(proactivity, dict):
        proactivity = {}
    mode = str(proactivity.get("mode") or "aggressive").strip().lower() or "aggressive"
    if mode not in {"conservative", "moderate", "aggressive"}:
        mode = "aggressive"
    execution = str(proactivity.get("background_execution") or "suggest_first").strip().lower() or "suggest_first"
    if execution not in {"notify_only", "suggest_first", "auto_start_safe"}:
        execution = "auto_start_safe"
    return {
        "mode": mode,
        "background_execution": execution,
    }


def build_native_persona_block(
    *,
    config: dict[str, Any] | None,
    role: str,
) -> str:
    personality = personality_settings(config)
    proactivity = proactivity_settings(config)
    lines = [f"You are Kestrel's native {role}."]
    if personality["enabled"]:
        if personality["profile"] == "operator":
            lines.extend(
                [
                    "Voice: observant, confident, slightly playful, and concise.",
                    "Be explicit about what is running in the background, what is blocked, and what needs approval.",
                ]
            )
        else:
            lines.append(f"Use the saved {personality['profile']} persona while staying concise and operationally clear.")
        if personality["intensity"] == "low":
            lines.append("Keep the personality subtle and restrained.")
        elif personality["intensity"] == "medium":
            lines.append("Let the personality show, but keep the wording tight.")
        else:
            lines.append("The voice can be distinct, but never theatrical.")
    else:
        lines.append("Use a plain, matter-of-fact tone.")
    lines.extend(
        [
            f"Proactivity mode: {proactivity['mode']} with background execution set to {proactivity['background_execution']}.",
            "No roleplay, no filler, and no fake confidence.",
            "Never claim a tool action, background job, or side effect happened unless it actually happened.",
        ]
    )
    return "\n".join(lines)


def build_ambient_state_block(ambient_state: dict[str, Any] | None) -> str:
    if not isinstance(ambient_state, dict):
        return ""
    lines: list[str] = []
    pending = int(ambient_state.get("pending_approvals_count") or 0)
    lines.append(f"- Pending approvals: {pending}")

    last_heartbeat = str(ambient_state.get("last_heartbeat_action") or "").strip()
    if last_heartbeat:
        lines.append(f"- Last heartbeat action: {last_heartbeat}")

    watched = [
        str(item).strip()
        for item in list(ambient_state.get("watched_changes") or [])[:5]
        if str(item).strip()
    ]
    if watched:
        lines.append(f"- Recent watched-file changes: {', '.join(watched)}")

    background = []
    for item in list(ambient_state.get("recent_background_tasks") or [])[:3]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip() or "unknown"
        goal = str(item.get("goal") or item.get("summary") or "").strip()
        if goal:
            background.append(f"{status}: {goal}")
    if background:
        lines.append(f"- Recent background tasks: {'; '.join(background)}")

    suggestions = []
    for item in list(ambient_state.get("background_suggestions") or [])[:3]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("goal") or "").strip()
        if title:
            suggestions.append(title)
    if suggestions:
        lines.append(f"- Pending suggestions: {'; '.join(suggestions)}")

    approvals = []
    for item in list(ambient_state.get("pending_approvals") or [])[:3]:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or item.get("command") or item.get("operation") or "").strip()
        if summary:
            approvals.append(summary)
    if approvals:
        lines.append(f"- Approval queue: {'; '.join(approvals)}")

    if not lines:
        return ""
    return "Ambient state:\n" + "\n".join(lines)


def compose_native_system_prompt(
    *,
    config: dict[str, Any] | None,
    role: str,
    role_instructions: str,
    ambient_state: dict[str, Any] | None = None,
    workspace_system_prompt: str = "",
    skill_prompt_block: str = "",
) -> str:
    sections = [
        build_native_persona_block(config=config, role=role),
    ]
    ambient_block = build_ambient_state_block(ambient_state)
    if ambient_block:
        sections.append(ambient_block)
    workspace_block = str(workspace_system_prompt or "").strip()
    if workspace_block:
        sections.append(f"Saved workspace system prompt:\n{workspace_block}")
    skill_block = str(skill_prompt_block or "").strip()
    if skill_block:
        sections.append(f"Relevant skill packs:\n{skill_block}")
    instructions = str(role_instructions or "").strip()
    if instructions:
        sections.append(instructions)
    return "\n\n".join(section for section in sections if section.strip())
