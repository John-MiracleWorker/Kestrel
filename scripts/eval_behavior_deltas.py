from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.app_factory import build_agent  # noqa: E402
from nested_memvid_agent.behavior_compiler import (  # noqa: E402
    BehaviorCompileRequest,
    BehaviorCompiler,
    BehaviorCompilerConfig,
)
from nested_memvid_agent.behavior_delta import (  # noqa: E402
    BehaviorDelta,
    BehaviorDeltaStatus,
    behavior_delta_from_metadata,
)
from nested_memvid_agent.behavior_delta_ledger import BehaviorDeltaLedger  # noqa: E402
from nested_memvid_agent.config import AgentConfig  # noqa: E402
from nested_memvid_agent.models import MemoryLayer  # noqa: E402
from nested_memvid_agent.state_store import AgentStateStore  # noqa: E402


@dataclass(frozen=True)
class BehaviorDeltaScenario:
    scenario_id: str
    goal: str
    active_delta_ids: tuple[str, ...]
    deltas: tuple[BehaviorDelta, ...]
    expected_behavior: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    task_type: str | None = None
    tool_names: tuple[str, ...] = ()
    memory_layers: tuple[MemoryLayer, ...] = ()


@dataclass(frozen=True)
class BehaviorDeltaReplayResult:
    scenario_id: str
    delta_id: str | None
    baseline_score: float
    delta_score: float
    improvement: float
    expected_behavior_hits: int
    expected_behavior_total: int
    gate_violations: tuple[str, ...]
    passed: bool
    compiled_text: str
    agent_context_prompt: str = ""
    agent_stop_reason: str | None = None
    activation_count: int = 0
    context_compile_events: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "delta_id": self.delta_id,
            "baseline_score": self.baseline_score,
            "delta_score": self.delta_score,
            "improvement": self.improvement,
            "expected_behavior_hits": self.expected_behavior_hits,
            "expected_behavior_total": self.expected_behavior_total,
            "gate_violations": list(self.gate_violations),
            "passed": self.passed,
            "compiled_text": self.compiled_text,
            "agent_context_prompt": self.agent_context_prompt,
            "agent_stop_reason": self.agent_stop_reason,
            "activation_count": self.activation_count,
            "context_compile_events": self.context_compile_events,
        }


def load_scenario(path: Path) -> BehaviorDeltaScenario:
    payload = json.loads(path.read_text())
    deltas = tuple(_delta_from_fixture(item) for item in payload.get("deltas", ()))
    memory_layers = tuple(MemoryLayer(item) for item in payload.get("memory_layers", ()) or ())
    return BehaviorDeltaScenario(
        scenario_id=str(payload["scenario_id"]),
        goal=str(payload["goal"]),
        active_delta_ids=tuple(str(item) for item in payload.get("active_delta_ids", ())),
        deltas=deltas,
        expected_behavior=tuple(str(item) for item in payload.get("expected_behavior", ())),
        failure_conditions=tuple(str(item) for item in payload.get("failure_conditions", ())),
        task_type=_optional_str(payload.get("task_type")),
        tool_names=tuple(str(item) for item in payload.get("tool_names", ()) or ()),
        memory_layers=memory_layers,
    )


def evaluate_behavior_delta_scenario(scenario: BehaviorDeltaScenario) -> BehaviorDeltaReplayResult:
    with tempfile.TemporaryDirectory(prefix="kestrel_behavior_delta_replay_") as tmp:
        ledger = BehaviorDeltaLedger(AgentStateStore(Path(tmp) / "state.db"))
        active_ids = set(scenario.active_delta_ids)
        active_deltas = []
        for delta in scenario.deltas:
            status = BehaviorDeltaStatus.ACTIVE if delta.id in active_ids else delta.status
            active_delta = _with_status(delta, status)
            ledger.record_delta(active_delta)
            if active_delta.status == BehaviorDeltaStatus.ACTIVE:
                active_deltas.append(active_delta)

        compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True, log_activations=False))
        compiled = compiler.compile(_request_for(scenario, active_deltas))

    baseline_text = _baseline_behavior_text(scenario)
    baseline_score = _score_text(baseline_text, scenario.expected_behavior)
    delta_score = _score_text(compiled.text, scenario.expected_behavior)
    gate_violations = _gate_violations(compiled.text, scenario.failure_conditions)
    improvement = round(delta_score - baseline_score, 4)
    expected_hits = _hit_count(compiled.text, scenario.expected_behavior)
    passed = delta_score > baseline_score and not gate_violations and expected_hits == len(scenario.expected_behavior)
    return BehaviorDeltaReplayResult(
        scenario_id=scenario.scenario_id,
        delta_id=next(iter(scenario.active_delta_ids), None),
        baseline_score=baseline_score,
        delta_score=delta_score,
        improvement=improvement,
        expected_behavior_hits=expected_hits,
        expected_behavior_total=len(scenario.expected_behavior),
        gate_violations=gate_violations,
        passed=passed,
        compiled_text=compiled.text,
    )


def evaluate_behavior_delta_agent_scenario(scenario: BehaviorDeltaScenario) -> BehaviorDeltaReplayResult:
    """Replay a scenario through a real NestedMV2Agent turn with mock provider.

    This is deeper than fixture-only compiler replay: it persists active deltas in
    the SQLite control plane, lets the agent compile context through its normal
    chat path, and verifies activation logging/events from the runtime boundary.
    """
    with tempfile.TemporaryDirectory(prefix="kestrel_behavior_delta_agent_replay_") as tmp:
        root = Path(tmp)
        state = AgentStateStore(root / "state.db")
        ledger = BehaviorDeltaLedger(state)
        active_ids = set(scenario.active_delta_ids)
        for delta in scenario.deltas:
            status = BehaviorDeltaStatus.ACTIVE if delta.id in active_ids else delta.status
            ledger.record_delta(_with_status(delta, status))

        config = AgentConfig(
            provider="mock",
            model="mock",
            backend="memory",
            memory_dir=root / "memory",
            log_dir=root / "logs",
            state_path=root / "state.db",
            enable_behavior_deltas=True,
            max_tool_rounds=0,
        )
        agent = build_agent(config, state=state)
        try:
            turn = agent.chat(scenario.goal, session_id="behavior_delta_replay", run_id=f"replay_{scenario.scenario_id}")
        finally:
            agent.close()

        text = turn.context_prompt
        baseline_text = _baseline_behavior_text(scenario)
        baseline_score = _score_text(baseline_text, scenario.expected_behavior)
        delta_score = _score_text(text, scenario.expected_behavior)
        gate_violations = _gate_violations(text, scenario.failure_conditions)
        improvement = round(delta_score - baseline_score, 4)
        expected_hits = _hit_count(text, scenario.expected_behavior)
        active_delta_id = next(iter(scenario.active_delta_ids), None)
        activations = ledger.list_activations(active_delta_id) if active_delta_id else ()
        context_events = [event for event in agent.event_log.tail(limit=50) if event.type == "context.compile"] if agent.event_log else []
        passed = delta_score > baseline_score and not gate_violations and expected_hits == len(scenario.expected_behavior)
        return BehaviorDeltaReplayResult(
            scenario_id=scenario.scenario_id,
            delta_id=active_delta_id,
            baseline_score=baseline_score,
            delta_score=delta_score,
            improvement=improvement,
            expected_behavior_hits=expected_hits,
            expected_behavior_total=len(scenario.expected_behavior),
            gate_violations=gate_violations,
            passed=passed,
            compiled_text=text,
            agent_context_prompt=text,
            agent_stop_reason=turn.stop_reason,
            activation_count=len(activations),
            context_compile_events=len(context_events),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay-evaluate Kestrel behavior-delta scenarios.")
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--provider", choices=["mock"], default="mock")
    parser.add_argument("--mode", choices=["compiler", "agent"], default="compiler")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    result = (
        evaluate_behavior_delta_agent_scenario(scenario)
        if args.mode == "agent"
        else evaluate_behavior_delta_scenario(scenario)
    )
    payload = result.to_payload()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Behavior-delta replay: {result.scenario_id}")
        print(f"Delta: {result.delta_id}")
        print(f"Baseline score: {result.baseline_score}")
        print(f"Delta score: {result.delta_score}")
        print(f"Improvement: {result.improvement}")
        print(f"Gate violations: {', '.join(result.gate_violations) if result.gate_violations else 'none'}")
        print(f"Passed: {result.passed}")
    return 1 if args.fail_on_regression and not result.passed else 0


def _delta_from_fixture(payload: dict[str, Any]) -> BehaviorDelta:
    normalized = dict(payload)
    normalized.setdefault("status", BehaviorDeltaStatus.PROPOSED.value)
    normalized.setdefault("rollback_plan", {"can_disable": True})
    normalized.setdefault("activation_stats", {})
    normalized.setdefault("confidence", 0.8)
    normalized.setdefault("importance", 0.7)
    return behavior_delta_from_metadata(normalized)


def _with_status(delta: BehaviorDelta, status: BehaviorDeltaStatus) -> BehaviorDelta:
    payload = delta.to_metadata()
    payload["status"] = status.value
    return behavior_delta_from_metadata(payload)


def _request_for(scenario: BehaviorDeltaScenario, deltas: list[BehaviorDelta]) -> BehaviorCompileRequest:
    task_type = scenario.task_type or _first_non_empty(tuple(value for delta in deltas for value in delta.trigger.task_types))
    tool_names = scenario.tool_names or tuple(value for delta in deltas for value in delta.trigger.tool_names)
    memory_layers = scenario.memory_layers or tuple(value for delta in deltas for value in delta.trigger.memory_layers)
    query = " ".join(value for delta in deltas for value in delta.trigger.query_patterns)
    return BehaviorCompileRequest(
        objective=scenario.goal,
        query=query,
        run_id=f"replay_{scenario.scenario_id}",
        task_type=task_type,
        tool_names=tool_names,
        memory_layers=memory_layers,
    )


def _baseline_behavior_text(scenario: BehaviorDeltaScenario) -> str:
    return f"BASELINE RUN: {scenario.goal}. No behavior delta instructions were compiled."


def _score_text(text: str, expected_behavior: tuple[str, ...]) -> float:
    if not expected_behavior:
        return 1.0
    return round(_hit_count(text, expected_behavior) / len(expected_behavior), 4)


def _hit_count(text: str, expectations: tuple[str, ...]) -> int:
    return sum(1 for expectation in expectations if _phrase_matches(text, expectation))


def _gate_violations(text: str, failure_conditions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(condition for condition in failure_conditions if _failure_condition_matches(text, condition))


def _phrase_matches(text: str, phrase: str) -> bool:
    text_tokens = set(_tokens(text))
    phrase_tokens = [token for token in _tokens(phrase) if token not in _STOPWORDS]
    if not phrase_tokens:
        return False
    return all(token in text_tokens for token in phrase_tokens)


def _failure_condition_matches(text: str, phrase: str) -> bool:
    if not _phrase_matches(text, phrase):
        return False
    text_tokens = set(_tokens(text))
    phrase_tokens = {token for token in _tokens(phrase) if token not in _STOPWORDS}
    if phrase_tokens and phrase_tokens.intersection({"retry", "replace", "write", "use", "remove"}):
        if text_tokens.intersection({"avoid", "block", "forbid", "not", "preserve"}):
            return False
    return True


def _tokens(value: str) -> list[str]:
    normalized = value.lower().replace(".mv2", " mv2 ")
    return [_stem(token) for token in re.findall(r"[a-zA-Z0-9]+", normalized)]


def _stem(token: str) -> str:
    irregular = {
        "writes": "write",
        "changes": "change",
        "bypasses": "bypass",
        "bypass": "bypass",
        "replaces": "replace",
        "uses": "use",
        "removes": "remove",
        "indices": "index",
    }
    if token in irregular:
        return irregular[token]
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ing") and len(token) > 5:
        return token[:-3]
    if token.endswith("ed") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def _first_non_empty(values: tuple[str, ...]) -> str | None:
    return next((value for value in values if value), None)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "before",
    "or",
    "the",
    "to",
    "with",
}


if __name__ == "__main__":
    raise SystemExit(main())
