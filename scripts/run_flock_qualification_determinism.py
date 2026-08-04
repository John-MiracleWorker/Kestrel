#!/usr/bin/env python3
"""Twenty-repeat deterministic Flock qualification gate (Adaptive Flock Task 22).

Executes the same bounded qualification journey ``repeats`` times against
deterministic mock targets and asserts that every repeat completes with the
exact same receipt projection digest and zero guardrail violations.

Frozen per repeat (plan Task 22 Step 3):

- clock: ``utc_now`` and the completion replay reference time are pinned to
  ``FROZEN_CLOCK``;
- IDs: run, case, attempt, lease, decision, and session IDs are fixed
  constants (receipt IDs are content-derived digests);
- evidence order: fair matrix admission is stable by ``(case_id, target_id)``;
- target inventory, prices, build ID, and provider outputs are fixed fixtures.

The report binds the source commit and a configuration digest so any drift is
attributable.  Mock evidence never certifies production provider
qualification; this gate only proves the qualification pipeline is
deterministic.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: S404 - git rev-parse only, fixed arguments
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from nested_memvid_agent.control_plane_integrity import (  # noqa: E402
    ControlPlaneIntegrity,
)
from nested_memvid_agent.routing.contracts import compile_task_contract  # noqa: E402
from nested_memvid_agent.routing.qualification_budget import (  # noqa: E402
    AttemptTokenCeilings,
)
from nested_memvid_agent.routing.qualification_digest import (  # noqa: E402
    canonical_digest,
)
from nested_memvid_agent.routing.qualification_executor import (  # noqa: E402
    AttemptEvidence,
    AttemptLease,
    ExecutorRouteDecision,
    ProviderAttempt,
)
from nested_memvid_agent.routing.qualification_ledger import (  # noqa: E402
    QualificationLedger,
)
from nested_memvid_agent.routing.qualification_models import (  # noqa: E402
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    PriceSnapshot,
    QualificationScope,
    QualificationThresholds,
)
from nested_memvid_agent.routing.qualification_receipt import (  # noqa: E402
    verify_terminal_receipt,
)
from nested_memvid_agent.routing.qualification_records import (  # noqa: E402
    QualificationAttempt,
    QualificationCase,
    QualificationCaseDraft,
    QualificationRun,
    QualificationRunDraft,
)
from nested_memvid_agent.routing.qualification_runner import (  # noqa: E402
    QualificationRunner,
)
from nested_memvid_agent.security_boundary import redact_secrets  # noqa: E402
from nested_memvid_agent.state_store import (  # noqa: E402
    AgentStateStore,
    TaskNodeRecord,
)

REPORT_SCHEMA = "kestrel.flock_qualification_determinism.v1"
DEFAULT_REPEATS = 20
FROZEN_CLOCK = "2026-08-03T00:00:00+00:00"
FROZEN_BUILD_ID = {"version": "0.5.0", "git": "flock-qualification-determinism"}
RUN_ID = "qual_determinism_gate"
TARGET_IDS = ("target_a", "target_b")
N_CASES = 2
CAPTURED_AT = "2026-08-02T00:00:00+00:00"
TOKEN_CEILINGS = AttemptTokenCeilings(max_input_tokens=1_000, max_output_tokens=500)

# Modules that bound ``utc_now`` by value at import time and participate in
# the qualification run path; every binding is pinned to the frozen clock.
_CLOCK_MODULES = (
    "nested_memvid_agent.state_store",
    "nested_memvid_agent.routing.ledger",
    "nested_memvid_agent.routing.ledger_schema",
    "nested_memvid_agent.routing.ledger_registry",
    "nested_memvid_agent.routing.qualification_ledger",
)


class _FrozenDateTime(datetime):
    """``datetime`` stand-in whose ``now`` never advances."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        moment = datetime.fromisoformat(FROZEN_CLOCK)
        if tz is None:
            return moment.replace(tzinfo=None)
        return moment.astimezone(tz)


def _frozen_configuration() -> dict[str, Any]:
    """The complete frozen qualification configuration for this gate."""

    return {
        "run_id": RUN_ID,
        "owner_principal": "owner@example.test",
        "project_id": "project-alpha",
        "task_family": "repository_inspection",
        "risk": "low",
        "capabilities": ["repository_inspection"],
        "policy_id": "balanced",
        "policy_revision": 1,
        "target_ids": list(TARGET_IDS),
        "target_inventory_digest": "1" * 64,
        "price_digest": "2" * 64,
        "learned_config_digest": "3" * 64,
        "project_authority_digest": "4" * 64,
        "n_cases": N_CASES,
        "thresholds": json.loads(json.dumps(vars(QualificationThresholds()))),
        "prices": {
            target: {
                "source": "operator_verified",
                "captured_at": CAPTURED_AT,
                "input_per_million_micros": 1_000_000,
                "output_per_million_micros": 2_000_000,
            }
            for target in TARGET_IDS
        },
        "token_ceilings": {
            "max_input_tokens": TOKEN_CEILINGS.max_input_tokens,
            "max_output_tokens": TOKEN_CEILINGS.max_output_tokens,
        },
        "max_spend_usd": "50.00",
        "effective_stop_cap_usd": "25.00",
        "attempt_ceiling_usd": "5.00",
        "build": FROZEN_BUILD_ID,
        "frozen_clock": FROZEN_CLOCK,
        "provider_outputs": {
            "provider": "recording",
            "output": "fixture output",
            "input_tokens": 1_000,
            "output_tokens": 500,
            "latency_seconds": 0.1,
        },
        "provider_kind": "deterministic_mock",
    }


def configuration_digest() -> str:
    return canonical_digest(_frozen_configuration())


def _scope() -> QualificationScope:
    return QualificationScope(
        project_id="project-alpha",
        task_family="repository_inspection",
        risk="low",
        capabilities=("repository_inspection",),
        policy_id="balanced",
        policy_revision=1,
        target_ids=TARGET_IDS,
        target_inventory_digest="1" * 64,
        price_digest="2" * 64,
        learned_config_digest="3" * 64,
        project_authority_digest="4" * 64,
    )


def _corpus() -> CorpusManifest:
    return CorpusManifest(
        schema_version=1,
        items=tuple(
            CorpusItem(
                item_id=f"corpus_item_{index}",
                task_family="repository_inspection",
                risk="low",
                capabilities=("repository_inspection",),
                task_contract_digest="a" * 64,
                acceptance_plan_digest="b" * 64,
                evidence_kind="real_project",
            )
            for index in range(1, N_CASES + 1)
        ),
    )


def _price(target_id: str) -> PriceSnapshot:
    return PriceSnapshot(
        target_id=target_id,
        source="operator_verified",
        captured_at=CAPTURED_AT,
        input_per_million=MoneyMicros(1_000_000),
        output_per_million=MoneyMicros(2_000_000),
    )


class _ScriptedExecutor:
    """Deterministic executor double; identical output for every attempt."""

    def execute(self, lease: AttemptLease) -> AttemptEvidence:
        return AttemptEvidence(
            lease_id=lease.lease_id,
            attempt_id=lease.attempt_id,
            run_id=lease.run_id,
            case_id=lease.case_id,
            actual_target_id=lease.target_id,
            containment=lease.containment,
            workspace_ref=f"workspace:{lease.lease_id}",
            route_decision=ExecutorRouteDecision(
                decision_id=f"decision-{lease.attempt_id}",
                selected_target_id=lease.target_id,
                selection_kind="direct_pin",
                actionable=True,
                reason_codes=(),
                hard_filter_reasons=(),
            ),
            provider_attempt=ProviderAttempt(
                target_id=lease.target_id,
                provider="recording",
                model=f"model-{lease.target_id}",
                output="fixture output",
                input_tokens=1_000,
                output_tokens=500,
                latency_seconds=0.1,
                failure_category=None,
            ),
            validation_passed=True,
            validation_codes=("accepted",),
            failure_category=None,
            evidence_refs=(f"workspace:{lease.lease_id}",),
        )


def _lease_factory(tasks: dict[str, TaskNodeRecord]):  # noqa: ANN202
    def build(
        run: QualificationRun,
        case: QualificationCase,
        attempt: QualificationAttempt,
    ) -> AttemptLease:
        task = tasks[case.case_id]
        return AttemptLease(
            lease_id=f"lease-{attempt.attempt_id}",
            run_id=run.run_id,
            case_id=case.case_id,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            target_id=attempt.target_id,
            task=task,
            task_contract_digest=compile_task_contract(task).digest,
            project_digest="a" * 64,
            tree_digest="b" * 64,
            target_digest=attempt.target_digest,
            price_digest="d" * 64,
            policy_digest="e" * 64,
            config_digest="f" * 64,
            reservation=attempt.reservation,
            containment="isolated_worktree",
        )

    return build


def _run_single_repeat(repeat_dir: Path) -> dict[str, Any]:
    """Run one frozen qualification journey and project its receipt."""

    state = AgentStateStore(repeat_dir / "state" / "agent.db")
    ledger = QualificationLedger(state)
    state.create_run(
        run_id=RUN_ID,
        message="Qualify flock targets",
        session_id=f"session-{RUN_ID}",
        workspace=str(repeat_dir),
        provider="mock",
        model="mock",
    )
    ledger.create_run(
        QualificationRunDraft(
            run_id=RUN_ID,
            owner_principal="owner@example.test",
            scope=_scope(),
            corpus=_corpus(),
            thresholds=QualificationThresholds(),
            target_snapshot={"targets": list(TARGET_IDS)},
            price_snapshot={"source": "operator_verified"},
            policy_payload={"policy_id": "balanced", "revision": 1},
            learned_payload={"state": "disabled"},
            project_authority={"principal": "owner@example.test"},
            build=FROZEN_BUILD_ID,
            max_spend=MoneyMicros.from_usd_text("50.00"),
            effective_stop_cap=MoneyMicros.from_usd_text("25.00"),
            attempt_ceiling=MoneyMicros.from_usd_text("5.00"),
        )
    )
    tasks: dict[str, TaskNodeRecord] = {}
    for item in _corpus().items:
        case_id = f"case_{item.item_id.rsplit('_', 1)[-1]}"
        ledger.create_case(
            QualificationCaseDraft(
                case_id=case_id,
                run_id=RUN_ID,
                item=item,
                repository_digest="c" * 64,
                privacy_eligible=True,
            )
        )
        tasks[case_id] = state.create_task_node(
            task_id=f"task-{case_id}",
            run_id=RUN_ID,
            title="Inspect repository context",
            goal="Gather relevant repository context without changing files.",
            profile="worker",
            approved=True,
            required_tools=("repo.search", "repo.map"),
            risk="low",
            acceptance_criteria=(),
        )
    runner = QualificationRunner(
        state,
        ledger,
        executor=_ScriptedExecutor(),
        lease_factory=_lease_factory(tasks),
        prices={target: _price(target) for target in TARGET_IDS},
        token_ceilings=TOKEN_CEILINGS,
        pause_timeout_seconds=15.0,
    )
    runner.start(RUN_ID)
    view = runner.get(RUN_ID)
    if view.status != "completed":
        raise RuntimeError(f"qualification run did not complete: {view.status}")
    receipts = [
        receipt
        for receipt in ledger.list_receipts(RUN_ID)
        if receipt.receipt_type == "run_terminal"
    ]
    if len(receipts) != 1:
        raise RuntimeError(f"expected one terminal receipt, found {len(receipts)}")
    payload = receipts[0].payload
    integrity = ControlPlaneIntegrity(Path(state.path).parent)
    if not verify_terminal_receipt(payload, integrity=integrity):
        raise RuntimeError("terminal receipt failed authentication")
    return {
        "receipt_projection_digest": payload["payload_digest"],
        "guardrail_violations": int(payload["guardrail_violations"]),
        "replay_unique_projection_digests": payload["replay"][
            "unique_projection_digests"
        ],
    }


def run_qualification_determinism(
    root: Path,
    repeats: int = DEFAULT_REPEATS,
    *,
    source_commit: str | None = None,
) -> dict[str, Any]:
    """Run ``repeats`` frozen qualification journeys and build the report."""

    if repeats < 1:
        raise ValueError("repeats must be at least one")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    resolved_commit = source_commit or _git_head()
    projections: list[str] = []
    guardrail_violations = 0
    replay_drift_repeats = 0
    completed = 0
    with _frozen_environment():
        for index in range(repeats):
            outcome = _run_single_repeat(root / f"repeat-{index:02d}")
            completed += 1
            projections.append(outcome["receipt_projection_digest"])
            guardrail_violations += outcome["guardrail_violations"]
            if outcome["replay_unique_projection_digests"] != 1:
                replay_drift_repeats += 1
    unique_digests = len(set(projections))
    reasons: list[str] = []
    if completed != repeats:
        reasons.append("repeat_incomplete")
    if unique_digests != 1:
        reasons.append("receipt_projection_drift")
    if replay_drift_repeats:
        reasons.append("replay_drift")
    if guardrail_violations != 0:
        reasons.append("guardrail_violation")
    passed = not reasons
    report = {
        "schema": REPORT_SCHEMA,
        "source_commit": resolved_commit,
        "configuration_digest": configuration_digest(),
        "configuration": _frozen_configuration(),
        "repeats": repeats,
        "completed_repeats": completed,
        "receipt_projection_digests": projections,
        "unique_receipt_projection_digests": unique_digests,
        "guardrail_violations": guardrail_violations,
        "flake_count": (repeats - completed) + max(0, unique_digests - 1),
        "passed": passed,
        "reasons": reasons,
        "qualification_scope": "deterministic_mock_targets_only",
        "production_provider_qualification": False,
    }
    return redact_secrets(report)


class _frozen_environment(ExitStack):
    """Pin every clock binding reachable from the qualification path."""

    def __init__(self) -> None:
        super().__init__()
        for module in _CLOCK_MODULES:
            self.enter_context(
                mock.patch(f"{module}.utc_now", lambda: FROZEN_CLOCK)
            )
        self.enter_context(
            mock.patch(
                "nested_memvid_agent.routing.qualification_runner.datetime",
                _FrozenDateTime,
            )
        )


def _git_head() -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_report(report: dict[str, Any], output: Path) -> None:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=output.parent, delete=False, encoding="utf-8"
    ) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    Path(temp_name).replace(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--output", required=True, help="report JSON path")
    parser.add_argument(
        "--run-root",
        default=None,
        help="directory for per-repeat state (default: alongside the report)",
    )
    parser.add_argument(
        "--source-commit",
        default=os.getenv("GITHUB_SHA"),
        help="40-hex source commit bound into the report (default: git HEAD)",
    )
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least one")
    output = Path(args.output)
    run_root = Path(args.run_root) if args.run_root else output.parent / "flock-runs"
    report = run_qualification_determinism(
        run_root, repeats=args.repeats, source_commit=args.source_commit
    )
    _write_report(report, output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
