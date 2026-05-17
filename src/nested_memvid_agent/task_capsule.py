from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .backends.base import MemoryBackend
from .backends.in_memory import InMemoryBackend
from .backends.memvid_backend import MemvidBackend
from .context_frames import MV2ContextFrame, to_memory_record
from .models import MemoryHit, MemoryKind, MemoryLayer, MemoryRecord
from .nested_learning import LearningSignal
from .runtime_models import AgentTurnResult, ToolExecution


@dataclass(frozen=True)
class TaskCapsuleSummary:
    run_id: str
    objective: str
    capsule_path: Path
    summary: str
    learning_signals: tuple[LearningSignal, ...] = ()
    candidate_policy_items: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    telemetry: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "objective": self.objective,
            "capsule_path": str(self.capsule_path),
            "summary": self.summary,
            "learning_signals": [
                {
                    "title": signal.title,
                    "content": signal.content,
                    "kind": signal.kind.value,
                    "source_layer": signal.source_layer.value,
                    "confidence": signal.confidence,
                    "importance": signal.importance,
                    "validation_score": signal.computed_validation_score,
                    "validation_evidence": signal.validation_evidence.to_metadata()
                    if signal.validation_evidence
                    else {"legacy_raw_score": True, "computed_score": signal.computed_validation_score},
                    "repeat_count": signal.repeat_count,
                    "explicit_instruction": signal.explicit_instruction,
                    "requested_target_layer": signal.requested_target_layer.value
                    if signal.requested_target_layer
                    else None,
                }
                for signal in self.learning_signals
            ],
            "candidate_policy_items": list(self.candidate_policy_items),
            "unresolved_questions": list(self.unresolved_questions),
            "telemetry": self.telemetry,
        }


class TaskCapsuleWriter:
    """Writes a run-scoped `complete.mv2` artifact without adding a permanent layer."""

    def __init__(
        self,
        *,
        runs_dir: Path,
        run_id: str,
        backend: str = "memory",
    ) -> None:
        self.runs_dir = runs_dir
        self.run_id = run_id
        self.backend_name = backend
        self.path = runs_dir / run_id / "complete.mv2"
        self.backend: MemoryBackend | None = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        backend_cls: type[MemoryBackend] = MemvidBackend if self.backend_name == "memvid" else InMemoryBackend
        self.backend = backend_cls(path=self.path, layer=MemoryLayer.EPISODIC)
        self.backend.open()
        if self.backend_name != "memvid":
            self.path.touch(exist_ok=True)

    def put_frame(self, frame: MV2ContextFrame) -> str:
        backend = self._backend()
        if frame.layer != MemoryLayer.EPISODIC:
            frame = MV2ContextFrame(
                id=frame.id,
                frame_type=frame.frame_type,
                title=frame.title,
                content=frame.content,
                layer=MemoryLayer.EPISODIC,
                kind=frame.kind,
                parent_ids=frame.parent_ids,
                child_ids=frame.child_ids,
                source_uri=frame.source_uri,
                source_span=frame.source_span,
                content_hash=frame.content_hash,
                token_count=frame.token_count,
                confidence=frame.confidence,
                importance=frame.importance,
                created_at=frame.created_at,
                updated_at=frame.updated_at,
                tags=frame.tags,
                metadata=frame.metadata,
            )
        return backend.put(to_memory_record(frame))

    def put_record(self, record: MemoryRecord) -> str:
        return self._backend().put(record)

    def seal(self) -> None:
        self._backend().seal()

    def close(self) -> None:
        if self.backend is not None:
            self.backend.close()
        self.backend = None

    def _backend(self) -> MemoryBackend:
        if self.backend is None:
            raise RuntimeError("TaskCapsuleWriter.open() must be called before use")
        return self.backend


def write_run_capsule(
    *,
    runs_dir: Path,
    run_id: str,
    objective: str,
    backend: str = "memory",
    selected_context: str = "",
    tool_executions: tuple[ToolExecution, ...] = (),
    final_response: str = "",
    files_touched: tuple[str, ...] = (),
    tests_run: tuple[str, ...] = (),
    errors_encountered: tuple[str, ...] = (),
    unresolved_questions: tuple[str, ...] = (),
    reusable_lessons: tuple[str, ...] = (),
    candidate_facts: tuple[str, ...] = (),
    candidate_procedures: tuple[str, ...] = (),
    candidate_corrections: tuple[str, ...] = (),
    candidate_policy_items: tuple[str, ...] = (),
) -> Path:
    writer = TaskCapsuleWriter(runs_dir=runs_dir, run_id=run_id, backend=backend)
    writer.open()
    try:
        payload: dict[str, object] = {
            "run_id": run_id,
            "objective": objective,
            "selected_context": selected_context,
            "tool_calls": [_execution_to_payload(execution) for execution in tool_executions],
            "tool_outputs": [execution.content for execution in tool_executions],
            "files_touched": list(files_touched),
            "tests_run": list(tests_run),
            "errors_encountered": list(errors_encountered),
            "final_assistant_response": final_response,
            "unresolved_questions": list(unresolved_questions),
            "reusable_lessons": list(reusable_lessons),
            "candidate_facts": list(candidate_facts),
            "candidate_procedures": list(candidate_procedures),
            "candidate_corrections": list(candidate_corrections),
            "candidate_policy_items": list(candidate_policy_items),
        }
        candidate_frames = _candidate_frames(payload)
        writer.put_frame(
            MV2ContextFrame(
                id=f"capsule_{run_id}",
                frame_type="task_summary",
                title=f"Run capsule: {run_id}",
                content=json.dumps(payload, indent=2),
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.SUMMARY,
                child_ids=tuple(frame.id for frame in candidate_frames),
                source_uri=f"mv2://runs/{run_id}/complete.mv2",
                source_span={"section": "root"},
                confidence=0.8,
                importance=0.8,
                tags={"capsule": "complete", "run_id": run_id},
                metadata={
                    "run_id": run_id,
                    "capsule_artifact": True,
                    "permanent_layer": False,
                },
            )
        )
        for frame in candidate_frames:
            writer.put_frame(frame)
        writer.seal()
        return writer.path
    finally:
        writer.close()


def write_turn_capsule(
    *,
    runs_dir: Path,
    run_id: str,
    result: AgentTurnResult,
    backend: str = "memory",
    selected_context: str = "",
) -> Path:
    errors = tuple(execution.content for execution in result.tool_executions if not execution.success)
    tests = tuple(
        execution.content[:500]
        for execution in result.tool_executions
        if execution.call.name in {"test.run", "shell.run"} and execution.success
    )
    return write_run_capsule(
        runs_dir=runs_dir,
        run_id=run_id,
        objective=result.user_message,
        backend=backend,
        selected_context=selected_context,
        tool_executions=result.tool_executions,
        final_response=result.assistant_message,
        tests_run=tests,
        errors_encountered=errors,
        reusable_lessons=_lessons_from_result(result),
        candidate_facts=_facts_from_result(result),
        candidate_corrections=_corrections_from_result(result),
    )


def summarize_run_capsule(
    *,
    runs_dir: Path,
    run_id: str,
    backend: str = "memory",
) -> TaskCapsuleSummary:
    path = runs_dir / run_id / "complete.mv2"
    capsule = _load_capsule_payload(path, backend=backend)
    objective = str(capsule.get("objective", ""))
    signals = extract_learning_signals(capsule, run_id=run_id)
    summary = _summary_text(capsule, signals)
    return TaskCapsuleSummary(
        run_id=run_id,
        objective=objective,
        capsule_path=path,
        summary=summary,
        learning_signals=tuple(signals),
        candidate_policy_items=tuple(_string_list(capsule.get("candidate_policy_items"))),
        unresolved_questions=tuple(_string_list(capsule.get("unresolved_questions"))),
        telemetry={
            "backend": backend,
            "is_permanent_layer": False,
            "signal_count": len(signals),
            "exists": path.exists(),
        },
    )


def extract_learning_signals(capsule: dict[str, object], *, run_id: str) -> list[LearningSignal]:
    signals: list[LearningSignal] = []
    source = "task_capsule"
    for content in _string_list(capsule.get("errors_encountered")):
        signals.append(
            LearningSignal(
                title=_title_for("Failure note", content),
                content=content,
                kind=MemoryKind.FAILURE,
                source_layer=MemoryLayer.WORKING,
                confidence=0.68,
                importance=0.75,
                validation_score=0.74,
                repeat_count=1,
                source=source,
                locator=run_id,
                metadata={"run_id": run_id, "capsule_signal": "failure"},
            )
        )
    for content in _string_list(capsule.get("candidate_corrections")):
        signals.append(
            LearningSignal(
                title=_title_for("Correction", content),
                content=content,
                kind=MemoryKind.CORRECTION,
                source_layer=MemoryLayer.EPISODIC,
                confidence=0.72,
                importance=0.75,
                validation_score=0.78,
                repeat_count=1,
                source=source,
                locator=run_id,
                metadata={"run_id": run_id, "capsule_signal": "correction"},
            )
        )
    for content in _string_list(capsule.get("candidate_facts")):
        signals.append(
            LearningSignal(
                title=_title_for("Candidate fact", content),
                content=content,
                kind=MemoryKind.FACT,
                source_layer=MemoryLayer.EPISODIC,
                confidence=0.72,
                importance=0.65,
                validation_score=0.78,
                repeat_count=1,
                source=source,
                locator=run_id,
                metadata={"run_id": run_id, "capsule_signal": "fact"},
            )
        )
    for content in _string_list(capsule.get("candidate_procedures")):
        signals.append(
            LearningSignal(
                title=_title_for("Candidate procedure", content),
                content=content,
                kind=MemoryKind.PROCEDURE,
                source_layer=MemoryLayer.EPISODIC,
                confidence=0.76,
                importance=0.78,
                validation_score=0.82,
                repeat_count=2,
                source=source,
                locator=run_id,
                metadata={"run_id": run_id, "capsule_signal": "procedure"},
            )
        )
    for content in _string_list(capsule.get("reusable_lessons")):
        signals.append(
            LearningSignal(
                title=_title_for("Reusable lesson", content),
                content=content,
                kind=MemoryKind.EVENT,
                source_layer=MemoryLayer.WORKING,
                confidence=0.68,
                importance=0.65,
                validation_score=0.7,
                repeat_count=1,
                source=source,
                locator=run_id,
                metadata={"run_id": run_id, "capsule_signal": "lesson"},
            )
        )
    for content in _string_list(capsule.get("candidate_policy_items")):
        signals.append(
            LearningSignal(
                title=_title_for("Policy candidate requiring review", content),
                content=content,
                kind=MemoryKind.POLICY,
                source_layer=MemoryLayer.PROCEDURAL,
                confidence=0.8,
                importance=0.9,
                validation_score=0.9,
                repeat_count=1,
                explicit_instruction=False,
                source=source,
                locator=run_id,
                metadata={"run_id": run_id, "capsule_signal": "policy_candidate", "requires_human_review": True},
                requested_target_layer=MemoryLayer.POLICY,
            )
        )
    return signals


def _candidate_frames(payload: dict[str, object]) -> list[MV2ContextFrame]:
    run_id = str(payload["run_id"])
    frames: list[MV2ContextFrame] = []
    candidates = [
        ("reusable_lessons", "task_summary", MemoryKind.SUMMARY),
        ("errors_encountered", "failure_note", MemoryKind.FAILURE),
        ("candidate_facts", "section_summary", MemoryKind.FACT),
        ("candidate_procedures", "skill_card", MemoryKind.PROCEDURE),
        ("candidate_corrections", "correction", MemoryKind.CORRECTION),
        ("candidate_policy_items", "trace_stub", MemoryKind.POLICY),
    ]
    for key, frame_type, kind in candidates:
        for index, content in enumerate(_string_list(payload.get(key)), start=1):
            frames.append(
                MV2ContextFrame(
                    id=f"capsule_{run_id}_{key}_{index}",
                    frame_type=frame_type,
                    title=_title_for(key.replace("_", " ").title(), content),
                    content=content,
                    layer=MemoryLayer.EPISODIC,
                    kind=kind,
                    parent_ids=(f"capsule_{run_id}",),
                    source_uri=f"mv2://runs/{run_id}/complete.mv2",
                    source_span={"section": key, "index": index},
                    confidence=0.75,
                    importance=0.7,
                    tags={"capsule": "complete", "run_id": run_id},
                    metadata={"run_id": run_id, "capsule_section": key, "permanent_layer": False},
                )
            )
    return frames


def _load_capsule_payload(path: Path, *, backend: str) -> dict[str, object]:
    if not path.exists():
        return {"run_id": path.parent.name, "objective": "", "missing": True}
    capsule_backend = _open_capsule_backend(path, backend=backend)
    try:
        records = getattr(capsule_backend, "records", None)
        if isinstance(records, list):
            for record in records:
                payload = _payload_from_record(record, run_id=path.parent.name)
                if payload is not None:
                    return payload
        for query in (
            f"capsule_{path.parent.name}",
            f"Run capsule: {path.parent.name}",
            path.parent.name,
        ):
            for hit in capsule_backend.find(query, k=8):
                payload = _payload_from_record(hit.record, run_id=path.parent.name)
                if payload is not None:
                    return payload
    finally:
        capsule_backend.close()
    return {"run_id": path.parent.name, "objective": "", "empty": True}


def _open_capsule_backend(path: Path, *, backend: str) -> MemoryBackend:
    if backend == "memvid":
        capsule_backend: MemoryBackend = MemvidBackend(path=path, layer=MemoryLayer.EPISODIC, read_only=True)
    elif backend == "memory":
        capsule_backend = InMemoryBackend(path=path, layer=MemoryLayer.EPISODIC)
    else:
        raise ValueError(f"Unknown capsule backend: {backend}")
    capsule_backend.open()
    return capsule_backend


def _payload_from_record(record: MemoryRecord, *, run_id: str) -> dict[str, object] | None:
    payload = _json_object_from_text(record.content)
    if payload is None:
        return None
    if str(payload.get("run_id", "")) != run_id:
        return None
    return payload


def _json_object_from_text(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            loaded, _ = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError:
            return None
    return loaded if isinstance(loaded, dict) else None


def _summary_text(capsule: dict[str, object], signals: list[LearningSignal]) -> str:
    objective = str(capsule.get("objective", ""))
    final_response = str(capsule.get("final_assistant_response", ""))
    errors = _string_list(capsule.get("errors_encountered"))
    lines = [
        f"Objective: {objective}",
        f"Final response: {final_response[:500]}",
        f"Learning signals: {len(signals)}",
        f"Errors encountered: {len(errors)}",
    ]
    return "\n".join(lines)


def _execution_to_payload(execution: ToolExecution) -> dict[str, object]:
    return {
        "tool": execution.call.name,
        "tool_call_id": execution.call.id,
        "arguments": execution.call.arguments,
        "success": execution.success,
        "content": execution.content,
        "data": execution.data,
        "error": execution.error,
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _title_for(prefix: str, content: str) -> str:
    compact = " ".join(content.split())
    return f"{prefix}: {compact[:72]}" if compact else prefix


def _lessons_from_result(result: AgentTurnResult) -> tuple[str, ...]:
    lessons = []
    if result.stop_reason == "approval_required":
        lessons.append("High-risk tool execution stopped until approval was granted.")
    if result.tool_executions:
        lessons.append(f"Run used {len(result.tool_executions)} tool calls before final response.")
    return tuple(lessons)


def _facts_from_result(result: AgentTurnResult) -> tuple[str, ...]:
    if result.assistant_message.strip():
        return (f"Run {result.session_id} completed with stop_reason={result.stop_reason}.",)
    return ()


def _corrections_from_result(result: AgentTurnResult) -> tuple[str, ...]:
    message = result.user_message.strip()
    if "correction" in message.lower():
        return (message,)
    return ()


def hits_to_context_text(hits: tuple[MemoryHit, ...]) -> str:
    return "\n\n".join(f"[{hit.record.layer.value}] {hit.record.title}\n{hit.snippet or hit.record.content}" for hit in hits)
