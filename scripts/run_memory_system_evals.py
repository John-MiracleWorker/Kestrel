from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.context_compiler import ContextCompiler, ContextCompilerConfig
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayeredMemorySystem
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.models import (
    EvidenceRef,
    MemoryKind,
    MemoryLayer,
    MemoryRecord,
    RetrievalQuery,
)
from nested_memvid_agent.nested_learning import (
    LearningSignal,
    NestedLearningKernel,
    ValidationEvidence,
    resolve_validation_evidence,
)
from nested_memvid_agent.promotion_ledger import PromotionLedger
from nested_memvid_agent.runtime_models import LLMResponse, ToolCall, ToolExecution, ToolSpec
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Kestrel memory-system evals.")
    parser.add_argument("--backend", choices=["memory"], default="memory")
    parser.add_argument("--provider", choices=["mock"], default="mock")
    parser.add_argument("--memory-dir", type=Path)
    args = parser.parse_args()

    if args.memory_dir is not None:
        if args.memory_dir.exists() and any(args.memory_dir.iterdir()):
            payload = {
                "backend": args.backend,
                "provider": args.provider,
                "diagnostics_schema": "kestrel.memory_system_eval.v1",
                "results": [],
                "summary": {
                    "case_count": 0,
                    "pass_count": 0,
                    "fail_count": 1,
                    "memory_write_count": 0,
                    "memory_hit_count": 0,
                    "policy_write_count": 0,
                },
                "passed": False,
                "stage": "preflight",
                "error": "memory_dir_must_be_empty_to_prevent_stale_evidence_reuse",
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        args.memory_dir.mkdir(parents=True, exist_ok=True)
        payload = _run(args.memory_dir, backend=args.backend, provider=args.provider)
    else:
        with tempfile.TemporaryDirectory(prefix="kestrel-memory-evals-") as tmp:
            payload = _run(Path(tmp), backend=args.backend, provider=args.provider)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["passed"] else 1


def _run(root: Path, *, backend: str, provider: str) -> dict[str, Any]:
    cases: list[tuple[str, Callable[[Path], dict[str, Any]]]] = [
        ("layer_contract", _case_layer_contract),
        ("retrieval_contract", _case_retrieval_contract),
        ("context_summary_first", _case_context_summary_first),
        ("promotion_gates", _case_promotion_gates),
        ("correction_tombstone", _case_correction_tombstone),
        ("promotion_ledger", _case_promotion_ledger),
        ("tool_surface", _case_tool_surface),
        ("agent_cross_layer_flow", _case_agent_cross_layer_flow),
        ("backend_consistency", _case_backend_consistency),
    ]
    results = []
    for name, fn in cases:
        case_root = root / name
        case_root.mkdir(parents=True, exist_ok=True)
        try:
            result = fn(case_root)
            passed = bool(result.pop("passed"))
        except Exception as exc:  # noqa: BLE001 - diagnostics harness reports failures as JSON
            passed = False
            result = {"error": f"{type(exc).__name__}: {exc}"}
        results.append({"name": name, "passed": passed, **result})
    summary = {
        "case_count": len(results),
        "pass_count": sum(1 for result in results if result["passed"]),
        "fail_count": sum(1 for result in results if not result["passed"]),
        "memory_write_count": sum(int(result.get("memory_write_count", 0)) for result in results),
        "memory_hit_count": sum(int(result.get("memory_hit_count", 0)) for result in results),
        "policy_write_count": sum(int(result.get("policy_write_count", 0)) for result in results),
    }
    return {
        "backend": backend,
        "provider": provider,
        "diagnostics_schema": "kestrel.memory_system_eval.v1",
        "results": results,
        "summary": summary,
        "passed": summary["fail_count"] == 0,
    }


def _memory(
    root: Path,
    *,
    ledger: PromotionLedger | None = None,
    enforce_stable_write_integrity: bool = True,
) -> LayeredMemorySystem:
    return LayeredMemorySystem.from_backend_factory(
        root / "memory",
        InMemoryBackend,
        ledger=ledger,
        enforce_stable_write_integrity=enforce_stable_write_integrity,
    )


def _case_layer_contract(root: Path) -> dict[str, Any]:
    memory = _memory(root)
    mv2_files = {layer.value: spec.mv2_file for layer, spec in memory.specs.items()}
    thresholds = {layer.value: spec.min_write_confidence for layer, spec in memory.specs.items()}
    passed = (
        set(memory.backends) == set(MemoryLayer)
        and mv2_files["policy"] == "policy.mv2"
        and memory.specs[MemoryLayer.POLICY].search_mode == "lex"
        and thresholds["working"] < thresholds["semantic"] < thresholds["policy"]
    )
    return {"passed": passed, "mv2_files": mv2_files, "thresholds": thresholds}


def _case_retrieval_contract(root: Path) -> dict[str, Any]:
    # This case isolates ranking and retrieval across every layer. Stable-write
    # admission is exercised separately by the promotion and tool-surface cases.
    memory = _memory(root, enforce_stable_write_integrity=False)
    sentinel = "eval_retrieval_contract_11"
    for layer in MemoryLayer:
        memory.put(
            MemoryRecord(
                title=f"{layer.value} eval record",
                content=f"{sentinel} appears in {layer.value}.",
                layer=layer,
                kind=MemoryKind.POLICY if layer == MemoryLayer.POLICY else MemoryKind.FACT,
                confidence=max(
                    DEFAULT_LAYER_SPECS[layer].min_write_confidence,
                    0.98 if layer == MemoryLayer.POLICY else 0.85,
                ),
            )
        )
    hits = memory.retrieve(RetrievalQuery(query=sentinel, k_per_layer=1))
    layers = sorted(hit.record.layer.value for hit in hits)
    last_retrieved_count = sum(1 for hit in hits if hit.record.metadata.get("last_retrieved_at"))
    return {
        "passed": set(layers) == {layer.value for layer in MemoryLayer}
        and last_retrieved_count == len(MemoryLayer),
        "retrieved_layers": layers,
        "fixture_mode": "integrity_disabled_for_cross_layer_retrieval_only",
        "memory_write_count": len(MemoryLayer),
        "memory_hit_count": len(hits),
    }


def _case_context_summary_first(root: Path) -> dict[str, Any]:
    memory = _memory(root)
    sentinel = "eval_summary_first_22"
    _put(
        memory,
        MemoryLayer.EPISODIC,
        "Eval summary",
        f"{sentinel} summary.",
        frame_type="task_summary",
    )
    _put(
        memory,
        MemoryLayer.EPISODIC,
        "Eval raw",
        f"{sentinel} raw exact evidence.",
        frame_type="raw_chunk",
    )
    _put(
        memory,
        MemoryLayer.EPISODIC,
        "Eval correction",
        f"{sentinel} correction.",
        kind=MemoryKind.CORRECTION,
        frame_type="correction",
    )
    compact = ContextPacker(memory).pack(ContextPackRequest(objective=sentinel, query=sentinel))
    exact = ContextPacker(memory).pack(
        ContextPackRequest(objective=f"Need exact quote for {sentinel}", query=sentinel)
    )
    compact_titles = [item.frame.title for item in compact.items]
    exact_titles = [item.frame.title for item in exact.items]
    return {
        "passed": "Eval summary" in compact_titles
        and "Eval raw" not in compact_titles
        and "Eval raw" in exact_titles,
        "compact_titles": compact_titles,
        "exact_titles": exact_titles,
        "memory_hit_count": len(compact.items) + len(exact.items),
    }


def _case_promotion_gates(root: Path) -> dict[str, Any]:
    del root

    def trusted_evidence(prefix: str, observations: int) -> ValidationEvidence:
        task_refs = tuple(
            EvidenceRef(source="eval.runtime_validation", locator=f"{prefix}-{index}")
            for index in range(observations)
        )
        evidence = ValidationEvidence(
            test_refs=(task_refs[0],),
            lint_refs=(task_refs[0],),
            repair_refs=(task_refs[0],),
            review_refs=(task_refs[0],),
            task_refs=task_refs,
        )
        return resolve_validation_evidence(
            evidence,
            status="runtime_validated",
            artifact_ids=tuple(ref.locator for ref in task_refs),
        )

    kernel = NestedLearningKernel()
    procedural_one = kernel.decide(
        LearningSignal(
            title="Eval one-off procedure",
            content="One success is not enough.",
            kind=MemoryKind.PROCEDURE,
            source_layer=MemoryLayer.EPISODIC,
            validation_score=None,
            validation_evidence=trusted_evidence("procedural-one", 1),
            repeat_count=1,
            requested_target_layer=MemoryLayer.PROCEDURAL,
        )
    )
    procedural_two = kernel.decide(
        LearningSignal(
            title="Eval repeated procedure",
            content="Two validated successes can become procedure.",
            kind=MemoryKind.PROCEDURE,
            source_layer=MemoryLayer.EPISODIC,
            validation_score=None,
            validation_evidence=trusted_evidence("procedural-two", 2),
            repeat_count=2,
            requested_target_layer=MemoryLayer.PROCEDURAL,
        )
    )
    policy_event = kernel.decide(
        LearningSignal(
            title="Eval ordinary policy",
            content="Ordinary event must not become policy.",
            kind=MemoryKind.POLICY,
            source_layer=MemoryLayer.PROCEDURAL,
            validation_score=None,
            validation_evidence=trusted_evidence("policy-event", 5),
            repeat_count=5,
            explicit_instruction=False,
            requested_target_layer=MemoryLayer.POLICY,
        )
    )
    return {
        "passed": not procedural_one.accepted
        and procedural_two.accepted
        and not policy_event.accepted,
        "procedural_one": procedural_one.to_payload(),
        "procedural_two": procedural_two.to_payload(),
        "policy_event": policy_event.to_payload(),
    }


def _case_correction_tombstone(root: Path) -> dict[str, Any]:
    state_path = root / "state.db"
    ledger = PromotionLedger(AgentStateStore(state_path))
    memory = _memory(root, ledger=ledger)
    target_id = _seed_validated_semantic(
        memory,
        promotion_id="eval-correction-promotion",
        title="Eval correction fact",
        content="eval_correction_33 old value.",
    )
    arguments = {
        "target_record_id": target_id,
        "correction_text": "eval_correction_33 corrected value.",
    }
    call = ToolCall(name="memory.correct", id="eval-memory-correct", arguments=arguments)
    result = build_default_tools().execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(state_path=state_path, allow_memory_import=True),
            workspace=root,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: arguments},
        ),
    )
    target = memory.get_record(MemoryLayer.SEMANTIC, target_id, include_inactive=True)
    row = ledger.summarize().rows[0]
    return {
        "passed": bool(
            result.success
            and target
            and target.metadata.get("active") is False
            and row.outcome_counts["corrected"] == 1
        ),
        "correction_record_id": result.data.get("correction_record_id"),
        "ledger_outcomes": row.outcome_counts,
        "memory_write_count": len(list(memory.iter_records(include_inactive=True))),
    }


def _case_promotion_ledger(root: Path) -> dict[str, Any]:
    state_path = root / "state.db"
    ledger = PromotionLedger(AgentStateStore(state_path))
    memory = _memory(root, ledger=ledger)
    record_id = _seed_validated_semantic(
        memory,
        promotion_id="eval-ledger-promotion",
        title="Eval ledger fact",
        content="eval_ledger_44 useful.",
    )
    memory.record_promotion_outcome("eval-ledger-promotion", "useful", evidence_record_id=record_id)
    result = build_default_tools().execute(
        ToolCall(name="memory.ledger", arguments={}),
        ToolContext(memory=memory, config=AgentConfig(state_path=state_path), workspace=root),
    )
    return {
        "passed": result.success and result.data["rows"][0]["outcomes"]["useful"] == 1,
        "rows": result.data.get("rows"),
    }


def _case_tool_surface(root: Path) -> dict[str, Any]:
    memory = _memory(root)
    registry = build_default_tools()
    stable_reject = registry.execute(
        ToolCall(
            name="memory.write",
            arguments={
                "layer": "semantic",
                "kind": "fact",
                "title": "Eval direct stable",
                "content": "eval_tool_surface_55 direct stable write must fail.",
                "confidence": 0.99,
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=root),
    )
    verify = registry.execute(
        ToolCall(name="memvid.verify", arguments={}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=root),
    )
    return {
        "passed": stable_reject.error == "stable_memory_write_rejected" and verify.success,
        "stable_reject_error": stable_reject.error,
        "verify_layers": verify.data.get("layers"),
    }


def _case_agent_cross_layer_flow(root: Path) -> dict[str, Any]:
    memory = _memory(root)
    registry = ToolRegistry()
    registry.register(EvalObservationTool())
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider(
                [
                    LLMResponse(
                        content="Capturing.",
                        tool_calls=(ToolCall(name="eval.observe", arguments={}),),
                    ),
                    LLMResponse(content="eval_cross_layer_66 final response."),
                ]
            ),
            tools=registry,
            config=AgentConfig(memory_dir=root / "memory", log_dir=root / "logs"),
        )
    )
    turn = agent.chat("eval_cross_layer_66 user message", session_id="eval", run_id="eval-run")
    _seed_validated_semantic(
        memory,
        promotion_id="eval-cross-layer-promotion",
        title="Eval cross-layer fact",
        content="eval_cross_layer_66 promoted semantic fact.",
        session_id="eval",
        run_id=turn.run_id,
    )
    compiled = ContextCompiler(
        memory, config=ContextCompilerConfig(context_pack_token_budget=650)
    ).compile(
        "eval_cross_layer_66 later objective",
        query="eval_cross_layer_66 semantic fact",
    )
    return {
        "passed": "Eval cross-layer fact" in compiled.prompt
        and len(list(memory.iter_records(MemoryLayer.POLICY))) == 0,
        "working_records": len(list(memory.iter_records(MemoryLayer.WORKING))),
        "episodic_records": len(list(memory.iter_records(MemoryLayer.EPISODIC))),
        "semantic_records": len(list(memory.iter_records(MemoryLayer.SEMANTIC))),
        "policy_write_count": len(list(memory.iter_records(MemoryLayer.POLICY))),
        "context_chars": compiled.total_chars,
        "memory_write_count": len(list(memory.iter_records())),
        "memory_hit_count": len(compiled.hits),
    }


def _case_backend_consistency(root: Path) -> dict[str, Any]:
    path = root / "backend.mv2"
    backend = InMemoryBackend(path=path, layer=MemoryLayer.SEMANTIC)
    backend.open()
    backend.put(
        MemoryRecord(
            id="eval-backend",
            title="Eval backend",
            content="eval_backend_77 original.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )
    backend.upsert(
        MemoryRecord(
            id="eval-backend",
            title="Eval backend",
            content="eval_backend_77 updated.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.91,
        )
    )
    hits_before = backend.find("eval_backend_77")
    backend.tombstone("eval-backend", reason="eval")
    hits_after = backend.find("eval_backend_77")
    inactive_hits = backend.find("eval_backend_77", include_inactive=True)
    backend.seal()
    return {
        "passed": bool(
            hits_before
            and not hits_after
            and inactive_hits
            and backend.verify()
            and path.with_suffix(".memory.json").exists()
        ),
        "hits_before": len(hits_before),
        "hits_after": len(hits_after),
        "inactive_hits": len(inactive_hits),
    }


def _put(
    memory: LayeredMemorySystem,
    layer: MemoryLayer,
    title: str,
    content: str,
    *,
    kind: MemoryKind = MemoryKind.FACT,
    frame_type: str = "section_summary",
) -> None:
    memory.put(
        MemoryRecord(
            title=title,
            content=content,
            layer=layer,
            kind=kind,
            confidence=0.86,
            metadata={"frame_type": frame_type},
        )
    )


def _seed_validated_semantic(
    memory: LayeredMemorySystem,
    *,
    promotion_id: str,
    title: str,
    content: str,
    session_id: str = "memory-system-eval",
    run_id: str | None = "memory-system-eval-run",
) -> str:
    """Exercise the real stable-memory admission path with bound evidence."""

    candidate_id = memory.put(
        MemoryRecord(
            id=f"candidate-{promotion_id}",
            title=title,
            content=content,
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.FACT,
            confidence=0.95,
            metadata={"session_id": session_id, "run_id": run_id},
            evidence=[EvidenceRef(source="memory_system_eval", locator=run_id or session_id)],
        )
    )
    receipt_id = memory.put_runtime_validation_receipt(
        tool_name="memory_system_eval.validate",
        tool_call_id=f"validate-{promotion_id}",
        evidence_bucket="test",
        command=("memory-system-eval", promotion_id),
        output_sha256=sha256(content.encode("utf-8")).hexdigest(),
        session_id=session_id,
        run_id=run_id,
        subject_record_id=candidate_id,
    )
    receipt_ref = EvidenceRef(source="memory_record", locator=receipt_id)
    evidence = resolve_validation_evidence(
        ValidationEvidence(
            test_refs=(receipt_ref,),
            lint_refs=(receipt_ref,),
            repair_refs=(receipt_ref,),
            review_refs=(receipt_ref,),
            task_refs=(receipt_ref,),
        ),
        status="runtime_validated",
        artifact_ids=(receipt_id,),
    )
    signal = LearningSignal(
        title=title,
        content=content,
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        confidence=0.95,
        validation_score=None,
        validation_evidence=evidence,
        repeat_count=1,
        source="memory_record",
        locator=candidate_id,
        metadata={"session_id": session_id, "run_id": run_id},
        requested_target_layer=MemoryLayer.SEMANTIC,
    )
    kernel = NestedLearningKernel(memory=memory)
    decision = kernel.decide(signal, action="promote")
    if not decision.accepted:
        raise RuntimeError(f"Validated semantic eval promotion was rejected: {decision.reason}")
    record = kernel.to_memory_record(signal, decision)
    record.id = f"record-{promotion_id}"
    record.metadata["promotion_id"] = promotion_id
    return memory.put_validated(
        record,
        authority="nested_learning",
        source_record_ids=(candidate_id, receipt_id),
        validation_evidence=evidence,
    )


class EvalObservationTool(AgentTool):
    spec = ToolSpec(
        name="eval.observe",
        description="Return a deterministic observation for memory-system evals.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        del context
        return ToolExecution(
            call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
            success=True,
            content="eval_cross_layer_66 tool result.",
        )


if __name__ == "__main__":
    raise SystemExit(main())
