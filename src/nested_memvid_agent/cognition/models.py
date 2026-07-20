from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ..models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from ..runtime_models import StrategyProposal, ToolExecution


@dataclass(frozen=True)
class StrategyDiff:
    previous_action: str
    new_action: str
    difference: str
    is_meaningfully_different: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "previous_action": self.previous_action,
            "new_action": self.new_action,
            "difference": self.difference,
            "is_meaningfully_different": self.is_meaningfully_different,
        }


@dataclass(frozen=True)
class RetryDecision:
    retry_allowed: bool
    reason: str
    required_change: str = ""
    strategy_diff: StrategyDiff | None = None
    similar_lessons: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "retry_allowed": self.retry_allowed,
            "reason": self.reason,
            "required_change": self.required_change,
            "strategy_diff": self.strategy_diff.to_payload() if self.strategy_diff else None,
            "similar_lessons": list(self.similar_lessons),
        }


@dataclass(frozen=True)
class FailureEpisode:
    failure_id: str
    run_id: str
    task_id: str | None
    tool_name: str | None
    command: str | None
    error_text: str
    category: str
    diagnosis: str
    attempted_strategy: str
    similar_lessons_used: tuple[str, ...] = ()
    resolved: bool = False
    resolution_summary: str | None = None
    validation_evidence: tuple[str, ...] = ()
    confidence: float = 0.76
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_tool_failure(
        cls,
        *,
        run_id: str,
        execution: ToolExecution,
        category: str,
        diagnosis: str,
        attempted_strategy: str,
        similar_lessons_used: tuple[str, ...] = (),
    ) -> FailureEpisode:
        return cls(
            failure_id=f"failure_{uuid4().hex}",
            run_id=run_id,
            task_id=None,
            tool_name=execution.call.name,
            command=_command_text(execution),
            error_text=_failure_text(execution),
            category=category,
            diagnosis=diagnosis,
            attempted_strategy=attempted_strategy,
            similar_lessons_used=similar_lessons_used,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "failure_id": self.failure_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "tool_name": self.tool_name,
            "command": self.command,
            "error_text": self.error_text,
            "category": self.category,
            "diagnosis": self.diagnosis,
            "attempted_strategy": self.attempted_strategy,
            "similar_lessons_used": list(self.similar_lessons_used),
            "resolved": self.resolved,
            "resolution_summary": self.resolution_summary,
            "validation_evidence": list(self.validation_evidence),
            "confidence": self.confidence,
            "created_at": self.created_at,
        }

    def to_memory_record(self) -> MemoryRecord:
        title = f"FailureEpisode: {self.category}"
        if self.tool_name:
            title = f"{title} in {self.tool_name}"
        content = json.dumps(self.to_payload(), indent=2)
        evidence = [
            EvidenceRef(
                source=f"agent_runtime://runs/{self.run_id}",
                locator=self.failure_id,
                quote=self.error_text[:280],
            )
        ]
        evidence.extend(
            EvidenceRef(source="validation", locator=validation_ref)
            for validation_ref in self.validation_evidence
        )
        return MemoryRecord(
            id=self.failure_id,
            title=title,
            content=content,
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.FAILURE,
            confidence=self.confidence,
            importance=0.78,
            tags={"run_id": self.run_id, "failure_category": self.category},
            metadata={
                "cognition_schema": "failure_episode.v1",
                "frame_type": "failure_note",
                "run_id": self.run_id,
                "tool_name": self.tool_name,
                "failure_category": self.category,
                "validation_status": "resolved" if self.resolved else "unresolved",
            },
            evidence=evidence,
        )


@dataclass(frozen=True)
class LessonCard:
    id: str
    title: str
    failure_signature: str
    failure_category: str
    context: str
    root_cause: str
    bad_strategy: str
    corrected_strategy: str
    validation_command: str | None
    evidence_refs: tuple[str, ...]
    success_count: int
    failure_count: int
    confidence: float
    applies_when: tuple[str, ...]
    avoid_when: tuple[str, ...]
    created_at: str
    updated_at: str

    @classmethod
    def from_resolution(
        cls,
        *,
        failure: FailureEpisode,
        validation: ToolExecution,
        strategy: StrategyProposal,
    ) -> LessonCard:
        now = datetime.now(UTC).isoformat()
        corrected = strategy.changed_strategy.strip()
        validation_command = _command_text(validation)
        category = failure.category
        return cls(
            id=f"lesson_{uuid4().hex}",
            title=f"{category}: {corrected[:72] or 'validated changed strategy'}",
            failure_signature=_signature(failure.error_text),
            failure_category=category,
            context=f"Run {failure.run_id}; tool={failure.tool_name or 'unknown'}",
            root_cause=failure.diagnosis,
            bad_strategy=failure.attempted_strategy or failure.command or "unspecified failed action",
            corrected_strategy=corrected,
            validation_command=validation_command,
            evidence_refs=(failure.failure_id, validation.call.id),
            success_count=1,
            failure_count=1,
            confidence=0.84,
            applies_when=(category, failure.tool_name or "tool_failure"),
            avoid_when=("repeating the same tool call without a changed strategy",),
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def from_memory_record(cls, record: MemoryRecord) -> LessonCard:
        payload = json.loads(record.content)
        if not isinstance(payload, dict):
            raise ValueError("LessonCard record content must decode to an object")
        return cls(
            id=str(payload.get("id", record.id)),
            title=str(payload.get("title", record.title.removeprefix("LessonCard: ").strip())),
            failure_signature=str(payload.get("failure_signature", "")),
            failure_category=str(payload.get("failure_category", record.metadata.get("failure_category", ""))),
            context=str(payload.get("context", "")),
            root_cause=str(payload.get("root_cause", "")),
            bad_strategy=str(payload.get("bad_strategy", "")),
            corrected_strategy=str(payload.get("corrected_strategy", "")),
            validation_command=_optional_string(payload.get("validation_command")),
            evidence_refs=tuple(str(item) for item in _list_value(payload.get("evidence_refs"))),
            success_count=int(payload.get("success_count", record.metadata.get("success_count", 0)) or 0),
            failure_count=int(payload.get("failure_count", record.metadata.get("failure_count", 0)) or 0),
            confidence=float(payload.get("confidence", record.confidence)),
            applies_when=tuple(str(item) for item in _list_value(payload.get("applies_when"))),
            avoid_when=tuple(str(item) for item in _list_value(payload.get("avoid_when"))),
            created_at=str(payload.get("created_at", record.created_at.isoformat())),
            updated_at=str(payload.get("updated_at", record.updated_at.isoformat())),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "failure_signature": self.failure_signature,
            "failure_category": self.failure_category,
            "context": self.context,
            "root_cause": self.root_cause,
            "bad_strategy": self.bad_strategy,
            "corrected_strategy": self.corrected_strategy,
            "validation_command": self.validation_command,
            "evidence_refs": list(self.evidence_refs),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "confidence": self.confidence,
            "applies_when": list(self.applies_when),
            "avoid_when": list(self.avoid_when),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_memory_record(self) -> MemoryRecord:
        content = json.dumps(self.to_payload(), indent=2)
        return MemoryRecord(
            id=self.id,
            title=f"LessonCard: {self.title}",
            content=content,
            layer=MemoryLayer.PROCEDURAL,
            kind=MemoryKind.PROCEDURE,
            confidence=self.confidence,
            importance=0.84,
            tags={"failure_category": self.failure_category, "lesson_card": self.id},
            metadata={
                "cognition_schema": "lesson_card.v1",
                "frame_type": "skill_card",
                "failure_category": self.failure_category,
                "validation_status": "validated_once",
                "success_count": self.success_count,
                "failure_count": self.failure_count,
                "repeat_count": self.success_count + self.failure_count,
            },
            evidence=[
                EvidenceRef(source="failure_episode", locator=self.evidence_refs[0]),
                EvidenceRef(source="validation", locator=self.evidence_refs[-1]),
            ],
        )


@dataclass
class ProofOfWorkSummary:
    objective: str
    completed_steps: list[str] = field(default_factory=list)
    tools_used: list[dict[str, Any]] = field(default_factory=list)
    validation_evidence: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    diagnoses: list[dict[str, Any]] = field(default_factory=list)
    lessons_applied: list[dict[str, Any]] = field(default_factory=list)
    lessons_created: list[dict[str, Any]] = field(default_factory=list)
    remaining_risks: list[str] = field(default_factory=list)
    stop_reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "completed_steps": list(self.completed_steps),
            "tools_used": list(self.tools_used),
            "validation_evidence": list(self.validation_evidence),
            "failures": list(self.failures),
            "diagnoses": list(self.diagnoses),
            "lessons_applied": list(self.lessons_applied),
            "lessons_created": list(self.lessons_created),
            "remaining_risks": list(self.remaining_risks),
            "stop_reason": self.stop_reason,
        }


def _failure_text(execution: ToolExecution) -> str:
    parts = []
    if execution.error:
        parts.append(str(execution.error))
    if execution.content:
        parts.append(execution.content)
    return "\n".join(parts).strip() or "unknown tool failure"


def _command_text(execution: ToolExecution) -> str | None:
    command = execution.call.arguments.get("command")
    if isinstance(command, list):
        return " ".join(str(item) for item in command)
    if isinstance(command, str):
        return command
    if execution.call.arguments:
        return json.dumps(execution.call.arguments, sort_keys=True)
    return None


def _signature(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:240] or "unknown failure"


def _list_value(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
