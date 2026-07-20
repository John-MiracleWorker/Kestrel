from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .layers import DEFAULT_LAYER_SPECS, LayerSpec
from .models import MemoryHit, MemoryKind, MemoryLayer

ONBOARDING_SCHEMA_VERSION = "kestrel_onboarding_profile.v1"
SELF_PROFILE_SCHEMA = "user_profile"
SELF_PROFILE_QUERY = (
    "kestrel_onboarding_profile user_profile agent_name user_name preferred_name "
    "persona persona_id working_style goals interests communication_notes"
)
DEFAULT_PERSONA_ID = "steady"
TRUSTED_ONBOARDING_ORIGIN = "kestrel.web.onboarding"
TRUSTED_ONBOARDING_SOURCE = "web.onboarding_wizard"
TRUSTED_ONBOARDING_LOCATOR = "api://self/onboarding"
TRUSTED_ONBOARDING_PROVENANCE_SCHEMA = "kestrel.onboarding_provenance.v1"

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
    persona = persona_by_id(
        str(payload.get("persona") or payload.get("persona_id") or DEFAULT_PERSONA_ID)
    )
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
    return next(
        (persona for persona in PERSONA_PRESETS if persona["id"] == normalized), PERSONA_PRESETS[0]
    )


def onboarding_record_title(profile: dict[str, Any]) -> str:
    agent_name = str(profile.get("agent_name") or "Kestrel")
    user_name = str(profile.get("preferred_name") or profile.get("user_name") or "the user")
    return f"Kestrel onboarding profile: {agent_name} for {user_name}"


def onboarding_record_content(profile: dict[str, Any]) -> str:
    return json.dumps(profile, indent=2, sort_keys=True)


def onboarding_state_from_reflection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    profile = trusted_onboarding_profile_from_rows(rows)
    return {
        "completed": bool(profile),
        "profile": profile,
        "personas": persona_presets_public(),
    }


def trusted_onboarding_profile_from_hits(
    hits: list[MemoryHit],
    *,
    spec: LayerSpec | None = None,
) -> dict[str, Any] | None:
    self_spec = spec or DEFAULT_LAYER_SPECS[MemoryLayer.SELF]
    candidates = [
        (_profile_from_text(hit.record.content), hit.record.id)
        for hit in hits
        if _is_trusted_onboarding_hit(hit, spec=self_spec)
    ]
    return _latest_profile_candidate(candidates)


def trusted_onboarding_profile_from_rows(
    rows: list[dict[str, Any]],
    *,
    spec: LayerSpec | None = None,
) -> dict[str, Any] | None:
    """Select the latest authenticated onboarding record from reflection output.

    Reflection rows are an API serialization boundary, so schema-valid content alone
    is never enough to mark onboarding complete. Ties use the durable record ID to
    keep repeated reads deterministic regardless of retrieval order.
    """

    self_spec = spec or DEFAULT_LAYER_SPECS[MemoryLayer.SELF]
    candidates: list[tuple[dict[str, Any] | None, str]] = []
    for row in rows:
        record = row.get("record")
        if not isinstance(record, dict) or not _is_trusted_onboarding_row(record, spec=self_spec):
            continue
        candidates.append(
            (
                _profile_from_text(str(record.get("content") or "")),
                str(record.get("id") or row.get("frame_id") or ""),
            )
        )
    return _latest_profile_candidate(candidates)


def trusted_onboarding_record_ids(
    hits: list[MemoryHit],
    *,
    spec: LayerSpec | None = None,
) -> frozenset[str]:
    self_spec = spec or DEFAULT_LAYER_SPECS[MemoryLayer.SELF]
    record_ids: set[str] = set()
    for hit in hits:
        if not _is_trusted_onboarding_hit(hit, spec=self_spec):
            continue
        record_ids.add(hit.record.id)
        record_ids.add(str(hit.record.metadata.get("frame_id") or hit.record.id))
    return frozenset(record_ids)


def trusted_onboarding_record_count(
    hits: list[MemoryHit],
    *,
    spec: LayerSpec | None = None,
) -> int:
    self_spec = spec or DEFAULT_LAYER_SPECS[MemoryLayer.SELF]
    return sum(_is_trusted_onboarding_hit(hit, spec=self_spec) for hit in hits)


def soul_profile_context_from_hits(
    hits: list[MemoryHit],
    *,
    spec: LayerSpec | None = None,
) -> str:
    profile = trusted_onboarding_profile_from_hits(hits, spec=spec)
    if not profile:
        return ""
    persona = persona_by_id(str(profile.get("persona") or DEFAULT_PERSONA_ID))
    identity: dict[str, str] = {
        "persona_id": persona["id"],
        "persona_name": persona["name"],
    }
    return (
        "Use only the fixed persona preset selected in this authenticated onboarding JSON.\n"
        + json.dumps({"trusted_onboarding_identity": identity}, ensure_ascii=False, sort_keys=True)
    )


def soul_communication_contract_from_hits(
    hits: list[MemoryHit],
    *,
    spec: LayerSpec | None = None,
) -> str:
    profile = trusted_onboarding_profile_from_hits(hits, spec=spec) or {}
    persona = persona_by_id(
        str(profile.get("persona") or profile.get("persona_id") or DEFAULT_PERSONA_ID)
    )
    lines = [
        "Active Communication Contract",
        f"- Persona: {persona['name']}. {persona['guidance']}",
        "- Default posture: warm, curious, practical, and direct without being clipped, dismissive, transactional, or emotionally absent.",
        "- For simple greetings, mirror the user's casual energy before steering: be a person in the room, not a ticket intake form.",
        "- Avoid flat acknowledgments like 'I'm here. What do you want to work on first?' unless the user explicitly asks for sterile brevity.",
        "- A good greeting should feel relaxed and companionable without assuming the user's name.",
        "- Own mistakes without defensiveness; name the correction, explain the next move, and keep going.",
        "- Do not scold the user for vague wording, frustration, corrections, or changed priorities.",
        "- When intent is blurry, ask one focused question or make a clearly labeled assumption.",
        "- When the user is annoyed or blocked, acknowledge the friction before giving the fix.",
        "- Prefer concrete next steps and evidence over lectures, performative hype, or empty reassurance.",
    ]
    return "\n".join(lines)


def soul_untrusted_preferences_from_hits(
    hits: list[MemoryHit],
    *,
    spec: LayerSpec | None = None,
) -> str:
    """Return free-form onboarding fields as bounded, explicitly untrusted JSON."""

    profile = trusted_onboarding_profile_from_hits(hits, spec=spec)
    if not profile:
        return ""
    preferences = {
        "display_labels": {
            "agent_name": _safe_display_name(profile.get("agent_name"), fallback="Kestrel"),
            "preferred_name": _safe_display_name(
                profile.get("preferred_name") or profile.get("user_name")
            ),
        },
        "working_style": _clean_text(profile.get("working_style"), max_chars=600),
        "goals": _clean_list(profile.get("goals"), max_items=6, max_chars=120),
        "interests": _clean_list(profile.get("interests"), max_items=6, max_chars=120),
        "communication_notes": _clean_text(profile.get("communication_notes"), max_chars=700),
        "continuous_learning": bool(profile.get("continuous_learning", True)),
    }
    return json.dumps(
        {"untrusted_onboarding_preferences": preferences},
        ensure_ascii=False,
        sort_keys=True,
    )


def _is_trusted_onboarding_hit(hit: MemoryHit, *, spec: LayerSpec) -> bool:
    record = hit.record
    return _trusted_onboarding_fields(
        layer=record.layer.value,
        kind=record.kind.value,
        confidence=record.confidence,
        metadata=record.metadata,
        evidence=record.evidence,
        spec=spec,
    )


def _is_trusted_onboarding_row(record: dict[str, Any], *, spec: LayerSpec) -> bool:
    evidence = record.get("evidence")
    return _trusted_onboarding_fields(
        layer=str(record.get("layer") or ""),
        kind=str(record.get("kind") or ""),
        confidence=record.get("confidence"),
        metadata=record.get("metadata"),
        evidence=evidence if isinstance(evidence, list) else [],
        spec=spec,
    )


def _trusted_onboarding_fields(
    *,
    layer: str,
    kind: str,
    confidence: object,
    metadata: object,
    evidence: list[Any],
    spec: LayerSpec,
) -> bool:
    if not isinstance(metadata, dict):
        return False
    nested = metadata.get("nested_learning")
    decision = nested.get("decision") if isinstance(nested, dict) else None
    provenance = metadata.get("onboarding_provenance")
    validation_evidence = metadata.get("validation_evidence")
    stable_envelope = metadata.get("stable_write_envelope")
    score = metadata.get("validation_score")
    if (
        layer != MemoryLayer.SELF.value
        or kind != MemoryKind.FACT.value
        or not isinstance(confidence, int | float)
        or isinstance(confidence, bool)
        or float(confidence) < spec.min_write_confidence
        or metadata.get("self_schema") != SELF_PROFILE_SCHEMA
        or metadata.get("validation_status") != "user_confirmed"
        or metadata.get("promotion_status") != "confirmed"
        or metadata.get("validation_method") != "nested_learning_kernel"
        or metadata.get("explicit_instruction") is not True
        or not isinstance(score, int | float)
        or isinstance(score, bool)
        or float(score) < spec.promotion_threshold
        or not isinstance(decision, dict)
        or decision.get("accepted") is not True
        or decision.get("action") != "write"
        or decision.get("target_layer") != MemoryLayer.SELF.value
        or not isinstance(validation_evidence, dict)
        or validation_evidence.get("legacy_raw_score") is not False
        or validation_evidence.get("resolved") is not True
        or validation_evidence.get("validation_status") != "human_confirmed"
        or not isinstance(stable_envelope, dict)
        or stable_envelope.get("authority") != "nested_learning"
        or stable_envelope.get("evidence_resolved") is not True
        or stable_envelope.get("target_layer") != MemoryLayer.SELF.value
        or not isinstance(provenance, dict)
        or provenance.get("schema") != TRUSTED_ONBOARDING_PROVENANCE_SCHEMA
        or provenance.get("origin") != TRUSTED_ONBOARDING_ORIGIN
        or provenance.get("source") != TRUSTED_ONBOARDING_SOURCE
        or provenance.get("locator") != TRUSTED_ONBOARDING_LOCATOR
    ):
        return False
    for ref in evidence:
        if isinstance(ref, dict):
            source = ref.get("source")
            locator = ref.get("locator")
        else:
            source = getattr(ref, "source", None)
            locator = getattr(ref, "locator", None)
        if source == TRUSTED_ONBOARDING_SOURCE and locator == TRUSTED_ONBOARDING_LOCATOR:
            return True
    return False


def _latest_profile_candidate(
    candidates: list[tuple[dict[str, Any] | None, str]],
) -> dict[str, Any] | None:
    valid = [(profile, record_id) for profile, record_id in candidates if profile is not None]
    if not valid:
        return None
    profile, _ = max(
        valid,
        key=lambda candidate: (
            str(candidate[0].get("updated_at") or ""),
            candidate[1],
        ),
    )
    return profile


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


def _safe_display_name(value: Any, *, fallback: str = "") -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        text = fallback
    text = text[:80].strip()
    if not text or len(text.split()) > 8:
        return fallback
    if any(not (character.isalnum() or character in " ._'-") for character in text):
        return fallback
    return text


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
