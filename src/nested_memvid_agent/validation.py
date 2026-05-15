from __future__ import annotations

from dataclasses import dataclass

from .layers import LayeredMemorySystem
from .models import MemoryLayer, RetrievalQuery


@dataclass(frozen=True)
class GoldenQuestion:
    name: str
    query: str
    expected_terms: tuple[str, ...]
    layers: tuple[MemoryLayer, ...] = tuple(MemoryLayer)
    min_hits: int = 1


@dataclass(frozen=True)
class ValidationResult:
    name: str
    passed: bool
    reason: str
    hit_count: int


class RetrievalValidator:
    def __init__(self, memory: LayeredMemorySystem) -> None:
        self.memory = memory

    def run(self, questions: list[GoldenQuestion]) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        for question in questions:
            hits = self.memory.retrieve(
                RetrievalQuery(query=question.query, layers=question.layers, k_per_layer=10)
            )
            text = "\n".join(f"{hit.record.title}\n{hit.record.content}\n{hit.snippet or ''}" for hit in hits).lower()
            missing = [term for term in question.expected_terms if term.lower() not in text]
            passed = len(hits) >= question.min_hits and not missing
            reason = "ok" if passed else f"missing={missing}, hits={len(hits)}"
            results.append(
                ValidationResult(
                    name=question.name,
                    passed=passed,
                    reason=reason,
                    hit_count=len(hits),
                )
            )
        return results


def summarize_validation(results: list[ValidationResult]) -> str:
    passed = sum(1 for result in results if result.passed)
    total = len(results)
    lines = [f"Validation: {passed}/{total} passed"]
    for result in results:
        marker = "PASS" if result.passed else "FAIL"
        lines.append(f"- {marker} {result.name}: {result.reason}")
    return "\n".join(lines)
