from __future__ import annotations

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
        return self._retrieve_lessons(query=query, k=k)

    def recall_failure(
        self,
        *,
        classification: FailureClassification,
        failure_text: str,
        k: int = 5,
    ) -> list[dict[str, Any]]:
        return self._retrieve_lessons(query=f"{classification.category} {failure_text}", k=k)

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
        return lesson, self.memory.put(lesson.to_memory_record())

    def _retrieve_lessons(self, *, query: str, k: int) -> list[dict[str, Any]]:
        hits = self.memory.retrieve(
            RetrievalQuery(
                query=query,
                layers=(MemoryLayer.PROCEDURAL, MemoryLayer.EPISODIC),
                k_per_layer=max(1, min(k, 10)),
            )
        )
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
