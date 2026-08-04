"""No-authority-expansion matrix (Adaptive Flock plan, Task 21).

Every boundary case in
``tests/evals/adaptive_flock_qualification/authority_matrix.json`` is executed
against the real routing chain with a durable, effective activation grant and
a trained learned winner that differs from the static target.  For each case
the uniform invariant must hold:

* the learned selection never expands task authority — the effective
  ``AgentConfig`` after routing is identical to the base config on every
  field the routing layer does not own (routing may only rebind the provider
  target fields), and
* the compiled task contract digest is unchanged (the grant has no input to
  contract compilation).

High/critical-risk scopes are deterministic-only: even with an active grant
the learned route must not apply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import pytest
from test_flock_grant_runtime import (
    GrantHarness,
    _configured_ledger,
    _learned_coordinator,
    _train_learned_winner,
)

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.contracts import compile_task_contract
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord

MATRIX_PATH = Path("tests/evals/adaptive_flock_qualification/authority_matrix.json")

#: Boundaries the corpus must cover (Adaptive Flock plan, Task 21).
REQUIRED_BOUNDARIES: tuple[str, ...] = (
    "tools",
    "mcp",
    "skills",
    "plugins",
    "network",
    "workspace",
    "secrets",
    "budget",
    "approvals",
    "privacy",
    "containment",
    "task_graph",
    "memory",
    "high_risk",
)

#: The only AgentConfig fields routing may ever rewrite (see
#: ``routing/service.py::AdaptiveFlockRoutingService.apply_decision``).
ROUTING_OWNED_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "provider",
        "model",
        "base_url",
        "api_key_env",
        "fallback_provider",
        "fallback_model",
        "fallback_base_url",
        "fallback_api_key_env",
        "lan_runtime_authority",
    }
)


@dataclass(frozen=True)
class AuthorityCase:
    case_id: str
    boundary: str
    description: str
    risk: str
    required_tools: tuple[str, ...]
    base_config: dict[str, Any]
    contract_options: dict[str, Any]
    expect: dict[str, Any]


def load_authority_matrix() -> list[AuthorityCase]:
    if not MATRIX_PATH.is_file():
        raise AssertionError(f"authority matrix corpus is missing: {MATRIX_PATH}")
    payload = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    cases = [
        AuthorityCase(
            case_id=str(entry["case_id"]),
            boundary=str(entry["boundary"]),
            description=str(entry["description"]),
            risk=str(entry.get("risk", "low")),
            required_tools=tuple(str(tool) for tool in entry.get("required_tools", ())),
            base_config=dict(entry.get("base_config", {})),
            contract_options=dict(entry.get("contract_options", {})),
            expect=dict(entry["expect"]),
        )
        for entry in payload["cases"]
    ]
    boundaries = {case.boundary for case in cases}
    missing = [boundary for boundary in REQUIRED_BOUNDARIES if boundary not in boundaries]
    if missing:
        raise AssertionError(f"authority matrix misses boundaries: {missing}")
    return cases


AUTHORITY_MATRIX = load_authority_matrix()


def _create_matrix_task(state: AgentStateStore, case: AuthorityCase) -> TaskNodeRecord:
    run_id = f"run-matrix-{case.case_id}"
    state.create_run(
        run_id=run_id,
        message="Inspect repository context",
        session_id=f"session-matrix-{case.case_id}",
        workspace="/tmp/workspace",
        provider="mock",
        model="mock",
    )
    return state.create_task_node(
        task_id=f"task-matrix-{case.case_id}",
        run_id=run_id,
        title="Inspect repository context",
        goal="Gather relevant repository context without changing files.",
        profile="worker",
        approved=True,
        required_tools=case.required_tools,
        risk=case.risk,
        acceptance_criteria=("Relevant code is located.",),
    )


@pytest.mark.parametrize(
    "case",
    AUTHORITY_MATRIX,
    ids=[case.case_id for case in AUTHORITY_MATRIX],
)
def test_learned_selection_never_expands_task_authority(
    case: AuthorityCase,
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    task = _create_matrix_task(state, case)
    base_config = AgentConfig(**case.base_config)
    before_contract = compile_task_contract(task, **case.contract_options)
    harness = GrantHarness(state, before_contract)
    coordinator = _learned_coordinator(ledger, harness.evaluator)

    durable = coordinator.assign(
        base_config,
        task,
        subagent_id=None,
        attempt=1,
        **case.contract_options,
    )

    # The test must not pass vacuously: low/medium-risk cases exercise a real
    # learned selection; deterministic-only risks prove the learned route was
    # refused despite the active grant.
    if case.expect["learned_applies"]:
        assert durable.record.activation_effective is True
        assert durable.record.selection_kind == "learned_constrained"
        assert durable.assignment.decision.selected_target.target_id == "cheap"
    else:
        assert durable.record.activation_effective is False
        assert durable.record.activation_grant_id is None
        assert durable.assignment.decision.selected_target.target_id == "expensive"
        shadow_reason = case.expect.get("shadow_abstention_reason")
        if shadow_reason is not None:
            shadow = ledger.get_shadow(durable.record.decision_id)
            assert shadow is not None
            assert shadow.activated is False
            assert shadow.abstention_reason == shadow_reason
        evaluator_reason = case.expect.get("evaluator_reason")
        if evaluator_reason is not None:
            # Defense in depth: even if the coordinator short-circuit were
            # bypassed, the activation evaluator itself refuses the grant.
            evaluation = harness.evaluator.evaluate(before_contract)
            assert evaluation.effective is False
            assert evaluator_reason in evaluation.reason_codes

    after_contract = durable.assignment.contract
    after_config = durable.assignment.config

    # The contract is compiled from the task alone: the active grant cannot
    # change any contract field, so the digest is byte-identical.
    assert after_contract.digest == before_contract.digest

    # Effective authority after routing is a subset of (identical to) the
    # authority before routing on every field routing does not own.
    for config_field in fields(AgentConfig):
        if config_field.name in ROUTING_OWNED_CONFIG_FIELDS:
            continue
        assert getattr(after_config, config_field.name) == getattr(
            base_config, config_field.name
        ), f"authority field changed: {config_field.name}"

    # Boundary-specific invariants from the corpus.
    for name in case.expect.get("unchanged_config_fields", ()):
        assert getattr(after_config, name) == getattr(base_config, name), name
    for name in case.expect.get("unchanged_contract_fields", ()):
        assert getattr(after_contract, name) == getattr(before_contract, name), name

    if case.expect.get("verify_task_graph"):
        record = state.get_task_node(task.task_id)
        assert record.approved == task.approved
        assert record.required_tools == task.required_tools
        assert record.risk == task.risk
        assert record.acceptance_criteria == task.acceptance_criteria
