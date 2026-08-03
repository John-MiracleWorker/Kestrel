"""Shipped deterministic Flock qualification corpus (Adaptive Flock plan, Task 4).

The v1 fixture package under ``nested_memvid_agent/qualification_fixtures/v1``
ships immutable, digest-bound qualification fixtures covering schema, hard
filters, replay, abstention, failure categories, usage accounting, and cap
admission. Every fixture file is validated before use, validator names must
come from the trusted deterministic registry, and fixture-only evidence is
always marked ``synthetic`` so it can never satisfy live production
activation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, cast

from .qualification_digest import canonical_digest
from .qualification_models import CorpusItem, CorpusManifest, EvidenceKind, RiskLevel

__all__ = [
    "ABSTENTION_CATEGORIES",
    "EXPECTED_V1_CORPUS_DIGEST",
    "FAILURE_CATEGORIES",
    "FixtureAttempt",
    "FixtureScopeEvaluation",
    "QualificationFixture",
    "ReplayRun",
    "ShippedQualificationCorpus",
    "evaluate_scope",
    "fixture_only_attempts",
    "load_qualification_corpus",
    "load_shipped_qualification_corpus",
    "registered_validator_ids",
    "replay_fixture",
]

_SCHEMA_VERSION = 1
_CORPUS_VERSION = "v1"
_MANIFEST_NAME = "manifest.json"
_MICROS_PER_MILLION_TOKENS = 1_000_000

# Digest of the shipped v1 corpus; the manifest carries the same value and
# the loader enforces the match.
EXPECTED_V1_CORPUS_DIGEST = "e27013e46546fda9c4f20c9768487e55343e4a7a6865356c05850e8072a03661"

FAILURE_CATEGORIES: tuple[str, ...] = (
    "cost_cap_exceeded",
    "guardrail_violation",
    "malformed_output",
    "provider_rate_limited",
    "provider_timeout",
)

ABSTENTION_CATEGORIES: tuple[str, ...] = (
    "insufficient_confidence",
    "policy_denied",
    "sparse_evidence",
)

_OUTCOMES: tuple[str, ...] = ("success", "failure", "abstention")
_RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high", "critical")


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _require_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _require_str_tuple(value: Any, name: str, *, non_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of strings")
    items = tuple(sorted({_require_text(entry, f"{name} entry") for entry in value}))
    if non_empty and not items:
        raise ValueError(f"{name} must not be empty")
    return items


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


@dataclass(frozen=True)
class ReplayRun:
    """One deterministic replay run expanded from a fixture script."""

    run_index: int
    target_id: str
    outcome: str
    category: str
    output_keys: tuple[str, ...]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class QualificationFixture:
    """Immutable validated fixture loaded from the shipped corpus package."""

    fixture_id: str
    schema_version: int
    task_family: str
    risk: RiskLevel
    capabilities: tuple[str, ...]
    task_contract: Mapping[str, Any]
    replay: Mapping[str, Any]
    validator_id: str
    validator_parameters: Mapping[str, Any]
    expected_outcome: str
    expected_category: str
    file_name: str
    file_digest: str

    @property
    def task_contract_digest(self) -> str:
        return canonical_digest(dict(self.task_contract))

    @property
    def acceptance_plan_digest(self) -> str:
        return canonical_digest(
            {
                "expected": {
                    "category": self.expected_category,
                    "outcome": self.expected_outcome,
                },
                "replay": dict(self.replay),
                "validator": {
                    "id": self.validator_id,
                    "parameters": dict(self.validator_parameters),
                },
            }
        )

    def to_corpus_item(self) -> CorpusItem:
        """Project the fixture to a canonical synthetic ``CorpusItem``."""
        return CorpusItem(
            item_id=self.fixture_id,
            task_family=self.task_family,
            risk=self.risk,
            capabilities=self.capabilities,
            task_contract_digest=self.task_contract_digest,
            acceptance_plan_digest=self.acceptance_plan_digest,
            evidence_kind="synthetic",
        )


@dataclass(frozen=True)
class ShippedQualificationCorpus:
    """Digest-bound shipped fixture corpus."""

    corpus_id: str
    corpus_version: str
    items: tuple[QualificationFixture, ...]

    @property
    def manifest(self) -> CorpusManifest:
        return CorpusManifest(
            schema_version=_SCHEMA_VERSION,
            items=tuple(fixture.to_corpus_item() for fixture in self.items),
        )

    @property
    def digest(self) -> str:
        return self.manifest.digest


@dataclass(frozen=True)
class FixtureAttempt:
    """Terminal replay attempt evidence derived from one fixture run."""

    attempt_id: str
    fixture_id: str
    target_id: str
    evidence_kind: EvidenceKind
    outcome: str
    category: str
    cost_micros: int
    violations: tuple[str, ...]


@dataclass(frozen=True)
class FixtureScopeEvaluation:
    """Fixture-gate evaluation: synthetic evidence can never qualify live."""

    qualified: bool
    reasons: tuple[str, ...]


ValidatorFn = Callable[[QualificationFixture, tuple[ReplayRun, ...]], tuple[str, ...]]


def _validate_guardrail_compliance(
    fixture: QualificationFixture, runs: tuple[ReplayRun, ...]
) -> tuple[str, ...]:
    params = fixture.validator_parameters
    allowed_targets = set(_require_str_tuple(params.get("allowed_targets"), "allowed_targets"))
    required_keys = set(_require_str_tuple(params.get("required_output_keys"), "required_output_keys"))
    max_violations = _require_int(params.get("max_guardrail_violations"), "max_guardrail_violations")
    violations: list[str] = []
    guardrail_violations = 0
    for run in runs:
        if run.target_id not in allowed_targets:
            violations.append("hard_filter_rejected_target")
        if run.outcome == "success" and not required_keys <= set(run.output_keys):
            violations.append("contract_output_missing")
        if run.category == "guardrail_violation":
            guardrail_violations += 1
        if run.outcome == "failure" and run.category not in FAILURE_CATEGORIES:
            violations.append("unregistered_failure_category")
    if guardrail_violations > max_violations:
        violations.append("guardrail_violation")
    return tuple(sorted(set(violations)))


def _validate_cost_reconciliation(
    fixture: QualificationFixture, runs: tuple[ReplayRun, ...]
) -> tuple[str, ...]:
    params = fixture.validator_parameters
    input_price = _require_int(params.get("input_per_million_micros"), "input_per_million_micros")
    output_price = _require_int(params.get("output_per_million_micros"), "output_per_million_micros")
    cap_micros = _require_int(params.get("cap_micros"), "cap_micros", minimum=1)
    expected_total = _require_int(
        fixture.replay.get("expected_total_micros"), "expected_total_micros"
    )
    violations: list[str] = []
    cumulative = 0
    for run in runs:
        if run.outcome != "success":
            violations.append("unexpected_outcome")
            continue
        cost = (
            run.input_tokens * input_price + run.output_tokens * output_price
        ) // _MICROS_PER_MILLION_TOKENS
        cumulative += cost
        if cumulative > cap_micros:
            violations.append("cap_admission_exceeded")
    if cumulative != expected_total:
        violations.append("usage_accounting_mismatch")
    return tuple(sorted(set(violations)))


def _validate_abstention_reason(
    fixture: QualificationFixture, runs: tuple[ReplayRun, ...]
) -> tuple[str, ...]:
    allowed = set(_require_str_tuple(fixture.validator_parameters.get("allowed_reasons"), "allowed_reasons"))
    violations: list[str] = []
    for run in runs:
        if run.outcome != "abstention":
            violations.append("unexpected_outcome")
        if run.category not in ABSTENTION_CATEGORIES or run.category not in allowed:
            violations.append("unregistered_abstention_reason")
    return tuple(sorted(set(violations)))


_VALIDATORS: dict[str, ValidatorFn] = {
    "abstention_reason": _validate_abstention_reason,
    "cost_reconciliation": _validate_cost_reconciliation,
    "guardrail_compliance": _validate_guardrail_compliance,
}


def registered_validator_ids() -> tuple[str, ...]:
    """Trusted deterministic validator registry."""
    return tuple(sorted(_VALIDATORS))


def _parse_script_entry(value: Any, name: str) -> Mapping[str, Any]:
    entry = _require_mapping(value, name)
    outcome = _require_text(entry.get("outcome"), f"{name}.outcome")
    if outcome not in _OUTCOMES:
        raise ValueError(f"{name}.outcome must be one of {', '.join(_OUTCOMES)}")
    category = _require_text(entry.get("category"), f"{name}.category")
    if outcome == "failure" and category not in FAILURE_CATEGORIES:
        raise ValueError(f"{name}.category must be a registered failure category")
    if outcome == "abstention" and category not in ABSTENTION_CATEGORIES:
        raise ValueError(f"{name}.category must be a registered abstention category")
    _require_str_tuple(entry.get("output_keys", []), f"{name}.output_keys", non_empty=False)
    _require_int(entry.get("input_tokens"), f"{name}.input_tokens")
    _require_int(entry.get("output_tokens"), f"{name}.output_tokens")
    return entry


def _parse_fixture(
    payload: Mapping[str, Any], *, fixture_id: str, file_name: str, file_digest: str
) -> QualificationFixture:
    schema_version = _require_int(payload.get("schema_version"), "schema_version", minimum=1)
    if schema_version != _SCHEMA_VERSION:
        raise ValueError(f"unsupported fixture schema version: {schema_version}")
    declared_id = _require_text(payload.get("fixture_id"), "fixture_id")
    if declared_id != fixture_id:
        raise ValueError(f"fixture_id {declared_id!r} does not match manifest entry {fixture_id!r}")
    stem = file_name.removesuffix(".json")
    if file_name == stem or not fixture_id.startswith(f"{stem}_v"):
        raise ValueError(f"fixture file {file_name!r} does not match fixture ID {fixture_id!r}")
    risk = _require_text(payload.get("risk"), "risk")
    if risk not in _RISK_LEVELS:
        raise ValueError(f"risk must be one of {', '.join(_RISK_LEVELS)}")

    task_contract = _require_mapping(payload.get("task_contract"), "task_contract")
    _require_text(task_contract.get("prompt"), "task_contract.prompt")

    replay = _require_mapping(payload.get("replay"), "replay")
    _require_int(replay.get("seed"), "replay.seed")
    _require_int(replay.get("runs"), "replay.runs", minimum=1)
    _require_str_tuple(replay.get("targets"), "replay.targets")
    script = replay.get("script")
    if not isinstance(script, list) or not script:
        raise ValueError("replay.script must be a non-empty list")
    for index, entry in enumerate(script):
        _parse_script_entry(entry, f"replay.script[{index}]")

    validator = _require_mapping(payload.get("validator"), "validator")
    validator_id = _require_text(validator.get("id"), "validator.id")
    if validator_id not in _VALIDATORS:
        raise ValueError(f"unregistered validator: {validator_id}")
    validator_parameters = _require_mapping(
        validator.get("parameters"), "validator.parameters"
    )

    expected = _require_mapping(payload.get("expected"), "expected")
    expected_outcome = _require_text(expected.get("outcome"), "expected.outcome")
    if expected_outcome not in _OUTCOMES:
        raise ValueError(f"expected.outcome must be one of {', '.join(_OUTCOMES)}")
    expected_category = _require_text(expected.get("category"), "expected.category")
    if expected_outcome == "failure" and expected_category not in FAILURE_CATEGORIES:
        raise ValueError("expected.category must be a registered failure category")
    if expected_outcome == "abstention" and expected_category not in ABSTENTION_CATEGORIES:
        raise ValueError("expected.category must be a registered abstention category")

    return QualificationFixture(
        fixture_id=fixture_id,
        schema_version=schema_version,
        task_family=_require_text(payload.get("task_family"), "task_family"),
        risk=cast(RiskLevel, risk),
        capabilities=_require_str_tuple(payload.get("capabilities"), "capabilities"),
        task_contract=task_contract,
        replay=replay,
        validator_id=validator_id,
        validator_parameters=validator_parameters,
        expected_outcome=expected_outcome,
        expected_category=expected_category,
        file_name=file_name,
        file_digest=file_digest,
    )


def load_qualification_corpus(root: Traversable | Path) -> ShippedQualificationCorpus:
    """Load and validate the fixture corpus below *root*.

    Every fixture file is digest-bound by the manifest, parsed, and checked
    against the trusted validator registry before the corpus digest itself is
    verified. Any mismatch raises ``ValueError``.
    """
    manifest_path = root / _MANIFEST_NAME
    try:
        manifest_bytes = manifest_path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("qualification corpus manifest is missing") from exc
    manifest = _require_mapping(
        json.loads(manifest_bytes.decode("utf-8")), _MANIFEST_NAME
    )
    if _require_int(manifest.get("schema_version"), "schema_version", minimum=1) != _SCHEMA_VERSION:
        raise ValueError("unsupported manifest schema version")
    corpus_id = _require_text(manifest.get("corpus_id"), "corpus_id")
    if _require_text(manifest.get("corpus_version"), "corpus_version") != _CORPUS_VERSION:
        raise ValueError("unsupported corpus version")
    declared_corpus_digest = _require_text(manifest.get("corpus_digest"), "corpus_digest")

    entries = _require_mapping(manifest.get("fixtures"), "fixtures")
    if not entries:
        raise ValueError("manifest must declare at least one fixture")

    fixtures: list[QualificationFixture] = []
    for fixture_id in sorted(entries):
        _require_text(fixture_id, "fixture ID")
        entry = _require_mapping(entries[fixture_id], f"fixtures.{fixture_id}")
        file_name = _require_text(entry.get("file"), f"fixtures.{fixture_id}.file")
        declared_digest = _require_text(entry.get("sha256"), f"fixtures.{fixture_id}.sha256")
        try:
            file_bytes = (root / file_name).read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(f"fixture file missing for {fixture_id}: {file_name}") from exc
        actual_digest = hashlib.sha256(file_bytes).hexdigest()
        if actual_digest != declared_digest:
            raise ValueError(f"fixture file digest mismatch for {fixture_id}")
        payload = _require_mapping(
            json.loads(file_bytes.decode("utf-8")), f"fixtures.{fixture_id}"
        )
        fixtures.append(
            _parse_fixture(
                payload,
                fixture_id=fixture_id,
                file_name=file_name,
                file_digest=actual_digest,
            )
        )

    corpus = ShippedQualificationCorpus(
        corpus_id=corpus_id,
        corpus_version=_CORPUS_VERSION,
        items=tuple(fixtures),
    )
    if corpus.digest != declared_corpus_digest:
        raise ValueError(
            "corpus digest mismatch: fixture contents do not match the manifest"
        )
    return corpus


def load_shipped_qualification_corpus() -> ShippedQualificationCorpus:
    """Load the shipped v1 corpus through package resources.

    Loading via :func:`importlib.resources.files` guarantees frozen builds
    ship and validate exactly the same fixture bytes.
    """
    root = resources.files("nested_memvid_agent") / "qualification_fixtures" / _CORPUS_VERSION
    return load_qualification_corpus(root)


def replay_fixture(fixture: QualificationFixture) -> tuple[ReplayRun, ...]:
    """Expand the deterministic replay script of *fixture* into exact runs."""
    runs = _require_int(fixture.replay.get("runs"), "replay.runs", minimum=1)
    targets = _require_str_tuple(fixture.replay.get("targets"), "replay.targets")
    script = fixture.replay["script"]
    assert isinstance(script, list)
    expanded: list[ReplayRun] = []
    for index in range(runs):
        entry = script[index % len(script)]
        assert isinstance(entry, dict)
        expanded.append(
            ReplayRun(
                run_index=index,
                target_id=targets[index % len(targets)],
                outcome=str(entry["outcome"]),
                category=str(entry["category"]),
                output_keys=tuple(str(key) for key in entry.get("output_keys", [])),
                input_tokens=int(entry.get("input_tokens", 0)),
                output_tokens=int(entry.get("output_tokens", 0)),
            )
        )
    return tuple(expanded)


def fixture_only_attempts(
    corpus: ShippedQualificationCorpus | None = None,
) -> tuple[FixtureAttempt, ...]:
    """Replay every fixture and return validated synthetic attempt evidence."""
    if corpus is None:
        corpus = load_shipped_qualification_corpus()
    attempts: list[FixtureAttempt] = []
    for fixture in corpus.items:
        runs = replay_fixture(fixture)
        violations = _VALIDATORS[fixture.validator_id](fixture, runs)
        for run in runs:
            attempts.append(
                FixtureAttempt(
                    attempt_id=f"{fixture.fixture_id}:{run.run_index}",
                    fixture_id=fixture.fixture_id,
                    target_id=run.target_id,
                    evidence_kind="synthetic",
                    outcome=run.outcome,
                    category=run.category,
                    cost_micros=(
                        run.input_tokens
                        * int(fixture.validator_parameters.get("input_per_million_micros", 0))
                        + run.output_tokens
                        * int(fixture.validator_parameters.get("output_per_million_micros", 0))
                    )
                    // _MICROS_PER_MILLION_TOKENS,
                    violations=violations,
                )
            )
    return tuple(attempts)


def evaluate_scope(attempts: tuple[FixtureAttempt, ...]) -> FixtureScopeEvaluation:
    """Evaluate attempt evidence at the fixture gate.

    Fixture-only evidence is always synthetic and therefore cannot satisfy
    live production activation: any synthetic attempt forces the scope to
    abstain with ``real_project_evidence_required``.
    """
    reasons: list[str] = []
    if not attempts:
        reasons.append("sparse_evidence")
    if any(attempt.evidence_kind == "synthetic" for attempt in attempts):
        reasons.append("real_project_evidence_required")
    if any(attempt.violations for attempt in attempts):
        reasons.append("validator_violations")
    return FixtureScopeEvaluation(qualified=not reasons, reasons=tuple(reasons))
