from __future__ import annotations

import json
from typing import Any

from ..diagnosis import classify_failure
from ..models import MemoryLayer, RetrievalQuery
from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from .base import AgentTool, ToolContext


class DiagnosisClassifyTool(AgentTool):
    spec = ToolSpec(
        name="diagnosis.classify",
        description="Classify a runtime/tool/test/provider failure and return the matching diagnostic playbook.",
        parameters={
            "type": "object",
            "properties": {
                "failure_text": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["failure_text"],
        },
        capabilities=("self-diagnosis", "failure-classification"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del context
        call = ToolCall(name=self.spec.name, arguments=arguments)
        failure_text = str(arguments.get("failure_text", "")).strip()
        if not failure_text:
            return self._result(call, success=False, content="Missing failure_text", error="missing_failure_text")
        classification = classify_failure(failure_text, source=str(arguments.get("source", "")))
        payload = classification.to_payload()
        content = json.dumps(payload, indent=2)
        return self._result(call, success=True, content=content, data=payload)


class DiagnosisRecallTool(AgentTool):
    spec = ToolSpec(
        name="diagnosis.recall",
        description="Classify a failure and retrieve similar prior failure lessons from procedural/episodic memory before retrying.",
        parameters={
            "type": "object",
            "properties": {
                "failure_text": {"type": "string"},
                "source": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["failure_text"],
        },
        capabilities=("self-diagnosis", "failure-recall", "nested-memory"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        failure_text = str(arguments.get("failure_text", "")).strip()
        if not failure_text:
            return self._result(call, success=False, content="Missing failure_text", error="missing_failure_text")
        classification = classify_failure(failure_text, source=str(arguments.get("source", "")))
        k = max(1, min(int(arguments.get("k", 5)), 10))
        recall = _recall_failure_lessons(context, classification.category, failure_text, k)
        payload = {
            **classification.to_payload(),
            **recall,
        }
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


def _recall_failure_lessons(context: ToolContext, category: str, failure_text: str, k: int) -> dict[str, Any]:
    query = f"{category} {failure_text}"
    hits = context.memory.retrieve(
        RetrievalQuery(
            query=query,
            layers=(MemoryLayer.PROCEDURAL, MemoryLayer.EPISODIC, MemoryLayer.WORKING),
            k_per_layer=k,
        )
    )
    rows = []
    for hit in hits[:k]:
        rows.append(
            {
                "layer": hit.record.layer.value,
                "kind": hit.record.kind.value,
                "title": hit.record.title,
                "score": hit.score,
                "snippet": hit.snippet or hit.record.content[:500],
            }
        )
    return {
        "query": query,
        "hits": rows,
        "retry_guidance": {
            "must_change_strategy_before_retry": bool(rows),
            "reason": "Similar prior failures were found; use recalled lessons before repeating the action."
            if rows
            else "No prior lesson found; follow the diagnostic playbook and record validated findings.",
        },
    }


def _recall_hit_titles(recall: dict[str, Any]) -> tuple[str, ...]:
    hits = recall.get("hits", [])
    if not isinstance(hits, list):
        return ()
    return tuple(str(hit.get("title", "")) for hit in hits if isinstance(hit, dict))
