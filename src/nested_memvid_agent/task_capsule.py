from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO
from uuid import uuid4

from .backends.base import MemoryBackend
from .backends.in_memory import InMemoryBackend
from .backends.memvid_backend import MemvidBackend
from .context_frames import MV2ContextFrame, to_memory_record
from .file_lock import lock_exclusive, unlock
from .models import EvidenceRef, MemoryHit, MemoryKind, MemoryLayer, MemoryRecord
from .nested_learning import LearningSignal, ValidationEvidence
from .private_artifacts import (
    create_private_empty_file,
    ensure_owner_only_directory,
    harden_memory_artifact_files,
    harden_private_file,
    harden_task_capsule_run,
    open_private_file_descriptor,
    write_private_text,
)
from .runtime_models import AgentTurnResult, ToolExecution
from .security_boundary import redact_secrets, sanitize_memory_record

_CAPSULE_RUN_ID_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,198}[A-Za-z0-9])?\Z",
    flags=re.ASCII,
)
_CAPSULE_COMPLETION_MARKER = "capsule.complete.json"
_CAPSULE_LAYER_LOCK = ".complete.mv2.kestrel.lock"
_CAPSULE_RETENTION_LOCK = ".kestrel-capsule-retention.lock"
_CAPSULE_DATA_ARTIFACTS = frozenset(
    {
        "complete.mv2",
        "complete.memory.json",
        "complete.mv2.records.json",
    }
)
_CAPSULE_KNOWN_ARTIFACTS = _CAPSULE_DATA_ARTIFACTS | {
    _CAPSULE_COMPLETION_MARKER,
    _CAPSULE_LAYER_LOCK,
}
_COMPLETION_MARKER_FORMAT = "kestrel-task-capsule-completion"
_MAX_LEGACY_CAPSULE_INDEX_BYTES = 64 * 1024 * 1024


def validate_capsule_run_id(run_id: str) -> str:
    """Require a portable run identifier that cannot escape the capsule root."""

    if not isinstance(run_id, str) or _CAPSULE_RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError(
            "run_id must be a single safe path component using 1-200 ASCII letters, "
            "digits, dots, underscores, or hyphens, and must start and end with a letter or digit"
        )
    return run_id


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
                    else {
                        "legacy_raw_score": True,
                        "computed_score": signal.computed_validation_score,
                    },
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


@dataclass(frozen=True)
class TaskCapsuleRetentionSkip:
    """One capsule directory that retention deliberately left untouched."""

    run_id: str
    reason: str

    def to_payload(self) -> dict[str, str]:
        return {"run_id": self.run_id, "reason": self.reason}


@dataclass(frozen=True)
class TaskCapsuleRetentionReport:
    """Structured result from one bounded task-capsule retention pass."""

    runs_dir: Path
    retention_count: int
    scanned_run_count: int
    completed_run_count: int
    retained_run_ids: tuple[str, ...] = ()
    deleted_run_ids: tuple[str, ...] = ()
    deleted_artifact_count: int = 0
    reclaimed_bytes: int = 0
    skipped: tuple[TaskCapsuleRetentionSkip, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "runs_dir": str(self.runs_dir),
            "retention_count": self.retention_count,
            "scanned_run_count": self.scanned_run_count,
            "completed_run_count": self.completed_run_count,
            "retained_run_ids": list(self.retained_run_ids),
            "deleted_run_ids": list(self.deleted_run_ids),
            "deleted_artifact_count": self.deleted_artifact_count,
            "reclaimed_bytes": self.reclaimed_bytes,
            "skipped": [item.to_payload() for item in self.skipped],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class _CompletedCapsule:
    run_id: str
    path: Path
    completed_at_ns: int
    artifact_fingerprints: tuple[tuple[object, ...], ...]
    root_lock_fingerprint: tuple[object, ...] | None


class _RetentionSkipError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
        self.run_id = validate_capsule_run_id(run_id)
        self.backend_name = backend
        self.path = runs_dir / self.run_id / "complete.mv2"
        self.backend: MemoryBackend | None = None

    def open(self) -> None:
        ensure_owner_only_directory(self.runs_dir)
        ensure_owner_only_directory(self.path.parent)
        backend_cls: type[MemoryBackend] = (
            MemvidBackend if self.backend_name == "memvid" else InMemoryBackend
        )
        self.backend = backend_cls(path=self.path, layer=MemoryLayer.EPISODIC)
        try:
            self.backend.open()
            if self.backend_name != "memvid":
                create_private_empty_file(self.path)
            self._harden_artifacts()
        except Exception:
            try:
                self.backend.close()
            finally:
                self.backend = None
            raise

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
        return backend.put(sanitize_memory_record(to_memory_record(frame)))

    def put_record(self, record: MemoryRecord) -> str:
        return self._backend().put(sanitize_memory_record(record))

    def seal(self) -> None:
        self._backend().seal()
        self._harden_artifacts()

    def close(self) -> None:
        error: BaseException | None = None
        try:
            if self.backend is not None:
                self.backend.close()
        except BaseException as exc:
            error = exc
        finally:
            self.backend = None
            try:
                self._harden_artifacts()
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

    def _backend(self) -> MemoryBackend:
        if self.backend is None:
            raise RuntimeError("TaskCapsuleWriter.open() must be called before use")
        return self.backend

    def _harden_artifacts(self) -> None:
        harden_memory_artifact_files(self.path)


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
    run_id = validate_capsule_run_id(run_id)
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
        safe_payload = redact_secrets(payload)
        payload = safe_payload if isinstance(safe_payload, dict) else {}
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
    finally:
        writer.close()
    _write_capsule_completion_marker(writer.path, run_id=run_id, backend=backend)
    return writer.path


def write_turn_capsule(
    *,
    runs_dir: Path,
    run_id: str,
    result: AgentTurnResult,
    backend: str = "memory",
    selected_context: str = "",
) -> Path:
    errors = tuple(
        execution.content for execution in result.tool_executions if not execution.success
    )
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


def enforce_task_capsule_retention(
    *,
    runs_dir: Path,
    retention_count: int = 1_000,
    preserve_run_ids: Iterable[str] = (),
) -> TaskCapsuleRetentionReport:
    """Keep a bounded number of safely identified completed task capsules.

    The pass is deliberately fail-closed. It never recursively removes arbitrary
    directories: a run directory must contain only the exact Kestrel capsule
    artifact set, have a durable completion marker (or a sealed legacy memory
    snapshot), and have both backend locks available for exclusive acquisition.
    """

    if isinstance(retention_count, bool) or retention_count < 1:
        raise ValueError("retention_count must be an integer greater than or equal to 1")
    runs_dir = Path(runs_dir)
    ensure_owner_only_directory(runs_dir)
    preserved = tuple(preserve_run_ids)
    with _exclusive_retention_pass(runs_dir):
        return _enforce_task_capsule_retention_locked(
            runs_dir=runs_dir,
            retention_count=retention_count,
            preserve_run_ids=preserved,
        )


def _enforce_task_capsule_retention_locked(
    *,
    runs_dir: Path,
    retention_count: int,
    preserve_run_ids: Iterable[str],
) -> TaskCapsuleRetentionReport:
    """Run one retention transaction while the root retention lock is held."""

    preserved = {validate_capsule_run_id(run_id) for run_id in preserve_run_ids}

    completed: list[_CompletedCapsule] = []
    skipped: list[TaskCapsuleRetentionSkip] = []
    scanned_run_count = 0
    for entry in sorted(runs_dir.iterdir(), key=lambda item: item.name):
        if _is_capsule_root_support_artifact(entry.name):
            continue
        try:
            entry_metadata = os.lstat(entry)
        except FileNotFoundError:
            continue
        try:
            run_id = validate_capsule_run_id(entry.name)
        except ValueError:
            if stat.S_ISDIR(entry_metadata.st_mode) or stat.S_ISLNK(entry_metadata.st_mode):
                skipped.append(TaskCapsuleRetentionSkip(entry.name, "unsafe_run_id"))
            continue
        scanned_run_count += 1
        if not stat.S_ISDIR(entry_metadata.st_mode):
            skipped.append(TaskCapsuleRetentionSkip(run_id, "unsafe_run_directory"))
            continue
        try:
            completed.append(_inspect_completed_capsule(runs_dir, run_id))
        except _RetentionSkipError as exc:
            skipped.append(TaskCapsuleRetentionSkip(run_id, exc.reason))

    newest_first = sorted(
        completed,
        key=lambda item: (item.completed_at_ns, item.run_id),
        reverse=True,
    )
    keep_run_ids = {item.run_id for item in newest_first if item.run_id in preserved}
    if newest_first:
        keep_run_ids.add(newest_first[0].run_id)
    target_keep_count = max(retention_count, len(keep_run_ids))
    for item in newest_first:
        if len(keep_run_ids) >= target_keep_count:
            break
        keep_run_ids.add(item.run_id)

    deleted_run_ids: list[str] = []
    deleted_artifact_count = 0
    reclaimed_bytes = 0
    warnings: list[str] = []
    for candidate in reversed(newest_first):
        if candidate.run_id in keep_run_ids:
            continue
        try:
            artifact_count, byte_count, cleanup_warnings = _delete_completed_capsule(
                runs_dir,
                candidate,
            )
        except _RetentionSkipError as exc:
            skipped.append(TaskCapsuleRetentionSkip(candidate.run_id, exc.reason))
            continue
        deleted_run_ids.append(candidate.run_id)
        deleted_artifact_count += artifact_count
        reclaimed_bytes += byte_count
        warnings.extend(cleanup_warnings)

    retained_run_ids = tuple(
        item.run_id for item in newest_first if item.run_id not in deleted_run_ids
    )
    return TaskCapsuleRetentionReport(
        runs_dir=runs_dir,
        retention_count=retention_count,
        scanned_run_count=scanned_run_count,
        completed_run_count=len(completed),
        retained_run_ids=retained_run_ids,
        deleted_run_ids=tuple(deleted_run_ids),
        deleted_artifact_count=deleted_artifact_count,
        reclaimed_bytes=reclaimed_bytes,
        skipped=tuple(sorted(skipped, key=lambda item: (item.run_id, item.reason))),
        warnings=tuple(warnings),
    )


def summarize_run_capsule(
    *,
    runs_dir: Path,
    run_id: str,
    backend: str = "memory",
) -> TaskCapsuleSummary:
    run_id = validate_capsule_run_id(run_id)
    ensure_owner_only_directory(runs_dir)
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
    validation_evidence = _capsule_validation_evidence(capsule.get("tool_calls"))
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
                validation_score=None if validation_evidence is not None else 0.78,
                validation_evidence=validation_evidence,
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
                validation_score=None if validation_evidence is not None else 0.82,
                validation_evidence=validation_evidence,
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
                metadata={
                    "run_id": run_id,
                    "capsule_signal": "policy_candidate",
                    "requires_human_review": True,
                },
                requested_target_layer=MemoryLayer.POLICY,
            )
        )
    return signals


def capsule_signal_staging_record(signal: LearningSignal) -> MemoryRecord | None:
    """Convert an unvalidated capsule candidate into explicit episodic staging.

    A task capsule is durable evidence, but its candidate text is not itself a
    validated stable-memory promotion.  Applying a capsule may therefore make
    facts, procedures, and corrections discoverable as untrusted episodic
    evidence while leaving semantic/procedural memory untouched.  A later
    ``memory.learn``/correction flow must bind authenticated validation receipts
    to the exact staged claim before it can enter a stable layer.
    """

    capsule_signal = str((signal.metadata or {}).get("capsule_signal") or "")
    requested_target = {
        "fact": MemoryLayer.SEMANTIC,
        "procedure": MemoryLayer.PROCEDURAL,
        "correction": MemoryLayer.SEMANTIC,
    }.get(capsule_signal)
    if requested_target is None:
        return None
    frame_type = {
        "fact": "section_summary",
        "procedure": "skill_card",
        "correction": "correction",
    }[capsule_signal]
    return MemoryRecord(
        title=signal.title,
        content=signal.content,
        layer=MemoryLayer.EPISODIC,
        kind=signal.kind,
        confidence=max(signal.confidence, 0.5),
        importance=signal.importance,
        tags={**(signal.tags or {}), "capsule_staging": "unvalidated"},
        metadata={
            **(signal.metadata or {}),
            "frame_type": frame_type,
            "capsule_apply_status": "unvalidated_episodic_staging",
            "actual_layer": MemoryLayer.EPISODIC.value,
            "requested_stable_layer": requested_target.value,
            "stable_recall_eligible": False,
            "validation_status": "unresolved",
        },
        evidence=[
            EvidenceRef(
                source="task_capsule",
                locator=signal.locator,
                quote="Unvalidated capsule candidate staged as episodic evidence.",
            )
        ],
    )


def _capsule_validation_evidence(raw_calls: object) -> ValidationEvidence | None:
    if not isinstance(raw_calls, list):
        return None
    buckets: dict[str, list[EvidenceRef]] = {
        "test_refs": [],
        "lint_refs": [],
        "repair_refs": [],
        "review_refs": [],
        "task_refs": [],
    }
    source_evidence_chars = 0
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict) or raw_call.get("success") is not True:
            continue
        data = raw_call.get("data")
        if not isinstance(data, dict):
            continue
        payloads: list[object] = [
            data.get("validation_evidence"),
            data.get("runtime_validation_evidence"),
        ]
        validation = data.get("validation")
        if isinstance(validation, dict):
            payloads.append(validation.get("validation_evidence"))
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            for bucket in buckets:
                raw_refs = payload.get(bucket)
                if not isinstance(raw_refs, list):
                    continue
                for raw_ref in raw_refs:
                    if not isinstance(raw_ref, dict):
                        continue
                    source = str(raw_ref.get("source") or "").strip()
                    locator = str(raw_ref.get("locator") or "").strip()
                    if source != "memory_record" or not locator:
                        continue
                    quote = raw_ref.get("quote")
                    ref = EvidenceRef(
                        source=source,
                        locator=locator,
                        quote=str(quote) if quote is not None else None,
                    )
                    if ref not in buckets[bucket]:
                        buckets[bucket].append(ref)
            raw_chars = payload.get("source_evidence_chars")
            if isinstance(raw_chars, int) and not isinstance(raw_chars, bool):
                source_evidence_chars += max(raw_chars, 0)
    if not any(buckets.values()):
        return None
    return ValidationEvidence(
        test_refs=tuple(buckets["test_refs"]),
        lint_refs=tuple(buckets["lint_refs"]),
        repair_refs=tuple(buckets["repair_refs"]),
        review_refs=tuple(buckets["review_refs"]),
        task_refs=tuple(buckets["task_refs"]),
        source_evidence_chars=source_evidence_chars or None,
    )


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


def _write_capsule_completion_marker(path: Path, *, run_id: str, backend: str) -> None:
    artifact_names: list[str] = []
    for name in sorted(_CAPSULE_DATA_ARTIFACTS):
        candidate = path.parent / name
        if harden_private_file(candidate, missing_ok=True):
            artifact_names.append(name)
    if "complete.mv2" not in artifact_names:
        raise RuntimeError(f"Task capsule did not materialize its canonical artifact: {path}")
    payload = {
        "format": _COMPLETION_MARKER_FORMAT,
        "version": 1,
        "status": "complete",
        "run_id": run_id,
        "backend": backend,
        "artifacts": artifact_names,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    marker_path = path.parent / _CAPSULE_COMPLETION_MARKER
    write_private_text(marker_path, json.dumps(payload, sort_keys=True), encoding="utf-8")
    harden_private_file(marker_path)


def _inspect_completed_capsule(runs_dir: Path, run_id: str) -> _CompletedCapsule:
    run_path = runs_dir / run_id
    try:
        directory_metadata = _safe_directory_metadata(run_path)
        with _exclusive_capsule_locks(runs_dir, run_id):
            current_directory_metadata = _safe_directory_metadata(run_path)
            if not os.path.samestat(directory_metadata, current_directory_metadata):
                raise _RetentionSkipError("capsule_changed_during_scan")
            return _inspect_locked_completed_capsule(runs_dir, run_id)
    except FileNotFoundError as exc:
        # Another safe retention pass may have quarantined this capsule after
        # our directory listing. Treat disappearance as a skipped candidate,
        # never as a failed run-maintenance event.
        raise _RetentionSkipError("capsule_changed_during_scan") from exc


def _inspect_locked_completed_capsule(runs_dir: Path, run_id: str) -> _CompletedCapsule:
    run_path = runs_dir / run_id
    try:
        artifact_names = set(os.listdir(run_path))
    except FileNotFoundError as exc:
        raise _RetentionSkipError("capsule_changed_during_scan") from exc
    unknown_names = artifact_names - _CAPSULE_KNOWN_ARTIFACTS
    if unknown_names:
        raise _RetentionSkipError("unknown_capsule_artifact")
    if "complete.mv2" not in artifact_names or _CAPSULE_LAYER_LOCK not in artifact_names:
        raise _RetentionSkipError("partial_capsule")

    metadata_by_name: dict[str, os.stat_result] = {}
    for name in sorted(artifact_names):
        metadata_by_name[name] = _safe_regular_metadata(run_path / name)

    marker_metadata = metadata_by_name.get(_CAPSULE_COMPLETION_MARKER)
    if marker_metadata is not None:
        completed_at_ns = _validated_marker_completion_time(
            run_path,
            run_id=run_id,
            metadata_by_name=metadata_by_name,
        )
    elif _legacy_memory_snapshot_is_complete(
        run_path,
        run_id=run_id,
        metadata_by_name=metadata_by_name,
    ):
        completed_at_ns = metadata_by_name["complete.memory.json"].st_mtime_ns
    else:
        raise _RetentionSkipError("partial_capsule")

    root_lock_path = _capsule_root_lock_path(runs_dir, run_id)
    try:
        root_lock_metadata = _safe_regular_metadata(root_lock_path)
    except FileNotFoundError:
        root_lock_metadata = None
    return _CompletedCapsule(
        run_id=run_id,
        path=run_path,
        completed_at_ns=completed_at_ns,
        artifact_fingerprints=tuple(
            _artifact_fingerprint(name, metadata_by_name[name]) for name in sorted(metadata_by_name)
        ),
        root_lock_fingerprint=(
            _artifact_fingerprint(root_lock_path.name, root_lock_metadata)
            if root_lock_metadata is not None
            else None
        ),
    )


def _validated_marker_completion_time(
    run_path: Path,
    *,
    run_id: str,
    metadata_by_name: dict[str, os.stat_result],
) -> int:
    marker_metadata = metadata_by_name[_CAPSULE_COMPLETION_MARKER]
    raw = _read_unchanged_regular_text(
        run_path / _CAPSULE_COMPLETION_MARKER,
        marker_metadata,
        max_bytes=32_768,
    )
    try:
        marker = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _RetentionSkipError("partial_capsule") from exc
    if not isinstance(marker, dict):
        raise _RetentionSkipError("partial_capsule")
    expected_artifacts = sorted(
        name for name in metadata_by_name if name in _CAPSULE_DATA_ARTIFACTS
    )
    if (
        marker.get("format") != _COMPLETION_MARKER_FORMAT
        or marker.get("version") != 1
        or marker.get("status") != "complete"
        or marker.get("run_id") != run_id
        or marker.get("artifacts") != expected_artifacts
    ):
        raise _RetentionSkipError("partial_capsule")
    newest_data_write = max(metadata_by_name[name].st_mtime_ns for name in expected_artifacts)
    if marker_metadata.st_mtime_ns < newest_data_write:
        raise _RetentionSkipError("partial_capsule")
    return marker_metadata.st_mtime_ns


def _legacy_memory_snapshot_is_complete(
    run_path: Path,
    *,
    run_id: str,
    metadata_by_name: dict[str, os.stat_result],
) -> bool:
    snapshot_metadata = metadata_by_name.get("complete.memory.json")
    if snapshot_metadata is None:
        return False
    try:
        raw = _read_unchanged_regular_text(
            run_path / "complete.memory.json",
            snapshot_metadata,
            max_bytes=_MAX_LEGACY_CAPSULE_INDEX_BYTES,
        )
        loaded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, _RetentionSkipError):
        return False
    if not isinstance(loaded, list) or not all(isinstance(item, dict) for item in loaded):
        return False
    records = [item for item in loaded if isinstance(item, dict)]
    root = next((item for item in records if item.get("id") == f"capsule_{run_id}"), None)
    if root is None:
        return False
    metadata = root.get("metadata")
    tags = root.get("tags")
    if (
        not isinstance(metadata, dict)
        or metadata.get("capsule_artifact") is not True
        or metadata.get("permanent_layer") is not False
        or not isinstance(tags, dict)
        or tags.get("capsule") != "complete"
        or tags.get("run_id") != run_id
    ):
        return False
    content = root.get("content")
    if not isinstance(content, str):
        return False
    payload = _json_object_from_text(content)
    if payload is None or payload.get("run_id") != run_id:
        return False
    child_ids = metadata.get("child_ids", [])
    if not isinstance(child_ids, list) or not all(isinstance(item, str) for item in child_ids):
        return False
    record_ids = {item.get("id") for item in records}
    return all(child_id in record_ids for child_id in child_ids)


@contextmanager
def _exclusive_capsule_locks(runs_dir: Path, run_id: str) -> Iterator[None]:
    lock_paths: list[Path] = []
    root_lock_path = _capsule_root_lock_path(runs_dir, run_id)
    try:
        os.lstat(root_lock_path)
    except FileNotFoundError:
        pass
    else:
        lock_paths.append(root_lock_path)
    layer_lock_path = runs_dir / run_id / _CAPSULE_LAYER_LOCK
    try:
        os.lstat(layer_lock_path)
    except FileNotFoundError as exc:
        raise _RetentionSkipError("partial_capsule") from exc
    lock_paths.append(layer_lock_path)

    handles: list[IO[str]] = []
    try:
        for lock_path in lock_paths:
            handle = _open_existing_safe_lock(lock_path)
            try:
                lock_exclusive(handle, blocking=False)
            except OSError as exc:
                handle.close()
                raise _RetentionSkipError("active_capsule") from exc
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            try:
                unlock(handle)
            finally:
                handle.close()


def _open_existing_safe_lock(path: Path) -> IO[str]:
    try:
        before_open = _safe_regular_metadata(path)
    except FileNotFoundError as exc:
        raise _RetentionSkipError("capsule_changed_during_scan") from exc
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _RetentionSkipError("unsafe_capsule_artifact") from exc
    try:
        opened = os.fstat(descriptor)
        try:
            after_open = _safe_regular_metadata(path)
        except FileNotFoundError as exc:
            raise _RetentionSkipError("capsule_changed_during_scan") from exc
        _require_safe_regular_metadata(opened)
        if not os.path.samestat(before_open, opened) or not os.path.samestat(opened, after_open):
            raise _RetentionSkipError("capsule_changed_during_scan")
        return os.fdopen(descriptor, "r+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def _delete_completed_capsule(
    runs_dir: Path,
    candidate: _CompletedCapsule,
) -> tuple[int, int, tuple[str, ...]]:
    quarantine = runs_dir / f".kestrel-capsule-retention-{uuid4().hex}"
    refreshed: _CompletedCapsule
    with _exclusive_capsule_locks(runs_dir, candidate.run_id):
        refreshed = _inspect_locked_completed_capsule(runs_dir, candidate.run_id)
        if (
            refreshed.artifact_fingerprints != candidate.artifact_fingerprints
            or refreshed.root_lock_fingerprint != candidate.root_lock_fingerprint
        ):
            raise _RetentionSkipError("capsule_changed_during_retention")
        try:
            os.replace(candidate.path, quarantine)
        except OSError as exc:
            raise _RetentionSkipError("capsule_deletion_failed") from exc

    artifact_count = 0
    reclaimed_bytes = 0
    try:
        current_names = set(os.listdir(quarantine))
        expected_names = {str(item[0]) for item in refreshed.artifact_fingerprints}
        if current_names != expected_names:
            raise _RetentionSkipError("capsule_changed_during_retention")
        fingerprints_by_name = {str(item[0]): item for item in refreshed.artifact_fingerprints}
        for name in sorted(current_names):
            artifact_path = quarantine / name
            metadata = _safe_regular_metadata(artifact_path)
            if _artifact_fingerprint(name, metadata) != fingerprints_by_name[name]:
                raise _RetentionSkipError("capsule_changed_during_retention")
            artifact_path.unlink()
            artifact_count += 1
            reclaimed_bytes += metadata.st_size
        quarantine.rmdir()
    except Exception as exc:
        if not candidate.path.exists() and quarantine.exists():
            try:
                os.replace(quarantine, candidate.path)
            except OSError:
                pass
        if isinstance(exc, _RetentionSkipError):
            raise
        raise _RetentionSkipError("capsule_deletion_failed") from exc

    warnings: list[str] = []
    if refreshed.root_lock_fingerprint is not None:
        root_lock_path = _capsule_root_lock_path(runs_dir, candidate.run_id)
        try:
            metadata = _safe_regular_metadata(root_lock_path)
            if (
                _artifact_fingerprint(root_lock_path.name, metadata)
                != refreshed.root_lock_fingerprint
            ):
                warnings.append(f"{candidate.run_id}:root_lock_changed")
            else:
                root_lock_path.unlink()
                artifact_count += 1
                reclaimed_bytes += metadata.st_size
        except FileNotFoundError:
            pass
        except (OSError, _RetentionSkipError):
            warnings.append(f"{candidate.run_id}:root_lock_cleanup_failed")
    return artifact_count, reclaimed_bytes, tuple(warnings)


def _read_unchanged_regular_text(
    path: Path,
    expected_metadata: os.stat_result,
    *,
    max_bytes: int,
) -> str:
    if expected_metadata.st_size > max_bytes:
        raise _RetentionSkipError("capsule_index_too_large")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _RetentionSkipError("unsafe_capsule_artifact") from exc
    try:
        opened = os.fstat(descriptor)
        after_open = _safe_regular_metadata(path)
        _require_safe_regular_metadata(opened)
        if not os.path.samestat(expected_metadata, opened) or not os.path.samestat(
            opened, after_open
        ):
            raise _RetentionSkipError("capsule_changed_during_scan")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read(max_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _safe_directory_metadata(path: Path) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise _RetentionSkipError("capsule_changed_during_scan") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _RetentionSkipError("unsafe_run_directory")
    _require_current_owner(metadata)
    return metadata


def _safe_regular_metadata(path: Path) -> os.stat_result:
    metadata = os.lstat(path)
    _require_safe_regular_metadata(metadata)
    return metadata


def _require_safe_regular_metadata(metadata: os.stat_result) -> None:
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise _RetentionSkipError("unsafe_capsule_artifact")
    _require_current_owner(metadata)


def _require_current_owner(metadata: os.stat_result) -> None:
    geteuid = getattr(os, "geteuid", None)
    if os.name != "nt" and callable(geteuid) and metadata.st_uid != geteuid():
        raise _RetentionSkipError("unsafe_capsule_owner")


def _artifact_fingerprint(name: str, metadata: os.stat_result) -> tuple[object, ...]:
    return (
        name,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _capsule_root_lock_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / f".{run_id}.kestrel-memory.lock"


@contextmanager
def _exclusive_retention_pass(runs_dir: Path) -> Iterator[None]:
    """Serialize complete scan-and-delete passes across threads and processes."""

    descriptor = open_private_file_descriptor(runs_dir / _CAPSULE_RETENTION_LOCK)
    try:
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise
    acquired = False
    try:
        lock_exclusive(handle)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                unlock(handle)
        finally:
            handle.close()


def _is_capsule_root_support_artifact(name: str) -> bool:
    if name == _CAPSULE_RETENTION_LOCK:
        return True
    suffix = ".kestrel-memory.lock"
    if not name.startswith(".") or not name.endswith(suffix):
        return False
    run_id = name[1 : -len(suffix)]
    try:
        validate_capsule_run_id(run_id)
    except ValueError:
        return False
    return True


def _load_capsule_payload(path: Path, *, backend: str) -> dict[str, object]:
    harden_task_capsule_run(path.parent)
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
        iter_records = getattr(capsule_backend, "iter_records", None)
        if callable(iter_records):
            indexed_records = iter_records()
            if isinstance(indexed_records, Iterable):
                for record in indexed_records:
                    if not isinstance(record, MemoryRecord):
                        continue
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
        capsule_backend: MemoryBackend = MemvidBackend(
            path=path, layer=MemoryLayer.EPISODIC, read_only=True
        )
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
    return "\n\n".join(
        f"[{hit.record.layer.value}] {hit.record.title}\n{hit.snippet or hit.record.content}"
        for hit in hits
    )
