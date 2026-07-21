from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, cast
from uuid import uuid4

from .app_factory import build_agent
from .behavior_compiler import BehaviorCompiler, BehaviorCompilerConfig, BehaviorCompileRequest
from .behavior_delta import (
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaStatus,
    behavior_delta_from_metadata,
)
from .behavior_delta_extractor import BehaviorDeltaExtractor
from .behavior_delta_ledger import BehaviorDeltaLedger, BehaviorDeltaOutcome
from .config import AgentConfig
from .llm.base import ProviderError
from .llm.factory import build_llm_provider
from .llm.model_catalog import DEFAULT_API_KEY_ENVS, STATIC_MODEL_SUGGESTIONS
from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from .mutation_gate import MutationDecision, MutationGate, MutationGateEvidence
from .runtime_models import ChatMessage, LLMOptions, ToolCall, ToolExecution
from .security_boundary import redact_secrets as _boundary_redact_secrets
from .state_store import AgentStateStore, utc_now
from .task_capsule import summarize_run_capsule, write_run_capsule

StageStatus = Literal["pass", "fail", "skip"]
ProviderMode = Literal["mock", "live", "both"]
BackendMode = Literal["memory", "memvid", "both"]

LEARNING_EVAL_SCHEMA = "kestrel.learning_architecture_eval.v1"
DEFAULT_SCENARIO_DIR = Path("tests/evals/learning_architecture")
DEFAULT_LIVE_MODEL_BY_PROVIDER = {
    "openai": "gpt-5-mini",
    "openai-compatible": "local-model",
}
STAGE_NAMES = (
    "setup",
    "provider_smoke",
    "agent_run",
    "capsule_trace_extraction",
    "mutation_gate",
    "replay_validation",
    "behavior_compilation",
    "tool_aware_preflight",
    "outcome_ledger",
    "rollback",
)


@dataclass(frozen=True)
class LearningEvalStep:
    name: str
    status: StageStatus
    message: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "metrics": self.metrics,
            "artifacts": self.artifacts,
        }
        return cast(dict[str, Any], redact_secrets(payload))


@dataclass(frozen=True)
class LearningEvalExpectedOutcome:
    name: str
    delta_id: str | None = None
    notes: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> LearningEvalExpectedOutcome:
        if isinstance(payload, str):
            return cls(name=payload)
        if not isinstance(payload, dict):
            raise ValueError("expected_outcomes entries must be strings or objects")
        return cls(
            name=str(payload["name"]),
            delta_id=_optional_str(payload.get("delta_id")),
            notes=str(payload.get("notes", "")),
        )

    def to_payload(self) -> dict[str, Any]:
        return {"name": self.name, "delta_id": self.delta_id, "notes": self.notes}


@dataclass(frozen=True)
class LearningEvalScenario:
    id: str
    title: str
    description: str
    provider_modes: tuple[ProviderMode, ...]
    backend_modes: tuple[BackendMode, ...]
    initial_memory_records: tuple[dict[str, Any], ...]
    active_behavior_deltas: tuple[BehaviorDelta, ...]
    user_goal: str
    expected_delta_kinds: tuple[str, ...]
    expected_gate_status: str | None
    expected_activation_count: int | None
    expected_outcomes: tuple[LearningEvalExpectedOutcome, ...]
    forbidden_events: tuple[str, ...]
    required_events: tuple[str, ...]
    max_llm_calls: int | None = None
    max_tool_calls: int | None = None
    max_cost_usd: float | None = None
    timeout_seconds: int | None = None
    capsule: dict[str, Any] = field(default_factory=dict)
    mutation_gate_evidence: dict[str, Any] = field(default_factory=dict)
    replay: dict[str, Any] = field(default_factory=dict)
    tool_preflight: dict[str, Any] = field(default_factory=dict)
    rollback_delta_id: str | None = None
    controlled_activation: bool = False

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LearningEvalScenario:
        scenario_id = str(payload.get("id") or payload.get("scenario_id"))
        provider_modes = _provider_modes(payload.get("provider_modes", ("both",)))
        backend_modes = _backend_modes(payload.get("backend_modes", ("both",)))
        active_deltas = tuple(
            _delta_from_fixture(item) for item in payload.get("active_behavior_deltas", ())
        )
        return cls(
            id=scenario_id,
            title=str(payload.get("title") or scenario_id.replace("_", " ").title()),
            description=str(payload.get("description", "")),
            provider_modes=provider_modes,
            backend_modes=backend_modes,
            initial_memory_records=tuple(
                dict(item) for item in payload.get("initial_memory_records", ()) or ()
            ),
            active_behavior_deltas=active_deltas,
            user_goal=str(payload["user_goal"]),
            expected_delta_kinds=tuple(
                str(item) for item in payload.get("expected_delta_kinds", ()) or ()
            ),
            expected_gate_status=_optional_str(payload.get("expected_gate_status")),
            expected_activation_count=_optional_int(payload.get("expected_activation_count")),
            expected_outcomes=tuple(
                LearningEvalExpectedOutcome.from_payload(item)
                for item in payload.get("expected_outcomes", ()) or ()
            ),
            forbidden_events=tuple(str(item) for item in payload.get("forbidden_events", ()) or ()),
            required_events=tuple(str(item) for item in payload.get("required_events", ()) or ()),
            max_llm_calls=_optional_int(payload.get("max_llm_calls")),
            max_tool_calls=_optional_int(payload.get("max_tool_calls")),
            max_cost_usd=_optional_float(payload.get("max_cost_usd")),
            timeout_seconds=_optional_int(payload.get("timeout_seconds")),
            capsule=dict(payload.get("capsule", {}) or {}),
            mutation_gate_evidence=dict(payload.get("mutation_gate_evidence", {}) or {}),
            replay=dict(payload.get("replay", {}) or {}),
            tool_preflight=dict(payload.get("tool_preflight", {}) or {}),
            rollback_delta_id=_optional_str(payload.get("rollback_delta_id")),
            controlled_activation=bool(payload.get("controlled_activation", False)),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "provider_modes": list(self.provider_modes),
            "backend_modes": list(self.backend_modes),
            "initial_memory_records": list(self.initial_memory_records),
            "active_behavior_deltas": [
                delta.to_metadata() for delta in self.active_behavior_deltas
            ],
            "user_goal": self.user_goal,
            "expected_delta_kinds": list(self.expected_delta_kinds),
            "expected_gate_status": self.expected_gate_status,
            "expected_activation_count": self.expected_activation_count,
            "expected_outcomes": [item.to_payload() for item in self.expected_outcomes],
            "forbidden_events": list(self.forbidden_events),
            "required_events": list(self.required_events),
            "max_llm_calls": self.max_llm_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_cost_usd": self.max_cost_usd,
            "timeout_seconds": self.timeout_seconds,
            "rollback_delta_id": self.rollback_delta_id,
            "controlled_activation": self.controlled_activation,
        }


@dataclass(frozen=True)
class LearningEvalOptions:
    provider: str = "mock"
    model: str | None = None
    backend: str = "memory"
    memory_dir: Path | None = None
    workspace: Path | None = None
    report_path: Path | None = None
    max_cost_usd: float = 1.0
    max_llm_calls: int = 8
    max_tool_calls: int = 8
    timeout_seconds: int = 120
    keep_artifacts: bool = False
    dry_run: bool = False
    base_url: str | None = None
    api_key_env: str | None = None


@dataclass(frozen=True)
class LearningEvalResult:
    scenario_id: str
    title: str
    provider: str
    model: str
    backend: str
    status: StageStatus
    stages: tuple[LearningEvalStep, ...]
    llm_calls: int
    tool_calls: int
    estimated_cost_usd: float
    artifacts: dict[str, str] = field(default_factory=dict)
    deltas: dict[str, Any] = field(default_factory=dict)
    replay: dict[str, Any] = field(default_factory=dict)
    activations: tuple[dict[str, Any], ...] = ()
    outcomes: tuple[dict[str, Any], ...] = ()
    failures: tuple[str, ...] = ()
    skipped_reason: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def skipped(self) -> bool:
        return self.status == "skip"

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema": LEARNING_EVAL_SCHEMA,
            "scenario_id": self.scenario_id,
            "title": self.title,
            "provider": self.provider,
            "model": self.model,
            "backend": self.backend,
            "status": self.status,
            "passed": self.passed,
            "skipped": self.skipped,
            "skipped_reason": self.skipped_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stages": [stage.to_payload() for stage in self.stages],
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "artifacts": self.artifacts,
            "deltas": self.deltas,
            "replay": self.replay,
            "activations": list(self.activations),
            "outcomes": list(self.outcomes),
            "failures": list(self.failures),
        }
        return cast(dict[str, Any], redact_secrets(payload))


@dataclass(frozen=True)
class LearningEvalReport:
    results: tuple[LearningEvalResult, ...]
    report_path: Path | None = None

    @property
    def status(self) -> StageStatus:
        if any(result.status == "fail" for result in self.results):
            return "fail"
        if self.results and all(result.status == "skip" for result in self.results):
            return "skip"
        return "pass"

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema": LEARNING_EVAL_SCHEMA,
            "status": self.status,
            "summary": {
                "scenario_count": len(self.results),
                "passed": sum(1 for result in self.results if result.status == "pass"),
                "failed": sum(1 for result in self.results if result.status == "fail"),
                "skipped": sum(1 for result in self.results if result.status == "skip"),
                "llm_calls": sum(result.llm_calls for result in self.results),
                "tool_calls": sum(result.tool_calls for result in self.results),
                "estimated_cost_usd": round(
                    sum(result.estimated_cost_usd for result in self.results), 6
                ),
            },
            "results": [result.to_payload() for result in self.results],
            "report_path": str(self.report_path) if self.report_path else None,
        }
        return cast(dict[str, Any], redact_secrets(payload))


class EvalLimitExceeded(RuntimeError):
    pass


class _EvalLimits:
    def __init__(self, *, max_llm_calls: int, max_tool_calls: int, max_cost_usd: float) -> None:
        self.max_llm_calls = max_llm_calls
        self.max_tool_calls = max_tool_calls
        self.max_cost_usd = max_cost_usd
        self.llm_calls = 0
        self.tool_calls = 0
        self.estimated_cost_usd = 0.0

    def consume_llm_call(self) -> None:
        if self.max_cost_usd <= 0:
            raise EvalLimitExceeded("cost budget exhausted before the next LLM call")
        if self.llm_calls + 1 > self.max_llm_calls:
            raise EvalLimitExceeded(
                f"LLM call guard would be exceeded: {self.llm_calls + 1}>{self.max_llm_calls}"
            )
        self.llm_calls += 1

    def consume_tool_calls(self, count: int) -> None:
        if self.tool_calls + count > self.max_tool_calls:
            raise EvalLimitExceeded(
                f"tool call guard would be exceeded: {self.tool_calls + count}>{self.max_tool_calls}"
            )
        self.tool_calls += count

    def add_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        cost = _usage_cost(usage)
        self.estimated_cost_usd += cost
        if self.estimated_cost_usd > self.max_cost_usd:
            raise EvalLimitExceeded(
                f"cost guard exceeded: estimated {self.estimated_cost_usd:.6f}>{self.max_cost_usd:.6f}"
            )


@dataclass
class _RunContext:
    scenario: LearningEvalScenario
    options: LearningEvalOptions
    artifact_root: Path
    workspace: Path
    memory_dir: Path
    state: AgentStateStore
    ledger: BehaviorDeltaLedger
    config: AgentConfig
    limits: _EvalLimits
    run_id: str
    started: float
    agent: Any | None = None
    turn: Any | None = None
    capsule_payload: dict[str, Any] = field(default_factory=dict)
    proposals: list[BehaviorDelta] = field(default_factory=list)
    gate_decisions: dict[str, MutationDecision] = field(default_factory=dict)
    replay_result: dict[str, Any] = field(default_factory=dict)
    compiled_text: str = ""
    preflight_text: str = ""
    rollback_verified: bool = False


def load_learning_eval_scenario(
    value: str | Path, *, scenario_dir: Path = DEFAULT_SCENARIO_DIR
) -> LearningEvalScenario:
    path = Path(value)
    if not path.exists():
        path = scenario_dir / f"{value}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return LearningEvalScenario.from_payload(payload)


def list_learning_eval_scenarios(
    *, scenario_dir: Path = DEFAULT_SCENARIO_DIR
) -> list[LearningEvalScenario]:
    return [
        load_learning_eval_scenario(path, scenario_dir=scenario_dir)
        for path in sorted(scenario_dir.glob("*.json"))
    ]


def run_learning_eval_suite(
    scenarios: list[LearningEvalScenario],
    options: LearningEvalOptions,
) -> LearningEvalReport:
    results = [run_learning_eval(scenario, options) for scenario in scenarios]
    report = LearningEvalReport(results=tuple(results), report_path=options.report_path)
    if options.report_path is not None:
        write_learning_eval_markdown(report, options.report_path)
    return report


def run_learning_eval(
    scenario: LearningEvalScenario, options: LearningEvalOptions
) -> LearningEvalResult:
    started_at = datetime.now(UTC).isoformat()
    provider = options.provider
    model = resolve_eval_model(provider, options.model)
    compatibility_skip = _compatibility_skip(scenario, provider=provider, backend=options.backend)
    if compatibility_skip:
        return _skipped_result(
            scenario,
            options,
            model=model,
            reason=compatibility_skip,
            started_at=started_at,
        )
    live_skip = _live_provider_skip_reason(provider, options)
    if live_skip:
        return _skipped_result(
            scenario,
            options,
            model=model,
            reason=live_skip,
            started_at=started_at,
        )

    run_scope = f"{scenario.id}-{uuid4().hex[:12]}"
    artifact_root = _artifact_root(options) / run_scope
    workspace = (
        (options.workspace / run_scope)
        if options.workspace is not None
        else artifact_root / "workspace"
    )
    memory_dir = (
        (options.memory_dir / run_scope)
        if options.memory_dir is not None
        else workspace / ".nest" / "memory"
    )
    max_llm_calls = min(options.max_llm_calls, scenario.max_llm_calls or options.max_llm_calls)
    max_tool_calls = min(options.max_tool_calls, scenario.max_tool_calls or options.max_tool_calls)
    max_cost_usd = min(options.max_cost_usd, scenario.max_cost_usd or options.max_cost_usd)
    timeout_seconds = min(
        options.timeout_seconds, scenario.timeout_seconds or options.timeout_seconds
    )
    limits = _EvalLimits(
        max_llm_calls=max_llm_calls, max_tool_calls=max_tool_calls, max_cost_usd=max_cost_usd
    )
    state = AgentStateStore(workspace / ".nest" / "state" / "agent.db")
    ledger = BehaviorDeltaLedger(state)
    config = _eval_agent_config(
        options,
        provider=provider,
        model=model,
        backend=options.backend,
        workspace=workspace,
        memory_dir=memory_dir,
        timeout_seconds=timeout_seconds,
    )
    ctx = _RunContext(
        scenario=scenario,
        options=options,
        artifact_root=artifact_root,
        workspace=workspace,
        memory_dir=memory_dir,
        state=state,
        ledger=ledger,
        config=config,
        limits=limits,
        run_id=f"learning_eval_{scenario.id}_{uuid4().hex[:8]}",
        started=perf_counter(),
    )

    stages: list[LearningEvalStep] = []
    failures: list[str] = []
    try:
        for stage_name, stage_fn in (
            ("setup", _stage_setup),
            ("provider_smoke", _stage_provider_smoke),
            ("agent_run", _stage_agent_run),
            ("capsule_trace_extraction", _stage_capsule_trace_extraction),
            ("mutation_gate", _stage_mutation_gate),
            ("replay_validation", _stage_replay_validation),
            ("behavior_compilation", _stage_behavior_compilation),
            ("tool_aware_preflight", _stage_tool_aware_preflight),
            ("outcome_ledger", _stage_outcome_ledger),
            ("rollback", _stage_rollback),
        ):
            if options.dry_run and stage_name != "setup":
                stages.append(LearningEvalStep(stage_name, "skip", "dry-run skipped execution"))
                continue
            _check_timeout(ctx)
            try:
                stage = stage_fn(ctx)
            except EvalLimitExceeded as exc:
                stage = LearningEvalStep(stage_name, "fail", str(exc))
            except Exception as exc:  # noqa: BLE001 - eval boundary reports actionable diagnostics
                stage = LearningEvalStep(stage_name, "fail", _error_message(exc))
            stages.append(stage)
            if stage.status == "fail":
                failures.append(f"{stage.name}: {stage.message}")
                break
    finally:
        if ctx.agent is not None:
            ctx.agent.close()

    for event in scenario.forbidden_events:
        if _forbidden_event_seen(event, ctx):
            failures.append(f"forbidden event observed: {event}")
    stage_names = {stage.name for stage in stages if stage.status == "pass"}
    for event in scenario.required_events:
        if event in STAGE_NAMES and event not in stage_names:
            failures.append(f"required stage did not pass: {event}")

    status: StageStatus = (
        "fail" if failures or any(stage.status == "fail" for stage in stages) else "pass"
    )
    report = ctx.ledger.report_deltas().to_payload()
    activation_payloads: list[dict[str, Any]] = []
    outcome_payloads: list[dict[str, Any]] = []
    for delta in ctx.ledger.list_deltas():
        activation_payloads.extend(
            item.to_payload() for item in ctx.ledger.list_activations(delta.id)
        )
        outcome_payloads.extend(item.to_payload() for item in ctx.ledger.list_outcomes(delta.id))
    finished_at = datetime.now(UTC).isoformat()
    return LearningEvalResult(
        scenario_id=scenario.id,
        title=scenario.title,
        provider=provider,
        model=model,
        backend=options.backend,
        status=status,
        stages=tuple(stages),
        llm_calls=limits.llm_calls,
        tool_calls=limits.tool_calls,
        estimated_cost_usd=limits.estimated_cost_usd,
        artifacts=_artifact_payload(ctx),
        deltas=report,
        replay=ctx.replay_result,
        activations=tuple(redact_secrets(item) for item in activation_payloads),
        outcomes=tuple(redact_secrets(item) for item in outcome_payloads),
        failures=tuple(redact_secrets(failure) for failure in failures),
        started_at=started_at,
        finished_at=finished_at,
    )


def write_learning_eval_markdown(report: LearningEvalReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_payload()
    lines = [
        "# Kestrel Learning Architecture Eval",
        "",
        f"Status: `{payload['status']}`",
        "",
        "## Summary",
        "",
    ]
    summary = payload["summary"]
    for key in (
        "scenario_count",
        "passed",
        "failed",
        "skipped",
        "llm_calls",
        "tool_calls",
        "estimated_cost_usd",
    ):
        lines.append(f"- {key}: {summary[key]}")
    for result in payload["results"]:
        lines.extend(
            [
                "",
                f"## {result['scenario_id']}",
                "",
                f"Title: {result['title']}",
                f"Provider/backend/model: `{result['provider']}` / `{result['backend']}` / `{result['model']}`",
                f"Status: `{result['status']}`",
                f"LLM calls: {result['llm_calls']}",
                f"Tool calls: {result['tool_calls']}",
                f"Estimated cost USD: {result['estimated_cost_usd']}",
                "",
                "### Stages",
                "",
            ]
        )
        for stage in result["stages"]:
            message = f" - {stage['message']}" if stage.get("message") else ""
            lines.append(f"- `{stage['name']}`: `{stage['status']}`{message}")
        delta_summary = result.get("deltas", {}).get("summary", {})
        if delta_summary:
            lines.extend(
                [
                    "",
                    "### Behavior Deltas",
                    "",
                    f"- proposed/staged/active/rejected/rolled_back: {_delta_status_counts(result.get('deltas', {}))}",
                    f"- active_deltas: {delta_summary.get('active_deltas', 0)}",
                    f"- activated_deltas: {delta_summary.get('activated_deltas', 0)}",
                    f"- outcomes: {delta_summary.get('outcomes', {})}",
                ]
            )
        if result.get("replay"):
            replay = result["replay"]
            lines.extend(
                [
                    "",
                    "### Replay",
                    "",
                    f"- baseline_score: {replay.get('baseline_score')}",
                    f"- delta_score: {replay.get('delta_score')}",
                    f"- improvement: {replay.get('improvement')}",
                ]
            )
        if result.get("failures"):
            lines.extend(["", "### Failures", ""])
            for failure in result["failures"]:
                lines.append(f"- {failure}")
        artifacts = result.get("artifacts", {})
        if artifacts:
            lines.extend(["", "### Artifacts", ""])
            for key, value in artifacts.items():
                lines.append(f"- {key}: `{value}`")
    path.write_text(redact_secrets("\n".join(lines).rstrip() + "\n"), encoding="utf-8")


def resolve_eval_model(provider: str, model: str | None) -> str:
    if model:
        return model
    if os.getenv("NEST_AGENT_EVAL_MODEL"):
        return str(os.getenv("NEST_AGENT_EVAL_MODEL"))
    if provider == "mock":
        return "mock"
    if provider in DEFAULT_LIVE_MODEL_BY_PROVIDER:
        return DEFAULT_LIVE_MODEL_BY_PROVIDER[provider]
    fallback = STATIC_MODEL_SUGGESTIONS.get(provider, ())
    return fallback[0] if fallback else "model"


def redact_secrets(value: Any) -> Any:
    return _boundary_redact_secrets(value)


def _stage_setup(ctx: _RunContext) -> LearningEvalStep:
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    ctx.memory_dir.mkdir(parents=True, exist_ok=True)
    for delta in ctx.scenario.active_behavior_deltas:
        ctx.ledger.record_delta(delta)
    ctx.agent = build_agent(ctx.config, state=ctx.state)
    memory_writes = []
    for index, payload in enumerate(ctx.scenario.initial_memory_records, start=1):
        record = _memory_record_from_payload(payload, index=index)
        memory_writes.append(ctx.agent.memory.put(record))
    return LearningEvalStep(
        "setup",
        "pass",
        "isolated workspace, memory, state, and ledger ready",
        metrics={
            "workspace": str(ctx.workspace),
            "memory_dir": str(ctx.memory_dir),
            "seeded_memory_records": len(memory_writes),
            "seeded_active_behavior_deltas": len(ctx.scenario.active_behavior_deltas),
        },
    )


def _stage_provider_smoke(ctx: _RunContext) -> LearningEvalStep:
    ctx.limits.consume_llm_call()
    start = perf_counter()
    provider = ctx.agent.llm if ctx.agent is not None else build_llm_provider(ctx.config)
    response = provider.generate(
        [
            ChatMessage(role="system", content="You are running a guarded Kestrel learning eval."),
            ChatMessage(role="user", content="Reply with a short readiness acknowledgement."),
        ],
        [],
        LLMOptions(timeout_seconds=ctx.config.timeout_seconds, max_retries=0, temperature=0.0),
    )
    ctx.limits.add_usage(response.usage)
    return LearningEvalStep(
        "provider_smoke",
        "pass" if response.content.strip() else "fail",
        "provider returned a valid response"
        if response.content.strip()
        else "provider returned empty content",
        metrics={
            "provider": ctx.config.provider,
            "model": ctx.config.model,
            "latency_ms": round((perf_counter() - start) * 1000, 2),
            "usage": response.usage or {},
        },
    )


def _stage_agent_run(ctx: _RunContext) -> LearningEvalStep:
    if ctx.agent is None:
        return LearningEvalStep("agent_run", "fail", "agent was not initialized")
    simulated_tools = _simulated_tool_executions(ctx.scenario)
    ctx.limits.consume_tool_calls(len(simulated_tools))
    ctx.limits.consume_llm_call()
    start = perf_counter()
    ctx.turn = ctx.agent.chat(
        ctx.scenario.user_goal,
        session_id=f"learning_eval_{ctx.scenario.id}",
        run_id=ctx.run_id,
    )
    return LearningEvalStep(
        "agent_run",
        "pass"
        if ctx.turn.stop_reason in {"complete", "tool_error", "approval_required"}
        else "fail",
        f"agent stop_reason={ctx.turn.stop_reason}",
        metrics={
            "run_id": ctx.run_id,
            "latency_ms": round((perf_counter() - start) * 1000, 2),
            "memory_writes": len(ctx.turn.memory_writes),
            "tool_calls": len(ctx.turn.tool_executions) + len(simulated_tools),
            "stop_reason": ctx.turn.stop_reason,
        },
    )


def _stage_capsule_trace_extraction(ctx: _RunContext) -> LearningEvalStep:
    if ctx.turn is None:
        return LearningEvalStep("capsule_trace_extraction", "fail", "agent run is missing")
    simulated_tools = _simulated_tool_executions(ctx.scenario)
    capsule = _capsule_payload(ctx, simulated_tools)
    capsule_path = write_run_capsule(
        runs_dir=ctx.artifact_root / "runs",
        run_id=ctx.run_id,
        objective=ctx.scenario.user_goal,
        backend=ctx.options.backend,
        selected_context=ctx.turn.context_prompt,
        tool_executions=tuple(ctx.turn.tool_executions) + tuple(simulated_tools),
        final_response=ctx.turn.assistant_message,
        errors_encountered=tuple(str(item) for item in capsule.get("errors_encountered", ()) or ()),
        unresolved_questions=tuple(
            str(item) for item in capsule.get("unresolved_questions", ()) or ()
        ),
        reusable_lessons=tuple(str(item) for item in capsule.get("reusable_lessons", ()) or ()),
        candidate_facts=tuple(str(item) for item in capsule.get("candidate_facts", ()) or ()),
        candidate_procedures=tuple(
            str(item) for item in capsule.get("candidate_procedures", ()) or ()
        ),
        candidate_corrections=tuple(
            str(item) for item in capsule.get("candidate_corrections", ()) or ()
        ),
        candidate_policy_items=tuple(
            str(item) for item in capsule.get("candidate_policy_items", ()) or ()
        ),
    )
    summary = summarize_run_capsule(
        runs_dir=ctx.artifact_root / "runs", run_id=ctx.run_id, backend=ctx.options.backend
    )
    ctx.capsule_payload = {
        **capsule,
        "run_id": ctx.run_id,
        "objective": ctx.scenario.user_goal,
        "tool_calls": [
            _tool_execution_payload(execution)
            for execution in tuple(ctx.turn.tool_executions) + tuple(simulated_tools)
        ],
    }
    ctx.proposals = BehaviorDeltaExtractor(ledger=ctx.ledger).propose_from_capsule(
        ctx.capsule_payload,
        run_id=ctx.run_id,
        dry_run=False,
    )
    found_kinds = {delta.kind.value for delta in ctx.proposals}
    missing = [kind for kind in ctx.scenario.expected_delta_kinds if kind not in found_kinds]
    status: StageStatus = "fail" if missing else "pass"
    return LearningEvalStep(
        "capsule_trace_extraction",
        status,
        "capsule and behavior proposals extracted"
        if not missing
        else f"missing expected delta kinds: {missing}",
        metrics={
            "capsule_path": str(capsule_path),
            "summary_signal_count": len(summary.learning_signals),
            "proposal_count": len(ctx.proposals),
            "proposal_kinds": sorted(found_kinds),
        },
        artifacts={"capsule": str(capsule_path)},
    )


def _stage_mutation_gate(ctx: _RunContext) -> LearningEvalStep:
    candidates = ctx.proposals or list(ctx.scenario.active_behavior_deltas)
    if not candidates:
        return LearningEvalStep("mutation_gate", "skip", "no behavior deltas to evaluate")
    evidence = _mutation_gate_evidence(ctx.scenario)
    gate = MutationGate()
    decisions = {delta.id: gate.evaluate(delta, evidence) for delta in candidates}
    ctx.gate_decisions.update(decisions)
    for delta in candidates:
        decision = decisions[delta.id]
        if decision.status == BehaviorDeltaStatus.ACTIVE and not ctx.scenario.controlled_activation:
            continue
        stored_delta = ctx.ledger.get_delta(delta.id)
        if stored_delta is not None and stored_delta.status != decision.status:
            ctx.ledger.update_delta_status(
                delta.id, decision.status, reason=f"learning eval mutation gate: {decision.reason}"
            )
    expected = ctx.scenario.expected_gate_status
    observed = {decision.status.value for decision in decisions.values()}
    status: StageStatus = "pass"
    message = "mutation gate decisions recorded"
    if expected and expected not in observed:
        status = "fail"
        message = f"expected gate status {expected}, observed {sorted(observed)}"
    return LearningEvalStep(
        "mutation_gate",
        status,
        message,
        metrics={
            "decisions": {
                delta_id: {
                    "status": decision.status.value,
                    "accepted": decision.accepted,
                    "reason": decision.reason,
                    "blocked_by": list(decision.blocked_by),
                    "requires_replay": decision.requires_replay,
                    "requires_human_approval": decision.requires_human_approval,
                    "requires_exact_call_approval": decision.requires_exact_call_approval,
                }
                for delta_id, decision in decisions.items()
            }
        },
    )


def _stage_replay_validation(ctx: _RunContext) -> LearningEvalStep:
    expected = tuple(str(item) for item in ctx.scenario.replay.get("expected_behavior", ()) or ())
    failures = tuple(str(item) for item in ctx.scenario.replay.get("failure_conditions", ()) or ())
    candidates = ctx.proposals or list(ctx.scenario.active_behavior_deltas)
    if not expected or not candidates:
        ctx.replay_result = {
            "baseline_score": 1.0,
            "delta_score": 1.0,
            "improvement": 0.0,
            "skipped": True,
        }
        return LearningEvalStep(
            "replay_validation",
            "skip",
            "no replay expectations configured",
            metrics=ctx.replay_result,
        )
    delta_text = "\n".join(delta.behavior_change for delta in candidates)
    baseline_text = (
        f"BASELINE RUN: {ctx.scenario.user_goal}. No behavior delta instructions were compiled."
    )
    baseline_score = _score_text(baseline_text, expected)
    delta_score = _score_text(delta_text, expected)
    violations = tuple(
        condition for condition in failures if _phrase_matches(delta_text, condition)
    )
    improvement = round(delta_score - baseline_score, 4)
    ctx.replay_result = {
        "baseline_score": baseline_score,
        "delta_score": delta_score,
        "improvement": improvement,
        "expected_behavior_hits": _hit_count(delta_text, expected),
        "expected_behavior_total": len(expected),
        "gate_violations": list(violations),
    }
    status: StageStatus = "pass" if improvement >= 0 and not violations else "fail"
    return LearningEvalStep(
        "replay_validation",
        status,
        "replay compared baseline and behavior-with-delta",
        metrics=ctx.replay_result,
    )


def _stage_behavior_compilation(ctx: _RunContext) -> LearningEvalStep:
    active = ctx.ledger.list_deltas(status=BehaviorDeltaStatus.ACTIVE)
    if not active:
        ctx.compiled_text = ""
        return LearningEvalStep(
            "behavior_compilation",
            "pass",
            "no active deltas compiled; proposed/staged deltas stayed inactive",
            metrics={"active_delta_count": 0, "compiled_delta_ids": []},
        )
    compiler = BehaviorCompiler(
        ledger=ctx.ledger, config=BehaviorCompilerConfig(enabled=True, log_activations=False)
    )
    tool_name = str(ctx.scenario.tool_preflight.get("tool_name", ""))
    compiled = compiler.compile(
        BehaviorCompileRequest(
            objective=ctx.scenario.user_goal,
            query=ctx.scenario.user_goal,
            run_id=f"{ctx.run_id}_compile",
            tool_names=(tool_name,) if tool_name else (),
            task_type=_optional_str(ctx.scenario.tool_preflight.get("task_type")),
        )
    )
    ctx.compiled_text = compiled.text
    missing = [
        delta.id for delta in active if delta.id not in {item.id for item in compiled.deltas}
    ]
    status: StageStatus = "fail" if missing else "pass"
    return LearningEvalStep(
        "behavior_compilation",
        status,
        "active deltas compiled into runtime context"
        if not missing
        else f"active deltas did not compile: {missing}",
        metrics={
            "active_delta_count": len(active),
            "compiled_delta_ids": [delta.id for delta in compiled.deltas],
        },
    )


def _stage_tool_aware_preflight(ctx: _RunContext) -> LearningEvalStep:
    spec = ctx.scenario.tool_preflight
    if not spec:
        return LearningEvalStep("tool_aware_preflight", "skip", "no tool preflight configured")
    if ctx.agent is None:
        return LearningEvalStep("tool_aware_preflight", "fail", "agent was not initialized")
    ctx.limits.consume_tool_calls(1)
    call = ToolCall(
        name=str(spec.get("tool_name", "test.run")),
        arguments=dict(spec.get("arguments", {}) or {}),
        id=str(spec.get("tool_call_id", "learning-eval-tool-call")),
    )
    previous = tuple(
        _tool_execution_from_payload(item, index=index)
        for index, item in enumerate(spec.get("previous_failures", ()) or (), start=1)
    )
    first = ctx.agent.tool_preflight_for_call(
        objective=ctx.scenario.user_goal,
        call=call,
        run_id=ctx.run_id,
        task_id=str(spec.get("task_id", "learning-eval-task")),
        previous_executions=previous,
    )
    second = ctx.agent.tool_preflight_for_call(
        objective=ctx.scenario.user_goal,
        call=call,
        run_id=ctx.run_id,
        task_id=str(spec.get("task_id", "learning-eval-task")),
        previous_executions=previous,
    )
    ctx.preflight_text = second.text or first.text
    delta_ids = [delta.id for delta in first.deltas]
    activation_count = sum(
        1
        for delta_id in delta_ids
        for activation in ctx.ledger.list_activations(delta_id)
        if activation.compiled_section.startswith("TOOL BEHAVIOR-DELTA PREFLIGHT")
    )
    expected_count = ctx.scenario.expected_activation_count
    status: StageStatus = "pass"
    message = "tool preflight compiled relevant active deltas"
    if expected_count is not None and activation_count != expected_count:
        status = "fail"
        message = f"expected activation count {expected_count}, observed {activation_count}"
    if expected_count and not first.text:
        status = "fail"
        message = "expected preflight text but none was compiled"
    return LearningEvalStep(
        "tool_aware_preflight",
        status,
        message,
        metrics={
            "compiled_delta_ids": delta_ids,
            "activation_count": activation_count,
            "preflight_chars": len(first.text),
            "contains_evidence": "DELTA EVIDENCE" in first.text,
        },
    )


def _stage_outcome_ledger(ctx: _RunContext) -> LearningEvalStep:
    if not ctx.scenario.expected_outcomes:
        summary = ctx.ledger.summarize_deltas().to_payload()
        return LearningEvalStep(
            "outcome_ledger", "pass", "no explicit outcomes requested", metrics=summary
        )
    for expected in ctx.scenario.expected_outcomes:
        delta_id = expected.delta_id or _first_delta_id(ctx)
        if delta_id is None:
            return LearningEvalStep(
                "outcome_ledger", "fail", f"no delta available for outcome {expected.name}"
            )
        ctx.ledger.record_outcome(
            BehaviorDeltaOutcome(
                id=f"out_{delta_id}_{expected.name}_{uuid4().hex[:8]}",
                delta_id=delta_id,
                run_id=ctx.run_id,
                outcome=expected.name,  # type: ignore[arg-type]
                recorded_at=utc_now(),
                evidence_ref=EvidenceRef(
                    source="learning_eval", locator=f"{ctx.scenario.id}:outcome"
                ),
                notes=expected.notes or f"learning eval recorded {expected.name}",
            )
        )
    summary = ctx.ledger.summarize_deltas().to_payload()
    missing = [
        item.name
        for item in ctx.scenario.expected_outcomes
        if summary["outcomes"].get(item.name, 0) < 1
    ]
    status: StageStatus = "fail" if missing else "pass"
    return LearningEvalStep(
        "outcome_ledger",
        status,
        "outcome ledger summary verified" if not missing else f"missing outcomes: {missing}",
        metrics=summary,
    )


def _stage_rollback(ctx: _RunContext) -> LearningEvalStep:
    if not ctx.scenario.rollback_delta_id:
        return LearningEvalStep("rollback", "skip", "no rollback configured")
    delta_id = ctx.scenario.rollback_delta_id
    before = ctx.ledger.get_delta(delta_id)
    if before is None:
        return LearningEvalStep("rollback", "fail", f"unknown rollback delta: {delta_id}")
    ctx.ledger.update_delta_status(
        delta_id, BehaviorDeltaStatus.ROLLED_BACK, reason="learning eval rollback"
    )
    ctx.ledger.record_outcome(
        BehaviorDeltaOutcome(
            id=f"out_{delta_id}_rolled_back_{uuid4().hex[:8]}",
            delta_id=delta_id,
            run_id=ctx.run_id,
            outcome="rolled_back",
            recorded_at=utc_now(),
            evidence_ref=EvidenceRef(source="learning_eval", locator=f"{ctx.scenario.id}:rollback"),
            notes="rollback disabled the active test delta without deleting audit history",
        )
    )
    compiler = BehaviorCompiler(ledger=ctx.ledger, config=BehaviorCompilerConfig(enabled=True))
    compiled = compiler.compile(
        BehaviorCompileRequest(
            objective=ctx.scenario.user_goal,
            query=ctx.scenario.user_goal,
            run_id=f"{ctx.run_id}_rollback",
        )
    )
    after = ctx.ledger.get_delta(delta_id)
    ignored = delta_id not in {delta.id for delta in compiled.deltas}
    history = bool(ctx.ledger.list_outcomes(delta_id)) and after is not None
    ctx.rollback_verified = bool(
        after and after.status == BehaviorDeltaStatus.ROLLED_BACK and ignored and history
    )
    return LearningEvalStep(
        "rollback",
        "pass" if ctx.rollback_verified else "fail",
        "rollback disabled delta and preserved audit history"
        if ctx.rollback_verified
        else "rollback verification failed",
        metrics={
            "before_status": before.status.value,
            "after_status": after.status.value if after else None,
            "future_compilation_ignored_delta": ignored,
            "audit_history_preserved": history,
        },
    )


def _skipped_result(
    scenario: LearningEvalScenario,
    options: LearningEvalOptions,
    *,
    model: str,
    reason: str,
    started_at: str,
) -> LearningEvalResult:
    stages = tuple(LearningEvalStep(name, "skip", reason) for name in STAGE_NAMES)
    return LearningEvalResult(
        scenario_id=scenario.id,
        title=scenario.title,
        provider=options.provider,
        model=model,
        backend=options.backend,
        status="skip",
        stages=stages,
        llm_calls=0,
        tool_calls=0,
        estimated_cost_usd=0.0,
        failures=(),
        skipped_reason=reason,
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
    )


def _compatibility_skip(
    scenario: LearningEvalScenario, *, provider: str, backend: str
) -> str | None:
    provider_mode: ProviderMode = "mock" if provider == "mock" else "live"
    if "both" not in scenario.provider_modes and provider_mode not in scenario.provider_modes:
        return (
            f"scenario supports provider modes {list(scenario.provider_modes)}, not {provider_mode}"
        )
    backend_mode: BackendMode = "memvid" if backend == "memvid" else "memory"
    if "both" not in scenario.backend_modes and backend_mode not in scenario.backend_modes:
        return f"scenario supports backend modes {list(scenario.backend_modes)}, not {backend_mode}"
    return None


def _live_provider_skip_reason(provider: str, options: LearningEvalOptions) -> str | None:
    if provider == "mock":
        return None
    if os.getenv("RUN_LIVE_LEARNING_EVALS") != "1":
        return "live learning evals require RUN_LIVE_LEARNING_EVALS=1"
    if provider == "openai":
        key_env = options.api_key_env or DEFAULT_API_KEY_ENVS.get("openai") or "OPENAI_API_KEY"
        if not os.getenv(key_env):
            return f"provider=openai requires {key_env}"
    if provider == "openai-compatible":
        base_url = (
            options.base_url
            or os.getenv("NEST_AGENT_BASE_URL")
            or os.getenv("OPENAI_COMPATIBLE_BASE_URL")
        )
        if not base_url:
            return "provider=openai-compatible requires --base-url, NEST_AGENT_BASE_URL, or OPENAI_COMPATIBLE_BASE_URL"
    return None


def _artifact_root(options: LearningEvalOptions) -> Path:
    if options.workspace is not None:
        return options.workspace / ".nest" / "evals" / "learning_architecture"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".nest/evals/learning_architecture") / timestamp


def _eval_agent_config(
    options: LearningEvalOptions,
    *,
    provider: str,
    model: str,
    backend: str,
    workspace: Path,
    memory_dir: Path,
    timeout_seconds: int,
) -> AgentConfig:
    return AgentConfig(
        provider=provider,
        model=model,
        backend=backend,
        base_url=options.base_url
        or os.getenv("NEST_AGENT_BASE_URL")
        or os.getenv("OPENAI_COMPATIBLE_BASE_URL"),
        api_key_env=options.api_key_env,
        timeout_seconds=timeout_seconds,
        max_retries=0,
        temperature=0.0,
        memory_dir=memory_dir,
        log_dir=workspace / ".nest" / "logs",
        state_path=workspace / ".nest" / "state" / "agent.db",
        secret_store_path=workspace / ".nest" / "secrets" / "local_vault.json",
        workspace=workspace,
        skills_dir=workspace / ".nest" / "skills",
        plugins_dir=workspace / ".nest" / "plugins",
        mcp_config_path=workspace / ".nest" / "config" / "mcp_servers.json",
        channel_config_path=workspace / ".nest" / "config" / "channels.json",
        worker_worktree_dir=workspace / ".nest" / "worktrees",
        max_tool_rounds=0,
        allow_shell=False,
        allow_file_write=False,
        allow_policy_writes=False,
        allow_remote_mutation=False,
        enable_task_capsules=True,
        enable_behavior_deltas=True,
        memory_seal_write_threshold=1,
        memory_seal_interval_seconds=0.0,
        tool_retry_max_attempts=0,
    )


def _memory_record_from_payload(payload: dict[str, Any], *, index: int) -> MemoryRecord:
    layer = MemoryLayer(str(payload.get("layer", MemoryLayer.WORKING.value)))
    kind = MemoryKind(str(payload.get("kind", MemoryKind.OBSERVATION.value)))
    evidence = payload.get("evidence", ()) or ()
    return MemoryRecord(
        id=str(payload.get("id", f"eval_seed_{index}")),
        title=str(payload.get("title", f"Eval seed {index}")),
        content=str(payload["content"]),
        layer=layer,
        kind=kind,
        confidence=float(payload.get("confidence", 0.75)),
        importance=float(payload.get("importance", 0.6)),
        tags={str(key): str(value) for key, value in dict(payload.get("tags", {}) or {}).items()},
        metadata=dict(payload.get("metadata", {}) or {}),
        evidence=[
            EvidenceRef(
                source=str(item.get("source", "fixture")),
                locator=str(item.get("locator", f"seed:{index}")),
                quote=_optional_str(item.get("quote")),
            )
            for item in evidence
            if isinstance(item, dict)
        ],
    )


def _capsule_payload(ctx: _RunContext, simulated_tools: list[ToolExecution]) -> dict[str, Any]:
    del simulated_tools
    return dict(ctx.scenario.capsule or {})


def _simulated_tool_executions(scenario: LearningEvalScenario) -> list[ToolExecution]:
    return [
        _tool_execution_from_payload(item, index=index)
        for index, item in enumerate(scenario.capsule.get("tool_calls", ()) or (), start=1)
        if isinstance(item, dict)
    ]


def _tool_execution_from_payload(payload: dict[str, Any], *, index: int) -> ToolExecution:
    return ToolExecution(
        call=ToolCall(
            name=str(payload.get("tool") or payload.get("tool_name") or "test.run"),
            arguments=dict(payload.get("arguments", {}) or {}),
            id=str(payload.get("tool_call_id", f"sim_tool_{index}")),
        ),
        success=bool(payload.get("success", False)),
        content=str(payload.get("content") or payload.get("error") or "simulated tool result"),
        data=dict(payload.get("data", {}) or {}),
        error=_optional_str(payload.get("error"))
        if not bool(payload.get("success", False))
        else None,
    )


def _tool_execution_payload(execution: ToolExecution) -> dict[str, Any]:
    return {
        "tool": execution.call.name,
        "tool_call_id": execution.call.id,
        "arguments": execution.call.arguments,
        "success": execution.success,
        "content": execution.content,
        "data": execution.data,
        "error": execution.error,
    }


def _mutation_gate_evidence(scenario: LearningEvalScenario) -> MutationGateEvidence:
    payload = scenario.mutation_gate_evidence
    return MutationGateEvidence(
        validation_score=float(payload.get("validation_score", 0.0)),
        repeat_count=int(payload.get("repeat_count", 1)),
        explicit_instruction=bool(payload.get("explicit_instruction", False)),
        reviewed_rule=bool(payload.get("reviewed_rule", False)),
        replay_passed=bool(payload.get("replay_passed", False)),
        policy_delta_activation_enabled=bool(payload.get("policy_delta_activation_enabled", False)),
        critical_delta_activation_enabled=bool(
            payload.get("critical_delta_activation_enabled", False)
        ),
        exact_call_approved=bool(payload.get("exact_call_approved", False)),
        human_approved=bool(payload.get("human_approved", False)),
    )


def _first_delta_id(ctx: _RunContext) -> str | None:
    deltas = ctx.ledger.list_deltas()
    return deltas[0].id if deltas else None


def _artifact_payload(ctx: _RunContext) -> dict[str, str]:
    payload = {
        "artifact_root": str(ctx.artifact_root),
        "workspace": str(ctx.workspace),
        "memory_dir": str(ctx.memory_dir),
    }
    capsule = ctx.artifact_root / "runs" / ctx.run_id / "complete.mv2"
    if capsule.exists():
        payload["capsule"] = str(capsule)
    return payload


def _forbidden_event_seen(event: str, ctx: _RunContext) -> bool:
    if event == "policy_delta_active":
        return any(
            delta.status == BehaviorDeltaStatus.ACTIVE
            for delta in ctx.ledger.list_deltas(kind=BehaviorDeltaKind.POLICY)
        )
    if event == "approval_gate_rule_active":
        return any(
            delta.status == BehaviorDeltaStatus.ACTIVE
            for delta in ctx.ledger.list_deltas(kind=BehaviorDeltaKind.APPROVAL_GATE_RULE)
        )
    if event == "mv2_replaced":
        return any(
            "replace .mv2" in delta.behavior_change.lower()
            for delta in ctx.ledger.list_deltas(status=BehaviorDeltaStatus.ACTIVE)
        )
    if event == "secret_leak":
        return _contains_secret(json.dumps(ctx.ledger.report_deltas().to_payload(), default=str))
    return False


def _check_timeout(ctx: _RunContext) -> None:
    timeout_seconds = ctx.config.timeout_seconds
    if perf_counter() - ctx.started > timeout_seconds:
        raise EvalLimitExceeded(f"timeout guard exceeded after {timeout_seconds} seconds")


def _score_text(text: str, expected_behavior: tuple[str, ...]) -> float:
    if not expected_behavior:
        return 1.0
    return round(_hit_count(text, expected_behavior) / len(expected_behavior), 4)


def _hit_count(text: str, expectations: tuple[str, ...]) -> int:
    return sum(1 for expectation in expectations if _phrase_matches(text, expectation))


def _phrase_matches(text: str, phrase: str) -> bool:
    text_tokens = set(_tokens(text))
    phrase_tokens = [token for token in _tokens(phrase) if token not in _STOPWORDS]
    if not phrase_tokens:
        return False
    return all(token in text_tokens for token in phrase_tokens)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_.-]+", text.lower())


_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "before",
    "for",
    "in",
    "of",
    "or",
    "the",
    "to",
    "with",
}


def _delta_status_counts(report: dict[str, Any]) -> str:
    counts = {status: 0 for status in ("proposed", "staged", "active", "rejected", "rolled_back")}
    for row in report.get("deltas", ()):
        status = str(row.get("status", ""))
        if status in counts:
            counts[status] += 1
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def _delta_from_fixture(payload: dict[str, Any]) -> BehaviorDelta:
    normalized = dict(payload)
    normalized.setdefault("status", BehaviorDeltaStatus.ACTIVE.value)
    normalized.setdefault("rollback_plan", {"can_disable": True})
    normalized.setdefault("activation_stats", {})
    normalized.setdefault("confidence", 0.8)
    normalized.setdefault("importance", 0.7)
    return behavior_delta_from_metadata(normalized)


def _provider_modes(value: object) -> tuple[ProviderMode, ...]:
    values = (
        tuple(str(item) for item in value) if isinstance(value, list | tuple) else (str(value),)
    )
    for item in values:
        if item not in {"mock", "live", "both"}:
            raise ValueError(f"invalid provider mode: {item}")
    return values  # type: ignore[return-value]


def _backend_modes(value: object) -> tuple[BackendMode, ...]:
    values = (
        tuple(str(item) for item in value) if isinstance(value, list | tuple) else (str(value),)
    )
    for item in values:
        if item not in {"memory", "memvid", "both"}:
            raise ValueError(f"invalid backend mode: {item}")
    return values  # type: ignore[return-value]


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, str | bytes | bytearray | int | float):
        return int(value)
    raise TypeError(f"expected int-compatible value, got {type(value).__name__}")


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, str | bytes | bytearray | int | float):
        return float(value)
    raise TypeError(f"expected float-compatible value, got {type(value).__name__}")


def _usage_cost(usage: dict[str, Any]) -> float:
    for key in ("cost_usd", "estimated_cost_usd", "total_cost_usd"):
        value = usage.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def _error_message(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        return cast(str, redact_secrets(f"provider error ({exc.code}): {exc}"))
    return cast(str, redact_secrets(f"{type(exc).__name__}: {exc}"))


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|authorization)\s*[:=]\s*)[^\s,;]+"),
)


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)
