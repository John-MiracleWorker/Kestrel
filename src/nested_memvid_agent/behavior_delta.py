from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .models import EvidenceRef, MemoryKind, MemoryLayer


class BehaviorDeltaKind(StrEnum):
    PROCEDURE = "procedure"
    POLICY = "policy"
    TOOL_HEURISTIC = "tool_heuristic"
    RETRIEVAL_PRIOR = "retrieval_prior"
    CONTEXT_PACKING_RULE = "context_packing_rule"
    APPROVAL_GATE_RULE = "approval_gate_rule"
    SKILL_CANDIDATE = "skill_candidate"
    CORRECTION_RULE = "correction_rule"
    SELF_MODEL_RULE = "self_model_rule"


class BehaviorDeltaStatus(StrEnum):
    PROPOSED = "proposed"
    STAGED = "staged"
    ACTIVE = "active"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


class BehaviorDeltaRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class TriggerSpec:
    query_patterns: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    memory_layers: tuple[MemoryLayer, ...] = ()
    path_globs: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    semantic_hint: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "query_patterns": list(self.query_patterns),
            "task_types": list(self.task_types),
            "tool_names": list(self.tool_names),
            "memory_layers": [layer.value for layer in self.memory_layers],
            "path_globs": list(self.path_globs),
            "risk_tags": list(self.risk_tags),
            "semantic_hint": self.semantic_hint,
        }

    @classmethod
    def from_metadata(cls, payload: dict[str, Any] | None) -> TriggerSpec:
        payload = payload or {}
        return cls(
            query_patterns=_string_tuple(payload.get("query_patterns")),
            task_types=_string_tuple(payload.get("task_types")),
            tool_names=_string_tuple(payload.get("tool_names")),
            memory_layers=tuple(MemoryLayer(item) for item in payload.get("memory_layers", ()) or ()),
            path_globs=_string_tuple(payload.get("path_globs")),
            risk_tags=_string_tuple(payload.get("risk_tags")),
            semantic_hint=_optional_str(payload.get("semantic_hint")),
        )


@dataclass(frozen=True)
class ValidationPlan:
    required_checks: tuple[str, ...] = ()
    replay_scenarios: tuple[str, ...] = ()
    requires_human_approval: bool = False
    requires_exact_call_approval: bool = False
    min_validation_score: float = 0.0
    min_repeat_count: int = 1

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_validation_score <= 1.0:
            raise ValueError("min_validation_score must be between 0.0 and 1.0")
        if self.min_repeat_count < 0:
            raise ValueError("min_repeat_count must be >= 0")

    def to_metadata(self) -> dict[str, Any]:
        return {
            "required_checks": list(self.required_checks),
            "replay_scenarios": list(self.replay_scenarios),
            "requires_human_approval": self.requires_human_approval,
            "requires_exact_call_approval": self.requires_exact_call_approval,
            "min_validation_score": self.min_validation_score,
            "min_repeat_count": self.min_repeat_count,
        }

    @classmethod
    def from_metadata(cls, payload: dict[str, Any] | None) -> ValidationPlan:
        payload = payload or {}
        return cls(
            required_checks=_string_tuple(payload.get("required_checks")),
            replay_scenarios=_string_tuple(payload.get("replay_scenarios")),
            requires_human_approval=bool(payload.get("requires_human_approval", False)),
            requires_exact_call_approval=bool(payload.get("requires_exact_call_approval", False)),
            min_validation_score=float(payload.get("min_validation_score", 0.0)),
            min_repeat_count=int(payload.get("min_repeat_count", 1)),
        )


@dataclass(frozen=True)
class RollbackPlan:
    can_disable: bool = True
    rollback_notes: str = "Disable this delta and remove it from active compilation."
    tombstone_memory_record_id: str | None = None
    restore_delta_id: str | None = None

    def __post_init__(self) -> None:
        if not self.rollback_notes.strip():
            raise ValueError("rollback_notes cannot be empty")

    def to_metadata(self) -> dict[str, Any]:
        return {
            "can_disable": self.can_disable,
            "rollback_notes": self.rollback_notes,
            "tombstone_memory_record_id": self.tombstone_memory_record_id,
            "restore_delta_id": self.restore_delta_id,
        }

    @classmethod
    def from_metadata(cls, payload: dict[str, Any] | None) -> RollbackPlan:
        payload = payload or {}
        return cls(
            can_disable=bool(payload.get("can_disable", True)),
            rollback_notes=str(
                payload.get("rollback_notes", "Disable this delta and remove it from active compilation.")
            ),
            tombstone_memory_record_id=_optional_str(payload.get("tombstone_memory_record_id")),
            restore_delta_id=_optional_str(payload.get("restore_delta_id")),
        )


@dataclass(frozen=True)
class ActivationStats:
    activation_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    correction_count: int = 0
    last_activated_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("activation_count", "success_count", "failure_count", "correction_count"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")

    def to_metadata(self) -> dict[str, Any]:
        return {
            "activation_count": self.activation_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "correction_count": self.correction_count,
            "last_activated_at": self.last_activated_at,
        }

    @classmethod
    def from_metadata(cls, payload: dict[str, Any] | None) -> ActivationStats:
        payload = payload or {}
        return cls(
            activation_count=int(payload.get("activation_count", 0)),
            success_count=int(payload.get("success_count", 0)),
            failure_count=int(payload.get("failure_count", 0)),
            correction_count=int(payload.get("correction_count", 0)),
            last_activated_at=_optional_str(payload.get("last_activated_at")),
        )


@dataclass(frozen=True)
class BehaviorDelta:
    trigger: TriggerSpec
    behavior_change: str
    kind: BehaviorDeltaKind
    target_layer: MemoryLayer
    evidence_refs: tuple[EvidenceRef, ...]
    risk: BehaviorDeltaRisk
    validation_plan: ValidationPlan
    rollback_plan: RollbackPlan = field(default_factory=RollbackPlan)
    status: BehaviorDeltaStatus = BehaviorDeltaStatus.PROPOSED
    activation_stats: ActivationStats = field(default_factory=ActivationStats)
    confidence: float = 0.6
    importance: float = 0.5
    id: str = field(default_factory=lambda: f"delta_{uuid4().hex}")
    title: str = "Behavior delta"
    created_from_run_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("id cannot be empty")
        if not self.title.strip():
            raise ValueError("title cannot be empty")
        if not self.behavior_change.strip():
            raise ValueError("behavior_change cannot be empty")
        if not self.evidence_refs and not (
            self.status == BehaviorDeltaStatus.PROPOSED and self.metadata.get("draft") is True
        ):
            raise ValueError("evidence_refs cannot be empty unless the proposed delta is marked as a draft")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        if not 0.0 <= self.importance <= 1.0:
            raise ValueError("importance must be between 0.0 and 1.0")

    def to_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind.value,
            "target_layer": self.target_layer.value,
            "risk": self.risk.value,
            "status": self.status.value,
            "trigger": self.trigger.to_metadata(),
            "behavior_change": self.behavior_change,
            "validation_plan": self.validation_plan.to_metadata(),
            "rollback_plan": self.rollback_plan.to_metadata(),
            "activation_stats": self.activation_stats.to_metadata(),
            "confidence": self.confidence,
            "importance": self.importance,
            "created_from_run_id": self.created_from_run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "evidence": [_evidence_to_metadata(ref) for ref in self.evidence_refs],
            **self.metadata,
        }


def behavior_delta_to_memory_metadata(delta: BehaviorDelta) -> dict[str, Any]:
    """Wrap a behavior delta for storage under MemoryRecord.metadata."""

    return {"behavior_delta": delta.to_metadata()}


def behavior_delta_from_metadata(payload: dict[str, Any]) -> BehaviorDelta:
    """Rehydrate a BehaviorDelta from a metadata payload.

    Accepts either the direct behavior-delta metadata dictionary or a
    MemoryRecord-style wrapper containing a top-level ``behavior_delta`` key.
    """

    if "behavior_delta" in payload and isinstance(payload["behavior_delta"], dict):
        payload = payload["behavior_delta"]
    known_keys = {
        "id",
        "title",
        "kind",
        "target_layer",
        "risk",
        "status",
        "trigger",
        "behavior_change",
        "validation_plan",
        "rollback_plan",
        "activation_stats",
        "confidence",
        "importance",
        "created_from_run_id",
        "created_at",
        "updated_at",
        "expires_at",
        "evidence",
    }
    metadata = {key: value for key, value in payload.items() if key not in known_keys}
    return BehaviorDelta(
        id=str(payload["id"]),
        title=str(payload.get("title", "Behavior delta")),
        kind=BehaviorDeltaKind(str(payload["kind"])),
        target_layer=MemoryLayer(str(payload["target_layer"])),
        risk=BehaviorDeltaRisk(str(payload["risk"])),
        status=BehaviorDeltaStatus(str(payload.get("status", BehaviorDeltaStatus.PROPOSED.value))),
        trigger=TriggerSpec.from_metadata(_dict_or_empty(payload.get("trigger"))),
        behavior_change=str(payload["behavior_change"]),
        validation_plan=ValidationPlan.from_metadata(_dict_or_empty(payload.get("validation_plan"))),
        rollback_plan=RollbackPlan.from_metadata(_dict_or_empty(payload.get("rollback_plan"))),
        activation_stats=ActivationStats.from_metadata(_dict_or_empty(payload.get("activation_stats"))),
        confidence=float(payload.get("confidence", 0.6)),
        importance=float(payload.get("importance", 0.5)),
        created_from_run_id=_optional_str(payload.get("created_from_run_id")),
        created_at=str(payload.get("created_at", datetime.now(UTC).isoformat())),
        updated_at=str(payload.get("updated_at", datetime.now(UTC).isoformat())),
        expires_at=_optional_str(payload.get("expires_at")),
        evidence_refs=tuple(_evidence_from_metadata(item) for item in payload.get("evidence", ()) or ()),
        metadata=metadata,
    )


def memory_kind_for_behavior_delta(delta: BehaviorDelta) -> MemoryKind:
    if delta.kind in {BehaviorDeltaKind.POLICY, BehaviorDeltaKind.APPROVAL_GATE_RULE}:
        return MemoryKind.POLICY
    if delta.kind in {
        BehaviorDeltaKind.PROCEDURE,
        BehaviorDeltaKind.TOOL_HEURISTIC,
        BehaviorDeltaKind.CONTEXT_PACKING_RULE,
        BehaviorDeltaKind.RETRIEVAL_PRIOR,
        BehaviorDeltaKind.SKILL_CANDIDATE,
    }:
        return MemoryKind.PROCEDURE
    if delta.kind == BehaviorDeltaKind.CORRECTION_RULE:
        return MemoryKind.CORRECTION
    if delta.kind == BehaviorDeltaKind.SELF_MODEL_RULE:
        return MemoryKind.FACT
    if delta.target_layer == MemoryLayer.POLICY:
        return MemoryKind.POLICY
    return MemoryKind.PROCEDURE


def _evidence_to_metadata(ref: EvidenceRef) -> dict[str, Any]:
    return {"source": ref.source, "locator": ref.locator, "quote": ref.quote}


def _evidence_from_metadata(payload: Any) -> EvidenceRef:
    if not isinstance(payload, dict):
        raise TypeError("Evidence metadata must be a dictionary")
    return EvidenceRef(
        source=str(payload["source"]),
        locator=str(payload["locator"]),
        quote=_optional_str(payload.get("quote")),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(item) for item in value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
