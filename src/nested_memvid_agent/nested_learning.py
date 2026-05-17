from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal

from .context_frames import default_frame_type_for_memory
from .layers import DEFAULT_LAYER_SPECS, LayerSpec
from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord

LearningAction = Literal["reject", "write", "promote"]


@dataclass(frozen=True)
class ContextFlow:
    """A named nested-learning loop with its own update cadence and objective."""

    id: str
    level: int
    update_frequency: str
    source_layers: tuple[MemoryLayer, ...]
    target_layer: MemoryLayer
    objective: str
    compression: str
    retention: str

    def to_metadata(self) -> dict[str, object]:
        return {
            "id": self.id,
            "level": self.level,
            "update_frequency": self.update_frequency,
            "source_layers": [layer.value for layer in self.source_layers],
            "target_layer": self.target_layer.value,
            "objective": self.objective,
            "compression": self.compression,
            "retention": self.retention,
        }


@dataclass(frozen=True)
class OptimizerTrace:
    """Associative-memory trace for a memory update decision."""

    surprise: float
    validation_score: float
    repeat_count: int
    compression_ratio: float | None
    confidence_delta: float
    effective_confidence: float
    confidence_delta_kind: str = "expected"

    def to_metadata(self) -> dict[str, object]:
        return {
            "surprise": round(self.surprise, 4),
            "validation_score": round(self.validation_score, 4),
            "repeat_count": self.repeat_count,
            "compression_ratio": None if self.compression_ratio is None else round(self.compression_ratio, 4),
            "confidence_delta": round(self.confidence_delta, 4),
            "confidence_delta_kind": self.confidence_delta_kind,
            "effective_confidence": round(self.effective_confidence, 4),
        }


@dataclass(frozen=True)
class ValidationEvidence:
    """Structured validation evidence used to compute promotion gates."""

    test_refs: tuple[EvidenceRef, ...] = ()
    lint_refs: tuple[EvidenceRef, ...] = ()
    repair_refs: tuple[EvidenceRef, ...] = ()
    review_refs: tuple[EvidenceRef, ...] = ()
    task_refs: tuple[EvidenceRef, ...] = ()
    human_explicit: bool = False
    legacy_raw_score: bool = False
    source_evidence_chars: int | None = None

    def to_metadata(self) -> dict[str, object]:
        return {
            "test_refs": _ref_labels(self.test_refs),
            "lint_refs": _ref_labels(self.lint_refs),
            "repair_refs": _ref_labels(self.repair_refs),
            "review_refs": _ref_labels(self.review_refs),
            "task_refs": _ref_labels(self.task_refs),
            "human_explicit": self.human_explicit,
            "legacy_raw_score": self.legacy_raw_score,
            "source_evidence_chars": self.source_evidence_chars,
            "computed_score": compute_validation_score(self),
        }

    def all_refs(self) -> tuple[EvidenceRef, ...]:
        return self.test_refs + self.lint_refs + self.repair_refs + self.review_refs + self.task_refs


def compute_validation_score(evidence: ValidationEvidence) -> float:
    objective_buckets = (
        evidence.test_refs,
        evidence.lint_refs,
        evidence.repair_refs,
        evidence.review_refs,
    )
    objective_score = sum(1 for refs in objective_buckets if refs) / len(objective_buckets)
    human_bonus = 0.05 if evidence.human_explicit and objective_score > 0 else 0.0
    return round(min(objective_score + human_bonus, 1.0), 4)


@dataclass(frozen=True)
class LearningSignal:
    title: str
    content: str
    kind: MemoryKind
    source_layer: MemoryLayer
    confidence: float = 0.6
    importance: float = 0.5
    validation_score: float | None = 0.7
    validation_evidence: ValidationEvidence | None = None
    repeat_count: int = 1
    explicit_instruction: bool = False
    source: str = "learning_signal"
    locator: str = "manual"
    tags: dict[str, str] | None = None
    metadata: dict[str, Any] | None = None
    requested_target_layer: MemoryLayer | None = None
    source_evidence_chars: int | None = None

    @property
    def computed_validation_score(self) -> float:
        if self.validation_evidence is not None:
            return compute_validation_score(self.validation_evidence)
        return float(self.validation_score if self.validation_score is not None else 0.0)

    @property
    def effective_source_evidence_chars(self) -> int | None:
        if self.source_evidence_chars is not None:
            return self.source_evidence_chars
        if self.validation_evidence is not None:
            return self.validation_evidence.source_evidence_chars
        return None


@dataclass(frozen=True)
class LearningDecision:
    action: LearningAction
    target_layer: MemoryLayer | None
    target_kind: MemoryKind
    reason: str
    confidence: float
    importance: float
    flow: ContextFlow
    optimizer_trace: OptimizerTrace
    promotion_requirements: dict[str, object]

    @property
    def accepted(self) -> bool:
        return self.action in {"write", "promote"} and self.target_layer is not None

    def to_payload(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "action": self.action,
            "target_layer": None if self.target_layer is None else self.target_layer.value,
            "target_kind": self.target_kind.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "importance": self.importance,
            "context_flow": self.flow.to_metadata(),
            "optimizer_trace": self.optimizer_trace.to_metadata(),
            "promotion_requirements": self.promotion_requirements,
        }


DEFAULT_CONTEXT_FLOWS: dict[str, ContextFlow] = {
    "interaction_to_working": ContextFlow(
        id="interaction_to_working",
        level=1,
        update_frequency="per-step",
        source_layers=(MemoryLayer.WORKING,),
        target_layer=MemoryLayer.WORKING,
        objective="Capture volatile task state and current surprise signals.",
        compression="Raw interaction -> compact active-state observation.",
        retention="short",
    ),
    "working_to_episode": ContextFlow(
        id="working_to_episode",
        level=2,
        update_frequency="per-validated-event",
        source_layers=(MemoryLayer.WORKING,),
        target_layer=MemoryLayer.EPISODIC,
        objective="Compress validated working context into an auditable event.",
        compression="Working-memory evidence -> event summary.",
        retention="session/history",
    ),
    "episode_to_semantic": ContextFlow(
        id="episode_to_semantic",
        level=3,
        update_frequency="after-validation",
        source_layers=(MemoryLayer.EPISODIC,),
        target_layer=MemoryLayer.SEMANTIC,
        objective="Compress repeated or validated episodes into stable facts.",
        compression="Events -> stable project/user fact.",
        retention="long",
    ),
    "episode_to_procedural": ContextFlow(
        id="episode_to_procedural",
        level=4,
        update_frequency="after-repeated-validation",
        source_layers=(MemoryLayer.EPISODIC, MemoryLayer.PROCEDURAL),
        target_layer=MemoryLayer.PROCEDURAL,
        objective="Compress repeated outcomes into reusable recipes.",
        compression="Validated failures/successes -> procedure.",
        retention="long",
    ),
    "episode_to_self": ContextFlow(
        id="episode_to_self",
        level=4,
        update_frequency="after-self-validation",
        source_layers=(MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC, MemoryLayer.PROCEDURAL),
        target_layer=MemoryLayer.SELF,
        objective="Compress validated identity, capability, preference, and self-change evidence into self memory.",
        compression="Auditable evidence -> bounded self-model record.",
        retention="long",
    ),
    "procedure_to_policy": ContextFlow(
        id="procedure_to_policy",
        level=5,
        update_frequency="rare-reviewed",
        source_layers=(MemoryLayer.PROCEDURAL,),
        target_layer=MemoryLayer.POLICY,
        objective="Promote only durable, explicit constraints into policy.",
        compression="Validated procedure or instruction -> behavior rule.",
        retention="very-long/manual-review",
    ),
}


class NestedLearningKernel:
    """Conservative nested-learning decision rules for continuum memory updates."""

    def __init__(
        self,
        *,
        specs: dict[MemoryLayer, LayerSpec] | None = None,
        memory: Any | None = None,
    ) -> None:
        self.specs = specs or DEFAULT_LAYER_SPECS
        self.memory = memory

    def decide(self, signal: LearningSignal, *, action: LearningAction = "write") -> LearningDecision:
        target_layer, reason = self._target(signal)
        requested_or_target = signal.requested_target_layer or target_layer
        flow = _flow_for(signal.source_layer, target_layer)
        trace = self._optimizer_trace(signal, target_layer)
        requirements = self._promotion_requirements(signal, requested_or_target)
        if target_layer is None:
            return LearningDecision(
                action="reject",
                target_layer=None,
                target_kind=signal.kind,
                reason=reason,
                confidence=trace.effective_confidence,
                importance=signal.importance,
                flow=flow,
                optimizer_trace=trace,
                promotion_requirements=requirements,
            )
        return LearningDecision(
            action=action,
            target_layer=target_layer,
            target_kind=_target_kind(signal.kind, target_layer),
            reason=reason,
            confidence=trace.effective_confidence,
            importance=max(signal.importance, 0.65 if target_layer != MemoryLayer.WORKING else signal.importance),
            flow=flow,
            optimizer_trace=trace,
            promotion_requirements=requirements,
        )

    def from_record(
        self,
        record: MemoryRecord,
        *,
        validation_score: float,
        repeat_count: int = 1,
        explicit_instruction: bool = False,
    ) -> LearningDecision:
        signal = LearningSignal(
            title=record.title,
            content=record.content,
            kind=record.kind,
            source_layer=record.layer,
            confidence=record.confidence,
            importance=record.importance,
            validation_score=validation_score,
            validation_evidence=ValidationEvidence(legacy_raw_score=True) if validation_score is None else None,
            repeat_count=repeat_count,
            explicit_instruction=explicit_instruction,
            source="consolidator",
            locator=record.id,
            tags=record.tags,
            metadata=record.metadata,
        )
        return self.decide(signal, action="promote")

    def to_memory_record(self, signal: LearningSignal, decision: LearningDecision) -> MemoryRecord:
        target_layer = decision.target_layer
        if target_layer is None:
            raise ValueError("Cannot create memory record from rejected learning decision")
        evidence = [EvidenceRef(source=signal.source, locator=signal.locator, quote=decision.reason)]
        if signal.validation_evidence is not None:
            evidence.extend(signal.validation_evidence.all_refs())
            validation_evidence_metadata = signal.validation_evidence.to_metadata()
        else:
            validation_evidence_metadata = {
                "legacy_raw_score": True,
                "computed_score": signal.computed_validation_score,
                "test_refs": [],
                "lint_refs": [],
                "repair_refs": [],
                "review_refs": [],
                "task_refs": [],
                "human_explicit": signal.explicit_instruction,
                "source_evidence_chars": signal.source_evidence_chars,
            }
        metadata = {
            **(signal.metadata or {}),
            "frame_type": default_frame_type_for_memory(decision.target_kind, target_layer),
            "nested_learning": {
                "context_flow": decision.flow.to_metadata(),
                "optimizer_trace": decision.optimizer_trace.to_metadata(),
                "decision": decision.to_payload(),
            },
            "validation_method": "nested_learning_kernel",
            "validation_score": signal.computed_validation_score,
            "validation_evidence": validation_evidence_metadata,
            "repeat_count": signal.repeat_count,
            "explicit_instruction": signal.explicit_instruction,
        }
        return MemoryRecord(
            title=signal.title,
            content=signal.content,
            layer=target_layer,
            kind=decision.target_kind,
            tags=dict(signal.tags or {}),
            metadata=metadata,
            evidence=evidence,
            confidence=decision.confidence,
            importance=decision.importance,
        )

    def _target(self, signal: LearningSignal) -> tuple[MemoryLayer | None, str]:
        score = signal.computed_validation_score
        episodic_spec = self.specs[MemoryLayer.EPISODIC]
        if score < episodic_spec.promotion_threshold:
            return None, "Rejected: validation score is below the working-to-episodic gate."

        if signal.requested_target_layer is not None:
            allowed, reason = self._requested_target_allowed(signal, signal.requested_target_layer)
            return (signal.requested_target_layer, reason) if allowed else (None, reason)

        if signal.source_layer == MemoryLayer.WORKING:
            return MemoryLayer.EPISODIC, "Working context survived validation and became an episodic event."

        if signal.source_layer == MemoryLayer.EPISODIC:
            if signal.kind == MemoryKind.FACT and str((signal.metadata or {}).get("self_schema", "")).strip():
                if score >= self.specs[MemoryLayer.SELF].promotion_threshold:
                    return MemoryLayer.SELF, "Validated self-model signal became self memory."
            procedural_spec = self.specs[MemoryLayer.PROCEDURAL]
            if (
                signal.kind in {MemoryKind.FAILURE, MemoryKind.PROCEDURE}
                and signal.repeat_count >= procedural_spec.min_repeat_count_for_promotion
                and score >= procedural_spec.promotion_threshold
            ):
                return MemoryLayer.PROCEDURAL, "Repeated validated outcome became a reusable procedure."
            if score >= self.specs[MemoryLayer.SEMANTIC].promotion_threshold:
                return MemoryLayer.SEMANTIC, "Validated episode became stable semantic memory."
            return None, "Rejected: episodic signal did not clear semantic/procedural gates."

        if signal.source_layer == MemoryLayer.PROCEDURAL:
            policy_spec = self.specs[MemoryLayer.POLICY]
            if (
                score >= policy_spec.promotion_threshold
                and signal.repeat_count >= policy_spec.min_repeat_count_for_promotion
                and signal.explicit_instruction
            ):
                return MemoryLayer.POLICY, "Explicit repeated procedure cleared the policy-candidate gate."
            return None, "Rejected: policy promotion requires explicit instruction, high validation, and repeated evidence."

        if signal.source_layer == MemoryLayer.SEMANTIC:
            procedural_spec = self.specs[MemoryLayer.PROCEDURAL]
            if (
                signal.kind == MemoryKind.PROCEDURE
                and signal.repeat_count >= procedural_spec.min_repeat_count_for_promotion
                and score >= procedural_spec.promotion_threshold
            ):
                return MemoryLayer.PROCEDURAL, "Semantic procedure cleared repeated-use gate."
            return None, "Rejected: semantic memory is already stable and needs correction, not promotion."

        if signal.source_layer == MemoryLayer.SELF:
            return None, "Rejected: self memory is already part of the self-model and needs correction, not promotion."

        return None, "Rejected: policy memory cannot self-promote."

    def _requested_target_allowed(self, signal: LearningSignal, target: MemoryLayer) -> tuple[bool, str]:
        spec = self.specs[target]
        score = signal.computed_validation_score
        if target == MemoryLayer.POLICY:
            ok = (
                signal.explicit_instruction
                and score >= spec.promotion_threshold
                and signal.repeat_count >= spec.min_repeat_count_for_promotion
            )
            return (
                ok,
                "Explicit repeated signal requested policy memory and cleared the policy gate."
                if ok
                else (
                    "Rejected: requested policy writes require explicit instruction, "
                    f"validation >= {spec.promotion_threshold:.2f}, and repeat_count >= {spec.min_repeat_count_for_promotion}."
                ),
            )
        if target == MemoryLayer.PROCEDURAL:
            ok = score >= spec.promotion_threshold and signal.repeat_count >= spec.min_repeat_count_for_promotion
            return (
                ok,
                "Requested procedural memory cleared repeated validation gate."
                if ok
                else (
                    "Rejected: procedural writes require "
                    f"validation >= {spec.promotion_threshold:.2f} and repeat_count >= {spec.min_repeat_count_for_promotion}."
                ),
            )
        if target in {MemoryLayer.SELF, MemoryLayer.SEMANTIC, MemoryLayer.EPISODIC}:
            ok = score >= spec.promotion_threshold and signal.repeat_count >= spec.min_repeat_count_for_promotion
            return (
                ok,
                f"Requested {target.value} memory cleared validation gate."
                if ok
                else f"Rejected: {target.value} writes require validation >= {spec.promotion_threshold:.2f}.",
            )
        return True, "Requested working memory write accepted."

    def _promotion_requirements(self, signal: LearningSignal, target: MemoryLayer | None) -> dict[str, object]:
        payload: dict[str, object] = {
            "target_layer": None if target is None else target.value,
            "observed_validation_score": signal.computed_validation_score,
            "observed_validation_evidence": _validation_evidence_metadata(signal),
            "observed_repeat_count": signal.repeat_count,
            "observed_explicit_instruction": signal.explicit_instruction,
        }
        if target is not None:
            spec = self.specs[target]
            payload.update(
                {
                    "min_validation_score": spec.promotion_threshold,
                    "min_repeat_count": spec.min_repeat_count_for_promotion,
                    "requires_explicit_instruction": target == MemoryLayer.POLICY,
                }
            )
        else:
            payload.update(
                {
                    "min_validation_score": self.specs[MemoryLayer.EPISODIC].promotion_threshold,
                    "min_repeat_count": 1,
                    "requires_explicit_instruction": False,
                }
            )
        return payload

    def _optimizer_trace(self, signal: LearningSignal, target: MemoryLayer | None) -> OptimizerTrace:
        score = signal.computed_validation_score
        source_chars = signal.effective_source_evidence_chars
        compression_ratio = (len(signal.content) / source_chars) if source_chars and source_chars > 0 else None
        surprise = self._surprise(signal, target)
        repeat_bonus = min(max(signal.repeat_count - 1, 0) * 0.03, 0.12)
        target_bonus = 0.0 if target is None else 0.02 * _layer_level(target)
        confidence_delta = min(score - signal.confidence + repeat_bonus + target_bonus, 0.25)
        effective = max(0.0, min(0.99, signal.confidence + max(confidence_delta, 0.0)))
        return OptimizerTrace(
            surprise=surprise,
            validation_score=score,
            repeat_count=signal.repeat_count,
            compression_ratio=compression_ratio,
            confidence_delta=max(confidence_delta, 0.0),
            effective_confidence=effective,
        )

    def _surprise(self, signal: LearningSignal, target: MemoryLayer | None) -> float:
        if self.memory is None or target is None:
            return max(0.0, min(1.0, 1.0 - signal.confidence))
        try:
            from .models import RetrievalQuery

            hits = self.memory.retrieve(RetrievalQuery(query=f"{signal.title} {signal.content}", layers=(target,), k_per_layer=3))
        except Exception:
            return max(0.0, min(1.0, 1.0 - signal.confidence))
        if not hits:
            return 1.0
        similarity = max(SequenceMatcher(None, signal.content, hit.record.content).ratio() for hit in hits)
        conflict_bonus = 0.15 if any(hit.record.metadata.get("conflict_group_id") for hit in hits) else 0.0
        return max(0.0, min(1.0, (1.0 - similarity) + conflict_bonus))


def _flow_for(source: MemoryLayer, target: MemoryLayer | None) -> ContextFlow:
    if target == MemoryLayer.EPISODIC:
        return DEFAULT_CONTEXT_FLOWS["working_to_episode"]
    if target == MemoryLayer.SEMANTIC:
        return DEFAULT_CONTEXT_FLOWS["episode_to_semantic"]
    if target == MemoryLayer.PROCEDURAL:
        return DEFAULT_CONTEXT_FLOWS["episode_to_procedural"]
    if target == MemoryLayer.POLICY:
        return DEFAULT_CONTEXT_FLOWS["procedure_to_policy"]
    if target == MemoryLayer.SELF:
        return DEFAULT_CONTEXT_FLOWS["episode_to_self"]
    if source == MemoryLayer.WORKING:
        return DEFAULT_CONTEXT_FLOWS["interaction_to_working"]
    return DEFAULT_CONTEXT_FLOWS["episode_to_semantic"]


def _validation_evidence_metadata(signal: LearningSignal) -> dict[str, object]:
    if signal.validation_evidence is not None:
        return signal.validation_evidence.to_metadata()
    return {
        "test_refs": [],
        "lint_refs": [],
        "repair_refs": [],
        "review_refs": [],
        "task_refs": [],
        "human_explicit": signal.explicit_instruction,
        "legacy_raw_score": True,
        "source_evidence_chars": signal.source_evidence_chars,
        "computed_score": signal.computed_validation_score,
    }


def _ref_labels(refs: tuple[EvidenceRef, ...]) -> list[str]:
    return [f"{ref.source}:{ref.locator}" for ref in refs]


def _target_kind(kind: MemoryKind, target_layer: MemoryLayer) -> MemoryKind:
    if target_layer == MemoryLayer.POLICY:
        return MemoryKind.POLICY
    if target_layer == MemoryLayer.PROCEDURAL:
        return MemoryKind.PROCEDURE
    if target_layer == MemoryLayer.SELF:
        return MemoryKind.FACT
    if target_layer == MemoryLayer.SEMANTIC:
        return MemoryKind.FACT
    if target_layer == MemoryLayer.EPISODIC:
        return MemoryKind.EVENT
    return kind


def _layer_level(layer: MemoryLayer) -> int:
    return {
        MemoryLayer.WORKING: 1,
        MemoryLayer.EPISODIC: 2,
        MemoryLayer.SEMANTIC: 3,
        MemoryLayer.PROCEDURAL: 4,
        MemoryLayer.SELF: 4,
        MemoryLayer.POLICY: 5,
    }[layer]
