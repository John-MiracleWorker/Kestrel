from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.app_factory import build_agent
from nested_memvid_agent.behavior_compiler import (
    BehaviorCompiler,
    BehaviorCompilerConfig,
    BehaviorCompileRequest,
)
from nested_memvid_agent.behavior_delta import (
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
    TriggerSpec,
    ValidationPlan,
)
from nested_memvid_agent.behavior_delta_ledger import BehaviorDeltaLedger
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.factory import build_llm_provider
from nested_memvid_agent.llm.model_catalog import DEFAULT_API_KEY_ENVS, PROVIDER_OPTIONS
from nested_memvid_agent.models import EvidenceRef, MemoryKind, MemoryLayer, RetrievalQuery
from nested_memvid_agent.nested_learning import LearningSignal, NestedLearningKernel
from nested_memvid_agent.runtime_models import ChatMessage, LLMOptions, ToolCall
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.task_capsule import summarize_run_capsule, write_run_capsule
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools

DEFAULT_MODEL_ENV_BY_PROVIDER: dict[str, str] = {
    "openai": "KESTREL_IT_OPENAI_MODEL",
    "anthropic": "KESTREL_IT_ANTHROPIC_MODEL",
    "gemini": "KESTREL_IT_GEMINI_MODEL",
    "openrouter": "KESTREL_IT_OPENROUTER_MODEL",
    "ollama-cloud": "KESTREL_IT_OLLAMA_CLOUD_MODEL",
    "deepseek": "KESTREL_IT_DEEPSEEK_MODEL",
    "kimi": "KESTREL_IT_KIMI_MODEL",
    "openai-compatible": "KESTREL_IT_OPENAI_COMPATIBLE_MODEL",
    "ollama": "KESTREL_IT_OLLAMA_MODEL",
}


@dataclass(frozen=True)
class ProviderReadiness:
    provider: str
    model: str
    available: bool
    reason: str
    api_key_env: str | None = None
    model_env: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "available": self.available,
            "reason": self.reason,
            "api_key_env": self.api_key_env,
            "model_env": self.model_env,
            "api_key_configured": bool(self.api_key_env and os.getenv(self.api_key_env)),
        }


@dataclass(frozen=True)
class LiveEvalCaseResult:
    name: str
    passed: bool
    latency_ms: float = 0.0
    metrics: dict[str, object] = field(default_factory=dict)
    error: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "passed": self.passed,
            "latency_ms": round(self.latency_ms, 2),
            "metrics": self.metrics,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def provider_readiness(provider: str, *, model: str | None = None) -> ProviderReadiness:
    if provider not in PROVIDER_OPTIONS:
        return ProviderReadiness(provider=provider, model=model or "", available=False, reason=f"unsupported provider: {provider}")
    model_env = DEFAULT_MODEL_ENV_BY_PROVIDER.get(provider)
    resolved_model = model or (os.getenv(model_env) if model_env else None) or ""
    key_env = DEFAULT_API_KEY_ENVS.get(provider)
    missing: list[str] = []
    if key_env and not os.getenv(key_env):
        missing.append(key_env)
    if not resolved_model:
        missing.append(model_env or "--model")
    if missing:
        return ProviderReadiness(
            provider=provider,
            model=resolved_model,
            available=False,
            reason="missing required configuration: " + ", ".join(missing),
            api_key_env=key_env,
            model_env=model_env,
        )
    return ProviderReadiness(
        provider=provider,
        model=resolved_model,
        available=True,
        reason="ready",
        api_key_env=key_env,
        model_env=model_env,
    )


def build_live_eval_config(
    *,
    provider: str,
    model: str,
    backend: str,
    output_root: Path,
    timeout_seconds: int = 120,
) -> AgentConfig:
    return AgentConfig(
        provider=provider,
        model=model,
        backend=backend,
        memory_dir=output_root / "memory",
        log_dir=output_root / "logs",
        state_path=output_root / "state" / "agent.db",
        secret_store_path=output_root / "secrets" / "local_vault.json",
        workspace=output_root / "workspace",
        timeout_seconds=timeout_seconds,
        max_retries=0,
        temperature=0.0,
        allow_shell=False,
        allow_file_write=False,
        allow_policy_writes=False,
        allow_remote_mutation=False,
        enable_task_capsules=True,
        enable_agentic_cycle=True,
        enable_behavior_deltas=True,
        memory_seal_write_threshold=1,
        memory_seal_interval_seconds=0.0,
    )


def summarize_results(results: list[LiveEvalCaseResult]) -> dict[str, object]:
    pass_count = sum(1 for result in results if result.passed)
    fail_count = len(results) - pass_count
    return {
        "case_count": len(results),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "passed": fail_count == 0,
        "memory_writes": sum(_metric_int(result, "memory_writes") for result in results),
        "memory_hits": sum(_metric_int(result, "memory_hits") for result in results),
        "tool_count": sum(_metric_int(result, "tool_count") for result in results),
        "behavior_delta_activations": sum(_metric_int(result, "behavior_delta_activations") for result in results),
    }


def _metric_int(result: LiveEvalCaseResult, key: str) -> int:
    value = result.metrics.get(key, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a guarded live-provider Kestrel learning/capability E2E eval.")
    parser.add_argument("--provider", default="ollama-cloud", choices=list(PROVIDER_OPTIONS))
    parser.add_argument("--model", default=None)
    parser.add_argument("--backend", default="memory", choices=["memory", "memvid"])
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--keep-output", action="store_true")
    args = parser.parse_args()

    readiness = provider_readiness(args.provider, model=args.model)
    if not readiness.available:
        payload = {
            "schema": "kestrel.live_learning_eval.v1",
            "provider": readiness.to_payload(),
            "results": [],
            "summary": {"case_count": 0, "pass_count": 0, "fail_count": 1, "passed": False},
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    if args.output_root is None and not args.keep_output:
        with tempfile.TemporaryDirectory(prefix="kestrel-live-learning-") as tmp:
            payload = run_live_learning_eval(
                provider=readiness.provider,
                model=readiness.model,
                backend=args.backend,
                output_root=Path(tmp),
                timeout_seconds=args.timeout_seconds,
            )
    else:
        output_root = args.output_root or Path("./tmp-live-kestrel") / f"{readiness.provider}-{args.backend}-{uuid4().hex[:8]}"
        payload = run_live_learning_eval(
            provider=readiness.provider,
            model=readiness.model,
            backend=args.backend,
            output_root=output_root,
            timeout_seconds=args.timeout_seconds,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["summary"]["passed"] else 1


def run_live_learning_eval(
    *,
    provider: str,
    model: str,
    backend: str,
    output_root: Path,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    output_root.mkdir(parents=True, exist_ok=True)
    config = build_live_eval_config(
        provider=provider,
        model=model,
        backend=backend,
        output_root=output_root,
        timeout_seconds=timeout_seconds,
    )
    config.workspace.mkdir(parents=True, exist_ok=True)
    marker = f"kestrel_live_learning_{uuid4().hex[:10]}"
    cases: list[tuple[str, Callable[[], dict[str, object]]]] = [
        ("provider_handshake", lambda: _case_provider_handshake(config, marker)),
        ("durable_memory_reopen", lambda: _case_durable_memory_reopen(config, marker)),
        ("correction_frame", lambda: _case_correction_frame(config, marker)),
        ("procedural_promotion_gate", lambda: _case_procedural_promotion_gate()),
        ("task_capsule_learning_signal", lambda: _case_task_capsule(config, marker)),
        ("approval_gate_blocks_unapproved_high_risk_tool", lambda: _case_approval_gate(config)),
        ("behavior_delta_activation_log", lambda: _case_behavior_delta_activation(config, marker)),
    ]
    results = [_run_case(name, fn) for name, fn in cases]
    return {
        "schema": "kestrel.live_learning_eval.v1",
        "provider": {
            "provider": provider,
            "model": model,
            "backend": backend,
            "output_root": str(output_root),
        },
        "results": [result.to_payload() for result in results],
        "summary": summarize_results(results),
    }


def _run_case(name: str, fn: Callable[[], dict[str, object]]) -> LiveEvalCaseResult:
    started = perf_counter()
    try:
        payload = fn()
        passed = bool(payload.pop("passed"))
        return LiveEvalCaseResult(name=name, passed=passed, latency_ms=(perf_counter() - started) * 1000, metrics=payload)
    except Exception as exc:  # noqa: BLE001 - eval harness reports all diagnostics as JSON
        return LiveEvalCaseResult(
            name=name,
            passed=False,
            latency_ms=(perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )


def _case_provider_handshake(config: AgentConfig, marker: str) -> dict[str, object]:
    provider = build_llm_provider(config)
    response = provider.generate(
        [ChatMessage(role="user", content=f"Reply with exactly this marker and nothing else: {marker}")],
        tools=[],
        options=LLMOptions(timeout_seconds=config.timeout_seconds, max_retries=0, temperature=0.0),
    )
    text = response.content.strip()
    return {
        "passed": bool(text),
        "content_chars": len(text),
        "usage": response.usage,
    }


def _case_durable_memory_reopen(config: AgentConfig, marker: str) -> dict[str, object]:
    agent = build_agent(config)
    try:
        first = agent.chat(f"Remember: {marker} means live durable learning eval marker.", session_id=marker)
    finally:
        agent.close()
    reopened = build_agent(config)
    try:
        hits = reopened.memory.retrieve(RetrievalQuery(query=marker, k_per_layer=5))
        return {
            "passed": bool(hits) and len(first.memory_writes) >= 2,
            "memory_writes": len(first.memory_writes),
            "memory_hits": len(hits),
            "context_chars": first.context_chars,
            "stop_reason": first.stop_reason,
        }
    finally:
        reopened.close()


def _case_correction_frame(config: AgentConfig, marker: str) -> dict[str, object]:
    agent = build_agent(config)
    try:
        result = agent.chat(f"Remember: correction for {marker}: prefer evidence before claims.", session_id=f"{marker}-correction")
        corrections = [record for record in agent.memory.iter_records(include_inactive=True) if record.kind == MemoryKind.CORRECTION]
        return {
            "passed": bool(corrections),
            "memory_writes": len(result.memory_writes),
            "correction_count": len(corrections),
            "stop_reason": result.stop_reason,
        }
    finally:
        agent.close()


def _case_procedural_promotion_gate() -> dict[str, object]:
    kernel = NestedLearningKernel()
    one = kernel.decide(
        LearningSignal(
            title="Live eval one-off procedure",
            content="One success is not enough to promote a procedure.",
            kind=MemoryKind.PROCEDURE,
            source_layer=MemoryLayer.EPISODIC,
            validation_score=0.95,
            repeat_count=1,
            requested_target_layer=MemoryLayer.PROCEDURAL,
        )
    )
    repeated = kernel.decide(
        LearningSignal(
            title="Live eval repeated procedure",
            content="Repeated validated success can promote a procedure.",
            kind=MemoryKind.PROCEDURE,
            source_layer=MemoryLayer.EPISODIC,
            validation_score=0.95,
            repeat_count=2,
            requested_target_layer=MemoryLayer.PROCEDURAL,
        )
    )
    policy = kernel.decide(
        LearningSignal(
            title="Live eval ordinary policy",
            content="Ordinary events must not promote to policy.",
            kind=MemoryKind.POLICY,
            source_layer=MemoryLayer.EPISODIC,
            validation_score=0.99,
            repeat_count=5,
            explicit_instruction=False,
            requested_target_layer=MemoryLayer.POLICY,
        )
    )
    return {
        "passed": not one.accepted and repeated.accepted and not policy.accepted,
        "one_off_action": one.action,
        "repeated_action": repeated.action,
        "policy_action": policy.action,
    }


def _case_task_capsule(config: AgentConfig, marker: str) -> dict[str, object]:
    agent = build_agent(config)
    run_id = f"run_{marker}_capsule"
    try:
        result = agent.chat(f"Remember: capsule candidate fact for {marker}.", session_id=f"{marker}-capsule", run_id=run_id)
        capsule_path = write_run_capsule(
            runs_dir=config.memory_dir.parent / "runs",
            run_id=run_id,
            objective=result.user_message,
            backend=config.backend,
            selected_context=result.context_prompt,
            tool_executions=result.tool_executions,
            final_response=result.assistant_message,
            candidate_facts=(f"{marker} produced a live provider turn stored in an isolated task capsule.",),
            candidate_procedures=("For live learning evals, write explicit capsule candidates so extraction does not depend on model wording.",),
        )
        summary = summarize_run_capsule(runs_dir=config.memory_dir.parent / "runs", run_id=run_id, backend=config.backend)
        return {
            "passed": capsule_path.exists() and summary.telemetry.get("exists") is True and len(summary.learning_signals) >= 2,
            "capsule_path": str(capsule_path),
            "learning_signal_count": len(summary.learning_signals),
            "memory_writes": len(result.memory_writes),
        }
    finally:
        agent.close()


def _case_approval_gate(config: AgentConfig) -> dict[str, object]:
    approval_config = replace(config, allow_memory_import=True)
    registry = build_default_tools()
    call = ToolCall(name="memory.import", id="live-eval-import", arguments={"records": []})
    agent = build_agent(approval_config)
    try:
        execution = registry.execute(
            call,
            ToolContext(memory=agent.memory, config=approval_config, workspace=approval_config.workspace),
        )
        return {
            "passed": not execution.success and execution.error == "approval_required",
            "tool_count": 1,
            "error": execution.error,
        }
    finally:
        agent.close()


def _case_behavior_delta_activation(config: AgentConfig, marker: str) -> dict[str, object]:
    ledger = BehaviorDeltaLedger(AgentStateStore(config.state_path))
    delta = BehaviorDelta(
        id=f"delta_{marker}",
        title="Live eval retrieval reminder",
        kind=BehaviorDeltaKind.PROCEDURE,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.LOW,
        status=BehaviorDeltaStatus.ACTIVE,
        trigger=TriggerSpec(query_patterns=(marker,), semantic_hint="Live eval marker task."),
        behavior_change="When this live eval marker appears, mention that active behavior deltas are compiled only when relevant.",
        evidence_refs=(EvidenceRef(source="live_eval", locator=marker, quote="operator-approved isolated live eval"),),
        validation_plan=ValidationPlan(required_checks=("live_eval_behavior_delta_activation",), min_validation_score=0.1),
        metadata={"explicit_instruction": True, "replay_passed": True},
    )
    ledger.record_delta(delta)
    compiler = BehaviorCompiler(ledger=ledger, config=BehaviorCompilerConfig(enabled=True))
    compiled = compiler.compile(
        BehaviorCompileRequest(
            objective=f"Use the live eval marker {marker} and answer in one sentence.",
            query=marker,
            run_id=f"run_{marker}_delta",
        )
    )
    activations = ledger.list_activations(delta.id)
    return {
        "passed": bool(activations) and delta.id in {item.id for item in compiled.deltas},
        "behavior_delta_activations": len(activations),
        "compiled_chars": len(compiled.text),
    }


if __name__ == "__main__":
    raise SystemExit(main())
