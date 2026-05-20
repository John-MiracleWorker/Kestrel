from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .behavior_delta import BehaviorDelta, BehaviorDeltaKind


@dataclass(frozen=True)
class SkillCandidatePreview:
    delta_id: str
    manifest: dict[str, Any]
    instructions: str
    validation: dict[str, Any]
    installable: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "delta_id": self.delta_id,
            "installable": self.installable,
            "manifest": self.manifest,
            "instructions": self.instructions,
            "validation": self.validation,
        }


def render_skill_candidate_preview(delta: BehaviorDelta, *, skill_id: str | None = None) -> SkillCandidatePreview:
    """Render a non-installing SKILL.md preview from a skill-candidate delta.

    Behavior-delta skill integration is intentionally proposal-only in this
    phase: this helper produces a manifest and markdown preview, validates the
    manifest, and always leaves installation to the existing approval-gated
    skill install path.
    """

    if delta.kind != BehaviorDeltaKind.SKILL_CANDIDATE:
        raise ValueError("Behavior delta must be kind=skill_candidate to render a skill preview.")
    resolved_skill_id = skill_id or _slugify(delta.title or delta.id)
    manifest = {
        "id": resolved_skill_id,
        "name": delta.title,
        "description": _description(delta),
        "version": "0.1.0",
        "risk": _manifest_risk(delta),
        "runtime": {"type": "instruction"},
        "requires_approval": True,
        "permissions": [],
        "capabilities": ["skill", "behavior-delta", "instruction-only"],
        "provenance": {
            "behavior_delta_id": delta.id,
            "created_from_run_id": delta.created_from_run_id,
            "evidence": [ref.__dict__ for ref in delta.evidence_refs],
        },
    }
    instructions = _render_skill_md(delta, manifest)
    from .skill_validation import validate_skill_manifest

    return SkillCandidatePreview(
        delta_id=delta.id,
        manifest=manifest,
        instructions=instructions,
        validation=validate_skill_manifest(manifest),
        installable=False,
    )


def _description(delta: BehaviorDelta) -> str:
    hint = delta.trigger.semantic_hint or delta.behavior_change
    return f"Behavior-delta skill candidate from {delta.id}: {hint[:240]}"


def _manifest_risk(delta: BehaviorDelta) -> str:
    if delta.risk.value in {"high", "critical"}:
        return "high"
    if delta.risk.value == "medium":
        return "medium"
    return "low"


def _render_skill_md(delta: BehaviorDelta, manifest: dict[str, Any]) -> str:
    lines = [
        f"# {delta.title}",
        "",
        "## Trigger",
        _trigger_text(delta),
        "",
        "## Procedure",
        f"- {delta.behavior_change.strip()}",
        "- Keep this as an instruction-only workflow unless a human explicitly approves an executable implementation.",
        "",
        "## Verification",
        *_bullet_list(_verification_items(delta)),
        "",
        "## Pitfalls",
        *_bullet_list(_pitfall_items(delta)),
        "",
        "## Evidence",
        *_bullet_list(_evidence_items(delta)),
        "",
        "## Safety",
        "- Preview only: this skill candidate is not installed automatically.",
        "- Executable code was not generated from learning output.",
        "- Installation must use existing skill validation and approval gates.",
        "",
        "## Manifest Preview",
        f"- id: `{manifest['id']}`",
        f"- runtime: `{manifest['runtime']['type']}`",
        f"- behavior_delta_id: `{delta.id}`",
        "",
    ]
    return "\n".join(lines)


def _trigger_text(delta: BehaviorDelta) -> str:
    trigger = delta.trigger
    pieces: list[str] = []
    if trigger.semantic_hint:
        pieces.append(trigger.semantic_hint)
    if trigger.query_patterns:
        pieces.append("Query patterns: " + ", ".join(f"`{item}`" for item in trigger.query_patterns))
    if trigger.task_types:
        pieces.append("Task types: " + ", ".join(f"`{item}`" for item in trigger.task_types))
    if trigger.tool_names:
        pieces.append("Tools: " + ", ".join(f"`{item}`" for item in trigger.tool_names))
    if trigger.memory_layers:
        pieces.append("Memory layers: " + ", ".join(f"`{item.value}`" for item in trigger.memory_layers))
    return "\n".join(f"- {piece}" for piece in pieces) if pieces else "- Use when the delta trigger matches the current task."


def _verification_items(delta: BehaviorDelta) -> list[str]:
    metadata_items = _metadata_list(delta, "verification")
    items = list(metadata_items)
    items.extend(delta.validation_plan.required_checks)
    items.extend(f"Replay scenario: {scenario}" for scenario in delta.validation_plan.replay_scenarios)
    if delta.validation_plan.min_validation_score:
        items.append(f"Minimum validation score: {delta.validation_plan.min_validation_score:.2f}")
    if delta.validation_plan.min_repeat_count > 1:
        items.append(f"Minimum repeat count: {delta.validation_plan.min_repeat_count}")
    return items or ["Run the relevant task validation and record the outcome before relying on this skill."]


def _pitfall_items(delta: BehaviorDelta) -> list[str]:
    items = _metadata_list(delta, "pitfalls")
    if delta.metadata.get("skill_runtime"):
        items.append("Ignore any executable runtime metadata in the delta preview; generated executable skills require explicit approval.")
    return items or ["Do not install or execute generated code from this candidate without explicit approval."]


def _evidence_items(delta: BehaviorDelta) -> list[str]:
    return [
        f"{ref.source}:{ref.locator}" + (f" — {ref.quote}" if ref.quote else "")
        for ref in delta.evidence_refs
    ]


def _metadata_list(delta: BehaviorDelta, key: str) -> list[str]:
    raw = delta.metadata.get(key)
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list | tuple):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _bullet_list(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    slug = re.sub(r"[-_]{2,}", "-", slug)
    return slug or "behavior-delta-skill"
