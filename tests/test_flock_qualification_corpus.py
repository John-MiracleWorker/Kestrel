"""Shipped deterministic Flock qualification corpus (Adaptive Flock plan, Task 4)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from importlib import resources
from pathlib import Path

import pytest

from nested_memvid_agent.routing.qualification_corpus import (
    EXPECTED_V1_CORPUS_DIGEST,
    FixtureAttempt,
    evaluate_scope,
    fixture_only_attempts,
    load_qualification_corpus,
    load_shipped_qualification_corpus,
)
from nested_memvid_agent.routing.qualification_models import CorpusItem, CorpusManifest

_FIXTURE_IDS = {
    "routing_guardrails_v1",
    "cost_accounting_v1",
    "abstention_v1",
}


def _shipped_root() -> Path:
    root = resources.files("nested_memvid_agent") / "qualification_fixtures" / "v1"
    assert isinstance(root, Path)
    return root


def _copy_fixture_tree(tmp_path: Path) -> Path:
    dest = tmp_path / "v1"
    dest.mkdir()
    for entry in _shipped_root().iterdir():
        (dest / entry.name).write_bytes(entry.read_bytes())
    return dest


def _rewrite_fixture(dest: Path, fixture_id: str, mutate: object) -> None:
    """Rewrite one fixture file through *mutate* and rebind its manifest digest."""
    manifest_path = dest / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    file_name = manifest["fixtures"][fixture_id]["file"]
    payload = json.loads((dest / file_name).read_text(encoding="utf-8"))
    assert callable(mutate)
    mutate(payload)
    new_bytes = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    (dest / file_name).write_bytes(new_bytes)
    manifest["fixtures"][fixture_id]["sha256"] = hashlib.sha256(new_bytes).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_shipped_fixture_manifest_is_complete_and_digest_bound() -> None:
    corpus = load_shipped_qualification_corpus()
    assert {item.fixture_id for item in corpus.items} == {
        "routing_guardrails_v1",
        "cost_accounting_v1",
        "abstention_v1",
    }
    assert corpus.digest == EXPECTED_V1_CORPUS_DIGEST


def test_fixture_evidence_cannot_claim_live_provider_qualification() -> None:
    result = evaluate_scope(fixture_only_attempts())
    assert result.qualified is False
    assert "real_project_evidence_required" in result.reasons


def test_fixture_items_are_immutable_synthetic_corpus_items() -> None:
    corpus = load_shipped_qualification_corpus()

    assert isinstance(corpus.manifest, CorpusManifest)
    assert corpus.manifest.schema_version == 1
    assert corpus.manifest.items == tuple(fixture.to_corpus_item() for fixture in corpus.items)
    for fixture in corpus.items:
        assert fixture.schema_version == 1
        assert fixture.fixture_id in _FIXTURE_IDS
        assert fixture.task_contract["prompt"].strip()
        assert fixture.risk in ("low", "medium", "high")
        assert fixture.capabilities
        assert fixture.file_digest == hashlib.sha256(
            (_shipped_root() / fixture.file_name).read_bytes()
        ).hexdigest()
        item = fixture.to_corpus_item()
        assert isinstance(item, CorpusItem)
        assert item.item_id == fixture.fixture_id
        assert item.evidence_kind == "synthetic"
        assert item.actionable is True
        assert item.exclusion_reasons == ()
        with pytest.raises(dataclasses.FrozenInstanceError):
            fixture.fixture_id = "mutated"  # type: ignore[misc]


def test_fixture_loading_is_deterministic_across_loads() -> None:
    first = load_shipped_qualification_corpus()
    second = load_shipped_qualification_corpus()

    assert first.digest == second.digest
    assert fixture_only_attempts(first) == fixture_only_attempts(second)
    for attempt in fixture_only_attempts(first):
        assert attempt.evidence_kind == "synthetic"
        assert attempt.violations == ()


def test_fixture_replay_covers_targets_outcomes_and_cap_accounting() -> None:
    corpus = load_shipped_qualification_corpus()
    attempts = fixture_only_attempts(corpus)
    by_fixture: dict[str, list[FixtureAttempt]] = {}
    for attempt in attempts:
        by_fixture.setdefault(attempt.fixture_id, []).append(attempt)

    assert set(by_fixture) == _FIXTURE_IDS

    guardrail = by_fixture["routing_guardrails_v1"]
    assert len(guardrail) == 20
    assert {attempt.target_id for attempt in guardrail} == {
        "fixture_guardrail_primary",
        "fixture_guardrail_secondary",
    }
    assert any(attempt.outcome == "failure" for attempt in guardrail)
    assert all(
        attempt.outcome != "failure" or attempt.category == "provider_timeout"
        for attempt in guardrail
    )

    cost = by_fixture["cost_accounting_v1"]
    assert len(cost) == 20
    assert all(attempt.outcome == "success" for attempt in cost)
    assert sum(attempt.cost_micros for attempt in cost) == 2200

    abstention = by_fixture["abstention_v1"]
    assert len(abstention) == 20
    assert all(attempt.outcome == "abstention" for attempt in abstention)
    assert {attempt.category for attempt in abstention} == {"insufficient_confidence"}


def test_evaluate_scope_rejects_empty_and_violating_attempts() -> None:
    empty = evaluate_scope(())
    assert empty.qualified is False
    assert "sparse_evidence" in empty.reasons

    violating = FixtureAttempt(
        attempt_id="routing_guardrails_v1:0",
        fixture_id="routing_guardrails_v1",
        target_id="fixture_guardrail_primary",
        evidence_kind="real_project",
        outcome="success",
        category="guardrail_compliant",
        cost_micros=0,
        violations=("guardrail_violation",),
    )
    result = evaluate_scope((violating,))
    assert result.qualified is False
    assert "validator_violations" in result.reasons
    assert "real_project_evidence_required" not in result.reasons

    clean_real = dataclasses.replace(violating, violations=())
    clean_result = evaluate_scope((clean_real,))
    assert clean_result.qualified is True
    assert clean_result.reasons == ()


def test_tampered_fixture_bytes_are_rejected(tmp_path: Path) -> None:
    dest = _copy_fixture_tree(tmp_path)
    cost_path = dest / "cost_accounting.json"
    cost_path.write_bytes(cost_path.read_bytes().replace(b"2200", b"2201", 1))

    with pytest.raises(ValueError, match="digest"):
        load_qualification_corpus(dest)


def test_unregistered_validator_names_are_rejected(tmp_path: Path) -> None:
    dest = _copy_fixture_tree(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["validator"] = {"id": "unregistered_eval", "parameters": {}}

    _rewrite_fixture(dest, "abstention_v1", mutate)

    with pytest.raises(ValueError, match="unregistered validator"):
        load_qualification_corpus(dest)


def test_manifest_corpus_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    dest = _copy_fixture_tree(tmp_path)
    manifest_path = dest / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corpus_digest"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    with pytest.raises(ValueError, match="corpus digest"):
        load_qualification_corpus(dest)


def test_missing_fixture_file_is_rejected(tmp_path: Path) -> None:
    dest = _copy_fixture_tree(tmp_path)
    (dest / "abstention.json").unlink()

    with pytest.raises(ValueError, match="abstention"):
        load_qualification_corpus(dest)


def test_unexpected_fixture_content_is_rejected(tmp_path: Path) -> None:
    dest = _copy_fixture_tree(tmp_path)

    def mutate(payload: dict[str, object]) -> None:
        payload["risk"] = "extreme"

    _rewrite_fixture(dest, "abstention_v1", mutate)

    with pytest.raises(ValueError, match="risk"):
        load_qualification_corpus(dest)
