#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from nested_memvid_agent.behavior_compiler import (  # noqa: E402
    BehaviorCompileRequest,
    BehaviorCompiler,
    BehaviorCompilerConfig,
)
from nested_memvid_agent.behavior_delta import BehaviorDeltaStatus  # noqa: E402
from nested_memvid_agent.behavior_delta_extractor import BehaviorDeltaExtractor  # noqa: E402
from nested_memvid_agent.behavior_delta_ledger import (  # noqa: E402
    BehaviorDeltaLedger,
    BehaviorDeltaOutcome,
)
from nested_memvid_agent.models import EvidenceRef, MemoryLayer  # noqa: E402
from nested_memvid_agent.mutation_gate import MutationGate, MutationGateEvidence  # noqa: E402
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution  # noqa: E402
from nested_memvid_agent.state_store import AgentStateStore, utc_now  # noqa: E402
from nested_memvid_agent.task_capsule import write_run_capsule  # noqa: E402
from scripts.eval_behavior_deltas import (  # noqa: E402
    BehaviorDeltaScenario,
    evaluate_behavior_delta_scenario,
)

DEMO_RUN_ID = "demo_controlled_self_modification"
DEMO_OBJECTIVE = "Repair a validation loop that repeated the same failed command without changing strategy."


def run_demo(*, output_dir: Path, backend: str = "memory") -> dict[str, Any]:
    """Run a deterministic controlled self-modification demo and write artifacts.

    The demo intentionally uses an isolated SQLite state store and run-capsule
    directory. It proves the auditable loop without depending on live provider
    output or mutating the user's durable Kestrel memory.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    state = AgentStateStore(output_dir / "state.db")
    ledger = BehaviorDeltaLedger(state)
    runs_dir = output_dir / "runs"

    tool_call = ToolCall(
        id="tool_demo_failed_pytest",
        name="shell.run",
        arguments={"command": "python -m pytest tests/test_validation_loop.py"},
    )
    failed_execution = ToolExecution(call=tool_call, success=False, content="pytest failed: validation loop error")
    capsule_path = write_run_capsule(
        runs_dir=runs_dir,
        run_id=DEMO_RUN_ID,
        objective=DEMO_OBJECTIVE,
        backend=backend,
        tool_executions=(failed_execution, failed_execution),
        errors_encountered=("pytest failed twice with the same command",),
        final_response="Validation remained red because the retry did not change strategy.",
    )
    capsule_payload: dict[str, Any] = {
        "run_id": DEMO_RUN_ID,
        "objective": DEMO_OBJECTIVE,
        "tool_calls": [
            {"tool": "shell.run", "arguments": tool_call.arguments, "success": False},
            {"tool": "shell.run", "arguments": tool_call.arguments, "success": False},
        ],
        "errors_encountered": ["pytest failed twice with the same command"],
    }

    proposals = BehaviorDeltaExtractor(ledger=ledger).propose_from_capsule(
        capsule_payload,
        run_id=DEMO_RUN_ID,
        dry_run=False,
    )
    if len(proposals) != 1:
        raise RuntimeError(f"Expected exactly one demo proposal, got {len(proposals)}")
    delta = proposals[0]

    gate = MutationGate()
    initial_decision = gate.evaluate(
        delta,
        MutationGateEvidence(validation_score=0.8, repeat_count=2, replay_passed=False),
    )
    staged = ledger.update_delta_status(delta.id, initial_decision.status, reason=initial_decision.reason)

    scenario = BehaviorDeltaScenario(
        scenario_id="demo_repeated_tool_failure_requires_changed_strategy",
        goal="Retry validation after shell.run failed with unchanged arguments.",
        active_delta_ids=(delta.id,),
        deltas=(staged,),
        expected_behavior=("block unchanged retries", "changed strategy", "changed arguments"),
        failure_conditions=("retry unchanged arguments",),
        task_type="validation",
        tool_names=("shell.run",),
        memory_layers=(MemoryLayer.PROCEDURAL,),
    )
    replay = evaluate_behavior_delta_scenario(scenario)

    activation_decision = gate.evaluate(
        staged,
        MutationGateEvidence(validation_score=0.8, repeat_count=2, replay_passed=replay.passed),
    )
    active = ledger.update_delta_status(staged.id, activation_decision.status, reason=activation_decision.reason)

    compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True, log_activations=True))
    compiled = compiler.compile(
        BehaviorCompileRequest(
            objective=scenario.goal,
            query="retry failure validation shell.run",
            run_id="demo_activation_run",
            task_type="validation",
            tool_names=("shell.run",),
            memory_layers=(MemoryLayer.PROCEDURAL,),
        )
    )
    activations = ledger.list_activations(active.id)

    outcome = BehaviorDeltaOutcome(
        id=f"outcome_{uuid4().hex}",
        delta_id=active.id,
        run_id="demo_activation_run",
        outcome="useful",
        evidence_ref=EvidenceRef(source="behavior_delta_replay", locator=scenario.scenario_id, quote="Replay passed with no gate violations."),
        notes="Demo replay showed the delta would block unchanged retries and require a changed strategy.",
        recorded_at=utc_now(),
    )
    ledger.record_outcome(outcome)

    rolled_back = ledger.update_delta_status(active.id, BehaviorDeltaStatus.ROLLED_BACK, reason="Demo rollback step disables active compilation while preserving audit history.")
    rollback_outcome = BehaviorDeltaOutcome(
        id=f"outcome_{uuid4().hex}",
        delta_id=active.id,
        run_id="demo_rollback_run",
        outcome="rolled_back",
        evidence_ref=EvidenceRef(source="operator_demo", locator="rollback", quote="Rollback disabled the active delta."),
        notes="Rollback completed; the audit record remains in the ledger.",
        recorded_at=utc_now(),
    )
    ledger.record_outcome(rollback_outcome)

    post_rollback = compiler.compile(
        BehaviorCompileRequest(
            objective=scenario.goal,
            query="retry failure validation shell.run",
            run_id="demo_post_rollback_run",
            task_type="validation",
            tool_names=("shell.run",),
            memory_layers=(MemoryLayer.PROCEDURAL,),
        )
    )
    report = ledger.report_deltas()

    result: dict[str, Any] = {
        "passed": bool(
            capsule_path.exists()
            and initial_decision.status == BehaviorDeltaStatus.STAGED
            and replay.passed
            and activation_decision.status == BehaviorDeltaStatus.ACTIVE
            and compiled.deltas
            and len(activations) == 1
            and rolled_back.status == BehaviorDeltaStatus.ROLLED_BACK
            and not post_rollback.deltas
        ),
        "capsule": {"path": str(capsule_path), "exists": capsule_path.exists()},
        "proposal": {
            "delta_id": delta.id,
            "title": delta.title,
            "kind": delta.kind.value,
            "risk": delta.risk.value,
            "target_layer": delta.target_layer.value,
            "status": delta.status.value,
            "evidence_count": len(delta.evidence_refs),
            "behavior_change": delta.behavior_change,
        },
        "initial_gate": _decision_payload(initial_decision),
        "replay": replay.to_payload(),
        "activation_gate": _decision_payload(activation_decision),
        "compiled": {
            "text": compiled.text,
            "delta_ids": [item.id for item in compiled.deltas],
            "activation_count": len(activations),
            "activations": [item.to_payload() for item in activations],
        },
        "outcome": outcome.to_payload(),
        "rollback": {"status": rolled_back.status.value, "outcome": rollback_outcome.to_payload()},
        "post_rollback_compile": {
            "text": post_rollback.text,
            "delta_ids": [item.id for item in post_rollback.deltas],
            "activation_count": len(post_rollback.deltas),
        },
        "report": report.to_payload(),
        "artifacts": {},
    }
    result["passed"] = result["passed"] and result["report"]["summary"]["rollback_rate"] == 1.0

    json_path = output_dir / "controlled_self_modification_demo.json"
    report_path = output_dir / "controlled_self_modification_demo.md"
    result["artifacts"] = {"json_path": str(json_path), "report_path": str(report_path)}
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True))
    report_path.write_text(_render_markdown(result))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a deterministic Kestrel controlled self-modification demo.")
    parser.add_argument("--output-dir", type=Path, default=Path("tmp-controlled-self-modification-demo"))
    parser.add_argument("--backend", choices=("memory", "memvid"), default="memory")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_demo(output_dir=args.output_dir, backend=args.backend)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_render_markdown(result))
        print(f"\nArtifacts:\n- JSON: {result['artifacts']['json_path']}\n- Report: {result['artifacts']['report_path']}")
    return 0 if result["passed"] else 1


def _decision_payload(decision: Any) -> dict[str, Any]:
    return {
        "accepted": decision.accepted,
        "status": decision.status.value,
        "reason": decision.reason,
        "requires_replay": decision.requires_replay,
        "requires_human_approval": decision.requires_human_approval,
        "requires_exact_call_approval": decision.requires_exact_call_approval,
        "blocked_by": list(decision.blocked_by),
    }


def _render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Kestrel Controlled Self-Modification Demo",
        "",
        "This deterministic demo proves the capsule → proposal → mutation gate → replay → activation → outcome → rollback loop.",
        "",
        f"Overall passed: `{result['passed']}`",
        "",
        "## 1. Run capsule",
        f"- Capsule exists: `{result['capsule']['exists']}`",
        f"- Capsule path: `{result['capsule']['path']}`",
        "",
        "## 2. BehaviorDelta proposal",
        f"- Delta ID: `{result['proposal']['delta_id']}`",
        f"- Title: {result['proposal']['title']}",
        f"- Kind: `{result['proposal']['kind']}`",
        f"- Risk: `{result['proposal']['risk']}`",
        f"- Target layer: `{result['proposal']['target_layer']}`",
        f"- Evidence refs: `{result['proposal']['evidence_count']}`",
        f"- Behavior change: {result['proposal']['behavior_change']}",
        "",
        "## 3. Mutation gate before replay",
        f"- Decision: `{result['initial_gate']['status']}`",
        f"- Blocked by: `{', '.join(result['initial_gate']['blocked_by']) or 'none'}`",
        "",
        "## 4. Replay validation",
        f"- Passed: `{result['replay']['passed']}`",
        f"- Baseline score: `{result['replay']['baseline_score']}`",
        f"- Delta score: `{result['replay']['delta_score']}`",
        f"- Improvement: `{result['replay']['improvement']}`",
        f"- Gate violations: `{', '.join(result['replay']['gate_violations']) or 'none'}`",
        "",
        "## 5. Activation and compilation",
        f"- Activation gate: `{result['activation_gate']['status']}`",
        f"- Activation records: `{result['compiled']['activation_count']}`",
        "",
        "```text",
        result['compiled']['text'],
        "```",
        "",
        "## 6. Outcome tracking",
        f"- Outcome: `{result['outcome']['outcome']}`",
        f"- Notes: {result['outcome']['notes']}",
        "",
        "## 7. Rollback",
        f"- Delta status after rollback: `{result['rollback']['status']}`",
        f"- Post-rollback compiled delta count: `{result['post_rollback_compile']['activation_count']}`",
        "",
        "## 8. Ledger report",
        f"- Total deltas: `{result['report']['summary']['total_deltas']}`",
        f"- Activated deltas: `{result['report']['summary']['activated_deltas']}`",
        f"- Useful rate: `{result['report']['summary']['useful_rate']}`",
        f"- Rollback rate: `{result['report']['summary']['rollback_rate']}`",
        "",
        "Safety note: the demo uses isolated state/output directories and does not mutate durable project memory.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
