from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

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
    compression_ratio: float
    confidence_delta: float
    effective_confidence: float

    def to_metadata(self) -> dict[str, object]:
        return {
            "surprise": round(self.surprise, 4),
            "validation_score": round(self.validation_score, 4),
            "repeat_count": self.repeat_count,
            "compression_ratio": round(self.compression_ratio, 4),
            "confidence_delta": round(self.confidence_delta, 4),
            "effective_confidence": round(self.effective_confidence, 4),
        }


@dataclass(frozen=True)
class LearningSignal:
    title: str
    content: str
    kind: MemoryKind
    source_layer: MemoryLayer
    confidence: float = 0.6
    importance: float = 0.5
    validation_score: float = 0.7
    repeat_count: int = 1
    explicit_instruction: bool = False
    source: str = "learning_signal"
    locator: str = "manual"
    tags: dict[str, str] | None = None
    metadata: dict[str, Any] | None = None
    requested_target_layer: MemoryLayer | None = None


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

    def decide(self, signal: LearningSignal, *, action: LearningAction = "write") -> LearningDecision:
        target_layer, reason = self._target(signal)
        flow = _flow_for(signal.source_layer, target_layer)
        trace = _optimizer_trace(signal, target_layer)
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
            repeat_count=repeat_count,
            explicit_instruction=explicit_instruction,
            source="consolidator",
            locator=record.id,
            tags=record.tags,
            metadata=record.metadata,
        )
        return self.decide(signal, action="promote")

    def to_memory_record(self, signal: LearningSignal, decision: LearningDecision) -> MemoryRecord:
        if decision.target_layer is None:
            raise ValueError("Cannot create memory record from rejected learning decision")
        evidence = [EvidenceRef(source=signal.source, locator=signal.locator, quote=decision.reason)]
        metadata = {
            **(signal.metadata or {}),
            "nested_learning": {
                "context_flow": decision.flow.to_metadata(),
                "optimizer_trace": decision.optimizer_trace.to_metadata(),
                "decision": decision.to_payload(),
            },
            "validation_method": "nested_learning_kernel",
            "validation_score": signal.validation_score,
            "repeat_count": signal.repeat_count,
            "explicit_instruction": signal.explicit_instruction,
        }
        return MemoryRecord(
            title=signal.title,
            content=signal.content,
            layer=decision.target_layer,
            kind=decision.target_kind,
            tags=dict(signal.tags or {}),
            metadata=metadata,
            evidence=evidence,
            confidence=decision.confidence,
            importance=decision.importance,
        )

    def _target(self, signal: LearningSignal) -> tuple[MemoryLayer | None, str]:
        if signal.validation_score < 0.65:
            return None, "Rejected: validation score is below the working-to-episodic gate."

        if signal.requested_target_layer is not None:
            allowed, reason = _requested_target_allowed(signal, signal.requested_target_layer)
            return (signal.requested_target_layer, reason) if allowed else (None, reason)

        if signal.source_layer == MemoryLayer.WORKING:
            return MemoryLayer.EPISODIC, "Working context survived validation and became an episodic event."

        if signal.source_layer == MemoryLayer.EPISODIC:
            if signal.kind in {MemoryKind.FAILURE, MemoryKind.PROCEDURE} and signal.repeat_count >= 2 and signal.validation_score >= 0.78:
                return MemoryLayer.PROCEDURAL, "Repeated validated outcome became a reusable procedure."
            if signal.validation_score >= 0.78:
                return MemoryLayer.SEMANTIC, "Validated episode became stable semantic memory."
            return None, "Rejected: episodic signal did not clear semantic/procedural gates."

        if signal.source_layer == MemoryLayer.PROCEDURAL:
            if signal.validation_score >= 0.95 and signal.repeat_count >= 5 and signal.explicit_instruction:
                return MemoryLayer.POLICY, "Explicit repeated procedure cleared the policy-candidate gate."
            return None, "Rejected: policy promotion requires explicit instruction, high validation, and repeated evidence."

        if signal.source_layer == MemoryLayer.SEMANTIC:
            if signal.kind == MemoryKind.PROCEDURE and signal.repeat_count >= 2 and signal.validation_score >= 0.82:
                return MemoryLayer.PROCEDURAL, "Semantic procedure cleared repeated-use gate."
            return None, "Rejected: semantic memory is already stable and needs correction, not promotion."

        return None, "Rejected: policy memory cannot self-promote."


def _requested_target_allowed(signal: LearningSignal, target: MemoryLayer) -> tuple[bool, str]:
    if target == MemoryLayer.POLICY:
        ok = signal.explicit_instruction and signal.validation_score >= 0.97 and signal.repeat_count >= 5
        return (
            ok,
            "Explicit repeated signal requested policy memory and cleared the policy gate."
            if ok
            else "Rejected: requested policy writes require explicit instruction, validation >= 0.97, and repeat_count >= 5.",
        )
    if target == MemoryLayer.PROCEDURAL:
        ok = signal.validation_score >= 0.78 and signal.repeat_count >= 2
        return ok, "Requested procedural memory cleared repeated validation gate." if ok else "Rejected: procedural writes require validation >= 0.78 and repeat_count >= 2."
    if target == MemoryLayer.SEMANTIC:
        ok = signal.validation_score >= 0.78
        return ok, "Requested semantic memory cleared validation gate." if ok else "Rejected: semantic writes require validation >= 0.78."
    if target == MemoryLayer.EPISODIC:
        return True, "Requested episodic memory cleared baseline validation gate."
    return True, "Requested working memory write accepted."


def _flow_for(source: MemoryLayer, target: MemoryLayer | None) -> ContextFlow:
    if target == MemoryLayer.EPISODIC:
        return DEFAULT_CONTEXT_FLOWS["working_to_episode"]
    if target == MemoryLayer.SEMANTIC:
        return DEFAULT_CONTEXT_FLOWS["episode_to_semantic"]
    if target == MemoryLayer.PROCEDURAL:
        return DEFAULT_CONTEXT_FLOWS["episode_to_procedural"]
    if target == MemoryLayer.POLICY:
        return DEFAULT_CONTEXT_FLOWS["procedure_to_policy"]
    if source == MemoryLayer.WORKING:
        return DEFAULT_CONTEXT_FLOWS["interaction_to_working"]
    return DEFAULT_CONTEXT_FLOWS["episode_to_semantic"]


def _optimizer_trace(signal: LearningSignal, target: MemoryLayer | None) -> OptimizerTrace:
    content_chars = max(len(signal.content), 1)
    title_chars = max(len(signal.title), 1)
    compression_ratio = min(title_chars / content_chars, 1.0)
    surprise = max(0.0, min(1.0, 1.0 - signal.confidence))
    repeat_bonus = min(max(signal.repeat_count - 1, 0) * 0.03, 0.12)
    target_bonus = 0.0 if target is None else 0.02 * _layer_level(target)
    confidence_delta = min(signal.validation_score - signal.confidence + repeat_bonus + target_bonus, 0.25)
    effective = max(0.0, min(0.99, signal.confidence + max(confidence_delta, 0.0)))
    return OptimizerTrace(
        surprise=surprise,
        validation_score=signal.validation_score,
        repeat_count=signal.repeat_count,
        compression_ratio=compression_ratio,
        confidence_delta=max(confidence_delta, 0.0),
        effective_confidence=effective,
    )


def _target_kind(kind: MemoryKind, target_layer: MemoryLayer) -> MemoryKind:
    if target_layer == MemoryLayer.POLICY:
        return MemoryKind.POLICY
    if target_layer == MemoryLayer.PROCEDURAL:
        return MemoryKind.PROCEDURE
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
        MemoryLayer.POLICY: 5,
    }[layer]
