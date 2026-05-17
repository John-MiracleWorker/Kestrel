from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Any

from ..diagnosis import FailureClassification
from ..layers import LayeredMemorySystem
from ..models import MemoryLayer, RetrievalQuery
from ..runtime_models import StrategyProposal, ToolExecution
from .models import FailureEpisode, LessonCard


class LessonManager:
    def __init__(self, memory: LayeredMemorySystem) -> None:
        self.memory = memory

    def preflight(
        self,
        *,
        objective: str,
        expected_tools: tuple[str, ...] = (),
        k: int = 5,
    ) -> list[dict[str, Any]]:
        query = " ".join(["lesson failure", objective, *expected_tools]).strip()
        if not query:
            return []
        return self._retrieve_lessons_semantic(query=query, k=k, include_episodic=True)

    def recall_failure(
        self,
        *,
        classification: FailureClassification,
        failure_text: str,
        k: int = 5,
    ) -> list[dict[str, Any]]:
        return self._retrieve_lessons_semantic(query=f"{classification.category} {failure_text}", k=k, include_episodic=True)

    def record_failure(
        self,
        *,
        run_id: str,
        execution: ToolExecution,
        classification: FailureClassification,
        recall_hits: list[dict[str, Any]],
        attempted_strategy: str,
    ) -> tuple[FailureEpisode, str]:
        episode = FailureEpisode.from_tool_failure(
            run_id=run_id,
            execution=execution,
            category=classification.category,
            diagnosis=str(classification.playbook.get("name", classification.category)),
            attempted_strategy=attempted_strategy,
            similar_lessons_used=tuple(str(hit.get("id") or hit.get("title") or "") for hit in recall_hits if hit),
        )
        return episode, self.memory.put(episode.to_memory_record())

    def write_lesson_from_resolution(
        self,
        *,
        failure: FailureEpisode,
        validation: ToolExecution,
        strategy: StrategyProposal,
    ) -> tuple[LessonCard, str]:
        lesson = LessonCard.from_resolution(failure=failure, validation=validation, strategy=strategy)
        existing = self.find_existing_lesson(
            category=lesson.failure_category,
            corrected_strategy=lesson.corrected_strategy,
            failure_signature=lesson.failure_signature,
        )
        if existing is not None:
            evidence_refs = tuple(dict.fromkeys((*existing.evidence_refs, *lesson.evidence_refs)))
            lesson = replace(
                existing,
                failure_signature=lesson.failure_signature,
                context=lesson.context,
                root_cause=lesson.root_cause,
                bad_strategy=lesson.bad_strategy,
                corrected_strategy=lesson.corrected_strategy,
                validation_command=lesson.validation_command,
                evidence_refs=evidence_refs,
                success_count=existing.success_count + 1,
                failure_count=existing.failure_count + 1,
                confidence=max(existing.confidence, lesson.confidence),
                updated_at=datetime.now(UTC).isoformat(),
            )
            return lesson, self.memory.upsert(lesson.to_memory_record())
        return lesson, self.memory.upsert(lesson.to_memory_record())

    def find_existing_lesson(
        self,
        *,
        category: str,
        corrected_strategy: str,
        failure_signature: str = "",
    ) -> LessonCard | None:
        best: tuple[float, LessonCard] | None = None
        candidates = self._retrieve_lessons_semantic(
            query=_lesson_query(category=category, corrected_strategy=corrected_strategy, failure_signature=failure_signature),
            k=10,
            include_episodic=False,
        )
        candidate_ids = {str(item.get("id", "")) for item in candidates if item.get("id")}
        records = [
            record
            for record in self.memory.iter_records(MemoryLayer.PROCEDURAL)
            if not candidate_ids or record.id in candidate_ids
        ]
        for record in records:
            if str(record.metadata.get("cognition_schema", "")) != "lesson_card.v1":
                continue
            try:
                lesson = LessonCard.from_memory_record(record)
            except Exception:  # nosec B112
                continue
            if lesson.failure_category != category:
                continue
            similarity = SequenceMatcher(
                None,
                _normalize_strategy(lesson.corrected_strategy),
                _normalize_strategy(corrected_strategy),
            ).ratio()
            signature_similarity = SequenceMatcher(
                None,
                _normalize_signature(lesson.failure_signature),
                _normalize_signature(failure_signature),
            ).ratio()
            matches = similarity >= 0.85 or (signature_similarity >= 0.75 and similarity >= 0.60)
            score = max(similarity, (signature_similarity + similarity) / 2)
            if matches and (best is None or score > best[0]):
                best = (score, lesson)
        return None if best is None else best[1]

    def _retrieve_lessons_semantic(
        self,
        *,
        query: str,
        k: int,
        include_episodic: bool = False,
    ) -> list[dict[str, Any]]:
        layers = (MemoryLayer.PROCEDURAL, MemoryLayer.EPISODIC) if include_episodic else (MemoryLayer.PROCEDURAL,)
        hits = self.memory.retrieve(
            RetrievalQuery(
                query=query,
                layers=layers,
                k_per_layer=max(1, min(k, 10)),
                mode="hybrid",
            )
        )
        rows = self._lesson_rows_from_hits(hits, k=k)
        if len(rows) >= k:
            return rows[:k]

        seen = {str(row.get("id")) for row in rows}
        for layer in layers:
            for record in self.memory.iter_records(layer):
                if record.id in seen:
                    continue
                schema = str(record.metadata.get("cognition_schema", ""))
                if schema not in {"lesson_card.v1", "failure_episode.v1"}:
                    continue
                rows.append(
                    {
                        "id": record.id,
                        "layer": record.layer.value,
                        "kind": record.kind.value,
                        "title": record.title,
                        "score": 0.0,
                        "schema": schema,
                        "snippet": record.content[:500],
                    }
                )
                seen.add(record.id)
                if len(rows) >= k:
                    return rows
        return rows[:k]

    def _retrieve_lessons(self, *, query: str, k: int) -> list[dict[str, Any]]:
        hits = self.memory.retrieve(
            RetrievalQuery(
                query=query,
                layers=(MemoryLayer.PROCEDURAL, MemoryLayer.EPISODIC),
                k_per_layer=max(1, min(k, 10)),
            )
        )
        return self._lesson_rows_from_hits(hits, k=k)

    def _lesson_rows_from_hits(self, hits: list[Any], *, k: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for hit in hits[:k]:
            schema = str(hit.record.metadata.get("cognition_schema", ""))
            if schema and schema not in {"lesson_card.v1", "failure_episode.v1"}:
                continue
            if not schema and hit.record.kind.value not in {"failure", "procedure", "summary", "event"}:
                continue
            rows.append(
                {
                    "id": hit.record.id,
                    "layer": hit.record.layer.value,
                    "kind": hit.record.kind.value,
                    "title": hit.record.title,
                    "score": hit.score,
                    "schema": schema,
                    "snippet": hit.snippet or hit.record.content[:500],
                }
            )
        return rows


def _normalize_strategy(text: str) -> str:
    replacements = {
        "credential": "auth",
        "credentials": "auth",
        "token": "auth",
        "renew": "refresh",
        "rerun": "retry",
        "again": "retry",
    }
    tokens = []
    for raw in text.lower().replace(".", " ").split():
        token = raw.strip()
        if token.endswith("ing") and len(token) > 5:
            token = token[:-3]
            if len(token) >= 2 and token[-1] == token[-2]:
                token = token[:-1]
        elif token.endswith("s") and len(token) > 4:
            token = token[:-1]
        tokens.append(replacements.get(token, token))
    return " ".join(sorted(tokens))


def _normalize_signature(text: str) -> str:
    replacements = {
        "401": "auth",
        "403": "auth",
        "credential": "auth",
        "credentials": "auth",
        "token": "auth",
        "expired": "stale",
        "rejected": "denied",
        "unauthorized": "auth",
    }
    tokens = []
    for raw in text.lower().replace(".", " ").replace(":", " ").split():
        token = raw.strip()
        tokens.append(replacements.get(token, token))
    return " ".join(sorted(tokens))


def _lesson_query(*, category: str, corrected_strategy: str, failure_signature: str) -> str:
    category_hash = sha256(category.encode("utf-8")).hexdigest()[:12]
    return " ".join(
        part
        for part in (
            "lesson failure",
            category,
            corrected_strategy,
            failure_signature,
            category_hash,
        )
        if part
    )
