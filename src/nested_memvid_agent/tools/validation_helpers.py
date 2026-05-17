from __future__ import annotations

from typing import Any

from ..models import EvidenceRef
from ..nested_learning import ValidationEvidence


def _tool_validation_evidence_payload(
    bucket: str,
    source: str,
    command: list[str],
    content: str,
    success: bool,
) -> dict[str, object]:
    if not success:
        return {"source_evidence_chars": len(content)}
    return {
        bucket: [
            {
                "source": source,
                "locator": " ".join(command),
                "quote": content[:240],
            }
        ],
        "source_evidence_chars": len(content),
    }


def _validation_evidence_arg(arguments: dict[str, Any]) -> ValidationEvidence | None:
    raw = arguments.get("validation_evidence")
    if not isinstance(raw, dict):
        return None
    return ValidationEvidence(
        test_refs=tuple(_evidence_refs_arg(raw.get("test_refs"))),
        lint_refs=tuple(_evidence_refs_arg(raw.get("lint_refs"))),
        repair_refs=tuple(_evidence_refs_arg(raw.get("repair_refs"))),
        review_refs=tuple(_evidence_refs_arg(raw.get("review_refs"))),
        task_refs=tuple(_evidence_refs_arg(raw.get("task_refs"))),
        human_explicit=bool(
            raw.get("human_explicit", arguments.get("explicit_instruction", False))
        ),
        source_evidence_chars=_optional_int(raw.get("source_evidence_chars")),
    )


def _validation_evidence_payload_for_output(
    evidence: ValidationEvidence | None,
    validation_score: float,
) -> dict[str, object]:
    if evidence is None:
        return {"legacy_raw_score": True, "computed_score": validation_score}
    return evidence.to_metadata()


def _merge_validation_evidence_payloads(*payloads: object) -> dict[str, object]:
    merged: dict[str, list[object]] = {
        "test_refs": [],
        "lint_refs": [],
        "repair_refs": [],
        "review_refs": [],
        "task_refs": [],
    }
    source_chars = 0
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("test_refs", "lint_refs", "repair_refs", "review_refs", "task_refs"):
            values = payload.get(key)
            if isinstance(values, list):
                merged[key].extend(values)
        raw_chars = payload.get("source_evidence_chars")
        if isinstance(raw_chars, int):
            source_chars += raw_chars
    if source_chars:
        return {**merged, "source_evidence_chars": source_chars}
    return dict(merged)


def _evidence_refs_arg(value: object) -> list[EvidenceRef]:
    if not isinstance(value, list):
        return []
    refs: list[EvidenceRef] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        locator = str(item.get("locator", "")).strip()
        if source and locator:
            quote = item.get("quote")
            refs.append(
                EvidenceRef(
                    source=source, locator=locator, quote=str(quote) if quote is not None else None
                )
            )
    return refs


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, (str, float)):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None
