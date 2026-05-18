from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .models import MemoryHit

ONBOARDING_SCHEMA_VERSION = "kestrel_onboarding_profile.v1"
SELF_PROFILE_SCHEMA = "user_profile"
SELF_PROFILE_QUERY = (
    "kestrel_onboarding_profile user_profile agent_name user_name preferred_name "
    "persona persona_id working_style goals interests communication_notes"
)
DEFAULT_PERSONA_ID = "steady"

PERSONA_PRESETS: tuple[dict[str, str], ...] = (
    {
        "id": "steady",
        "name": "Steady Companion",
        "summary": "Warm, grounded, concise, and quietly capable.",
        "guidance": "Be warm and direct. Keep momentum, explain tradeoffs clearly, and avoid performative enthusiasm.",
    },
    {
        "id": "mentor",
        "name": "Patient Mentor",
        "summary": "Explains reasoning, teaches patterns, and checks understanding without dragging.",
        "guidance": "Be patient and instructional. Explain the why behind decisions while keeping the next action clear.",
    },
    {
        "id": "spark",
        "name": "Creative Spark",
        "summary": "More playful, imaginative, and idea-forward while staying useful.",
        "guidance": "Bring more creative options and a livelier voice, but keep answers practical and grounded in evidence.",
    },
    {
        "id": "operator",
        "name": "Calm Operator",
        "summary": "Precise, terse, and technical for focused execution.",
        "guidance": "Be crisp and operational. Lead with facts, actions, blockers, and verification evidence.",
    },
)


def persona_presets_public() -> list[dict[str, str]]:
    return [dict(persona) for persona in PERSONA_PRESETS]


def build_onboarding_profile(payload: dict[str, Any]) -> dict[str, Any]:
    persona = persona_by_id(str(payload.get("persona") or payload.get("persona_id") or DEFAULT_PERSONA_ID))
    user_name = _clean_text(payload.get("user_name"), max_chars=80)
    preferred_name = _clean_text(payload.get("preferred_name"), fallback=user_name, max_chars=80)
    agent_name = _clean_text(payload.get("agent_name"), fallback="Kestrel", max_chars=80)
    profile = {
        "schema_version": ONBOARDING_SCHEMA_VERSION,
        "setup_complete": True,
        "agent_name": agent_name,
        "user_name": user_name,
        "preferred_name": preferred_name,
        "persona": persona["id"],
        "persona_name": persona["name"],
        "persona_summary": persona["summary"],
        "persona_guidance": persona["guidance"],
        "working_style": _clean_text(payload.get("working_style"), max_chars=600),
        "goals": _clean_list(payload.get("goals"), max_items=6, max_chars=120),
        "interests": _clean_list(payload.get("interests"), max_items=6, max_chars=120),
        "communication_notes": _clean_text(payload.get("communication_notes"), max_chars=700),
        "continuous_learning": bool(payload.get("continuous_learning", True)),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    return profile


def persona_by_id(persona_id: str) -> dict[str, str]:
    normalized = persona_id.strip().lower()
    return next((persona for persona in PERSONA_PRESETS if persona["id"] == normalized), PERSONA_PRESETS[0])


def onboarding_record_title(profile: dict[str, Any]) -> str:
    agent_name = str(profile.get("agent_name") or "Kestrel")
    user_name = str(profile.get("preferred_name") or profile.get("user_name") or "the user")
    return f"Kestrel onboarding profile: {agent_name} for {user_name}"


def onboarding_record_content(profile: dict[str, Any]) -> str:
    return json.dumps(profile, indent=2, sort_keys=True)


def onboarding_state_from_reflection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    profile = latest_onboarding_profile_from_rows(rows)
    return {
        "completed": bool(profile),
        "profile": profile,
        "personas": persona_presets_public(),
    }


def latest_onboarding_profile_from_hits(hits: list[MemoryHit]) -> dict[str, Any] | None:
    return _latest_profile([_profile_from_text(hit.record.content) for hit in hits])


def latest_onboarding_profile_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    profiles: list[dict[str, Any] | None] = []
    for row in rows:
        record = row.get("record")
        if isinstance(record, dict):
            profiles.append(_profile_from_text(str(record.get("content") or "")))
    return _latest_profile(profiles)


def soul_profile_context_from_hits(hits: list[MemoryHit]) -> str:
    profile = latest_onboarding_profile_from_hits(hits)
    if not profile:
        return ""
    lines = [
        "Use this validated Soul/user profile when choosing names, tone, and collaboration defaults.",
        f"- Kestrel instance name: {profile.get('agent_name')}",
    ]
    if profile.get("preferred_name") or profile.get("user_name"):
        lines.append(f"- User name: {profile.get('preferred_name') or profile.get('user_name')}")
    if profile.get("persona_name"):
        lines.append(f"- Selected persona: {profile.get('persona_name')}")
    if profile.get("persona_guidance"):
        lines.append(f"- Persona guidance: {profile.get('persona_guidance')}")
    if profile.get("working_style"):
        lines.append(f"- User working style: {profile.get('working_style')}")
    goals = profile.get("goals")
    if isinstance(goals, list) and goals:
        lines.append("- User goals: " + "; ".join(str(goal) for goal in goals[:6]))
    interests = profile.get("interests")
    if isinstance(interests, list) and interests:
        lines.append("- User interests: " + "; ".join(str(interest) for interest in interests[:6]))
    if profile.get("communication_notes"):
        lines.append(f"- Communication notes: {profile.get('communication_notes')}")
    if profile.get("continuous_learning"):
        lines.append("- Learning preference: continue adapting from validated user corrections and explicit remember requests.")
    return "\n".join(lines)


def soul_communication_contract_from_hits(hits: list[MemoryHit]) -> str:
    profile = latest_onboarding_profile_from_hits(hits) or {}
    persona = persona_by_id(str(profile.get("persona") or profile.get("persona_id") or DEFAULT_PERSONA_ID))
    lines = [
        "Active Communication Contract",
        f"- Persona: {persona['name']}. {persona['guidance']}",
        "- Default posture: warm, curious, practical, and direct without being clipped or dismissive.",
        "- Own mistakes without defensiveness; name the correction, explain the next move, and keep going.",
        "- Do not scold the user for vague wording, frustration, corrections, or changed priorities.",
        "- When intent is blurry, ask one focused question or make a clearly labeled assumption.",
        "- When the user is annoyed or blocked, acknowledge the friction before giving the fix.",
        "- Prefer concrete next steps and evidence over lectures, performative hype, or empty reassurance.",
    ]
    if profile.get("preferred_name") or profile.get("user_name"):
        lines.append(f"- Address the user as {profile.get('preferred_name') or profile.get('user_name')} when it feels natural.")
    if profile.get("working_style"):
        lines.append(f"- User working style: {profile.get('working_style')}")
    if profile.get("communication_notes"):
        lines.append(f"- User communication notes: {profile.get('communication_notes')}")
    return "\n".join(lines)


def _latest_profile(profiles: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    valid = [profile for profile in profiles if profile is not None]
    if not valid:
        return None
    return sorted(valid, key=lambda profile: str(profile.get("updated_at") or ""))[-1]


def _profile_from_text(text: str) -> dict[str, Any] | None:
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != ONBOARDING_SCHEMA_VERSION:
        return None
    return payload


def _clean_text(value: Any, *, fallback: str = "", max_chars: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        text = fallback
    return text[:max_chars].strip()


def _clean_list(value: Any, *, max_items: int, max_chars: int) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    cleaned = []
    for item in raw_items:
        text = _clean_text(item, max_chars=max_chars)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= max_items:
            break
    return cleaned
