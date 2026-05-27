from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from ..consolidation import Consolidator
from ..context_frames import (
    default_frame_type_for_memory,
    estimate_tokens,
    from_memory_record,
    make_correction_frame,
)
from ..context_packer import ContextPacker, ContextPackRequest
from ..models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from ..nested_learning import (
    LearningSignal,
    NestedLearningKernel,
    compute_validation_score,
)
from ..plugin_manager import PluginManager
from ..promotion_ledger import OUTCOME_KINDS, PromotionLedger
from ..retention import RetentionCompactor
from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from ..secret_broker import SecretBroker, build_secret_broker, is_secret_ref
from ..skill_validation import validate_skill_manifest
from ..state_store import AgentStateStore
from ..task_capsule import summarize_run_capsule
from .base import AgentTool, ToolContext
from .command_tools import (
    CodexExecTool,
    LintRunTool,
    PatchApplyTool,
    ShellRunTool,
    TestRunTool,
)
from .diagnosis_tools import DiagnosisClassifyTool, DiagnosisRecallTool
from .discovery_tools import (
    McpRegistryTool,
    PluginRegistryTool,
    ProjectScriptsTool,
    SkillDiscoverTool,
    SkillInspectTool,
    ToolRegistryTool,
)
from .git_tools import (
    GitBranchTool,
    GitCommitTool,
    GitCreateLocalBranchTool,
    GitDiffTool,
    GitExportPatchTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
)
from .registry import ToolRegistry
from .repair_tools import (
    RepairApplyPatchTool,
    RepairOrchestrateValidateTool,
    RepairPrepareTool,
    RepairReviewTool,
    RepairRollbackTool,
    RepairStatusTool,
    RepairValidateTool,
)
from .validation_helpers import (
    _evidence_refs_arg,
    _validation_evidence_arg,
    _validation_evidence_payload_for_output,
)
from .web_tools import WebFetchTool, WebSearchTool
from .workspace_tools import (
    FileStatTool,
    FindFilesTool,
    ListFilesTool,
    ReadFileTool,
    RepoMapTool,
    RepoSearchTool,
    WriteFileTool,
    _safe_path,
)


class MemorySearchTool(AgentTool):
    spec = ToolSpec(
        name="memory.search",
        description="Search nested .mv2 memory layers and return ranked supporting records.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 20},
                "include_inactive": {"type": "boolean"},
            },
            "required": ["query"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        layer_values = arguments.get("layers")
        try:
            layers = (
                tuple(MemoryLayer(value) for value in layer_values)
                if isinstance(layer_values, list)
                else tuple(MemoryLayer)
            )
        except ValueError as exc:
            return self._result(
                call,
                success=False,
                content=f"Unknown memory layer: {exc}",
                error="invalid_tool_arguments",
            )
        k = int(arguments.get("k", 8))
        hits = context.memory.retrieve(
            RetrievalQuery(
                query=query,
                layers=layers,
                k_per_layer=k,
                include_inactive=bool(arguments.get("include_inactive", False)),
            )
        )
        rows = []
        for hit in hits[:k]:
            rows.append(
                {
                    "layer": hit.record.layer.value,
                    "kind": hit.record.kind.value,
                    "title": hit.record.title,
                    "score": hit.score,
                    "snippet": hit.snippet or hit.record.content[:500],
                }
            )
        return self._result(
            call,
            success=True,
            content=json.dumps(rows, indent=2),
            data={"hits": rows},
        )


class MemoryWriteTool(AgentTool):
    spec = ToolSpec(
        name="memory.write",
        description=(
            "Write direct working/episodic memory records. Stable layers require memory.learn, "
            "self.remember, memory.correct, memory.import, or an admin path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "layer": {"type": "string"},
                "kind": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "frame_type": {"type": "string"},
                "parent_ids": {"type": "array", "items": {"type": "string"}},
                "child_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["layer", "title", "content"],
        },
        risk="medium",
    )

    _DIRECT_WRITE_LAYERS = {MemoryLayer.WORKING, MemoryLayer.EPISODIC}
    _STABLE_WRITE_PATHS = (
        "memory.learn",
        "self.remember",
        "memory.correct",
        "memory.import",
        "admin path",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            layer = MemoryLayer(str(arguments.get("layer", "working")))
            kind = MemoryKind(str(arguments.get("kind", "observation")))
        except ValueError as exc:
            return self._result(call, success=False, content=str(exc), error="bad_memory_enum")
        if layer == MemoryLayer.POLICY and not context.config.allow_policy_writes:
            return self._result(
                call,
                success=False,
                content="Policy writes are disabled by default.",
                error="policy_write_disabled",
            )
        if layer not in self._DIRECT_WRITE_LAYERS:
            allowed_layers = sorted(layer.value for layer in self._DIRECT_WRITE_LAYERS)
            safe_paths = list(self._STABLE_WRITE_PATHS)
            return self._result(
                call,
                success=False,
                content=(
                    f"Direct memory.write is only allowed for {', '.join(allowed_layers)} layers. "
                    f"Use {'/'.join(safe_paths)} for {layer.value} memory so validation, provenance, "
                    "confidence, and approval gates are preserved."
                ),
                data={
                    "requested_layer": layer.value,
                    "allowed_direct_layers": allowed_layers,
                    "safe_write_paths": safe_paths,
                },
                error="stable_memory_write_rejected",
            )
        try:
            frame_type = str(
                arguments.get("frame_type") or default_frame_type_for_memory(kind, layer)
            )
            parent_ids_arg = arguments.get("parent_ids")
            child_ids_arg = arguments.get("child_ids")
            parent_ids = (
                [str(item) for item in parent_ids_arg] if isinstance(parent_ids_arg, list) else []
            )
            child_ids = (
                [str(item) for item in child_ids_arg] if isinstance(child_ids_arg, list) else []
            )
            record = MemoryRecord(
                layer=layer,
                kind=kind,
                title=str(arguments.get("title", "Memory")),
                content=str(arguments.get("content", "")),
                confidence=float(arguments.get("confidence", 0.7)),
                importance=float(arguments.get("importance", 0.5)),
                metadata={
                    "session_id": context.session_id,
                    "source": "tool.memory.write",
                    "frame_type": frame_type,
                    "parent_ids": parent_ids,
                    "child_ids": child_ids,
                },
            )
            record_id = context.memory.put(record)
            return self._result(
                call,
                success=True,
                content=f"Wrote memory {record_id}",
                data={"record_id": record_id},
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary must report errors to agent
            return self._result(call, success=False, content=str(exc), error="memory_write_failed")


class ContextPackTool(AgentTool):
    spec = ToolSpec(
        name="context.pack",
        description="Compile an on-demand pseudo-context window for the current objective.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "token_budget": {"type": "integer", "minimum": 256, "maximum": 50000},
                "layers": {"type": "array", "items": {"type": "string"}},
                "expand_raw": {"type": "boolean"},
                "include_telemetry": {"type": "boolean"},
            },
            "required": ["query"],
        },
        capabilities=("pseudo-context", "mv2-context"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        try:
            layers = _layers_arg(arguments.get("layers")) or tuple(MemoryLayer)
            packed = ContextPacker(context.memory).pack(
                ContextPackRequest(
                    objective=query,
                    query=query,
                    token_budget=int(
                        arguments.get("token_budget", context.config.context_pack_token_budget)
                    ),
                    allowed_layers=layers,
                    expand_raw=bool(
                        arguments.get("expand_raw", context.config.context_pack_expand_raw)
                    ),
                    include_telemetry=bool(arguments.get("include_telemetry", True)),
                )
            )
            payload = {
                "packed_prompt": packed.prompt,
                "token_estimate": packed.token_estimate,
                "selected_item_count": len(packed.items),
                "selected_layers": sorted({item.frame.layer.value for item in packed.items}),
                "conflict_warnings": list(packed.conflict_warnings),
                "evidence_refs": list(packed.evidence_refs),
                "telemetry": packed.telemetry,
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="context_pack_failed")


class ContextExpandTool(AgentTool):
    spec = ToolSpec(
        name="context.expand",
        description="Expand a specific memory/frame into raw supporting context.",
        parameters={
            "type": "object",
            "properties": {
                "frame_id": {"type": "string"},
                "record_id": {"type": "string"},
                "max_tokens": {"type": "integer", "minimum": 64, "maximum": 50000},
                "include_children": {"type": "boolean"},
                "include_parents": {"type": "boolean"},
            },
        },
        capabilities=("pseudo-context", "mv2-context"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        lookup_id = str(arguments.get("frame_id") or arguments.get("record_id") or "").strip()
        if not lookup_id:
            return self._result(
                call, success=False, content="Missing frame_id or record_id", error="missing_id"
            )
        try:
            hit = _find_memory_by_id(context, lookup_id)
            if hit is None:
                return self._result(
                    call,
                    success=False,
                    content=f"No memory found for {lookup_id}",
                    error="not_found",
                )
            frame = from_memory_record(hit.record)
            related = _related_frames(
                context,
                frame,
                include_children=bool(arguments.get("include_children", False)),
                include_parents=bool(arguments.get("include_parents", False)),
            )
            max_tokens = int(arguments.get("max_tokens", 2000))
            raw_content = _truncate_by_tokens(hit.record.content, max_tokens)
            payload = {
                "frame_id": frame.id,
                "record_id": hit.record.id,
                "raw_content": raw_content,
                "token_estimate": estimate_tokens(raw_content),
                "parent_ids": list(frame.parent_ids),
                "child_ids": list(frame.child_ids),
                "related": related,
                "evidence_metadata": {
                    "source_uri": frame.source_uri,
                    "source_span": frame.source_span,
                    "content_hash": frame.content_hash,
                    "layer": frame.layer.value,
                    "kind": frame.kind.value,
                },
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="context_expand_failed"
            )


class CapsuleSummarizeTool(AgentTool):
    spec = ToolSpec(
        name="capsule.summarize",
        description="Summarize a completed run capsule and show candidate learning signals.",
        parameters={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["run_id"],
        },
        risk="medium",
        capabilities=("task-capsule", "nested-learning"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        run_id = str(arguments.get("run_id", "")).strip()
        if not run_id:
            return self._result(
                call, success=False, content="Missing run_id", error="missing_run_id"
            )
        try:
            summary = summarize_run_capsule(
                runs_dir=context.config.memory_dir.parent / "runs",
                run_id=run_id,
                backend=context.config.backend,
            )
            kernel = NestedLearningKernel()
            decisions = []
            for signal in summary.learning_signals:
                decision = kernel.decide(signal)
                payload = decision.to_payload()
                payload["signal_title"] = signal.title
                payload["dry_run"] = True
                decisions.append(payload)
            payload = {
                **summary.to_payload(),
                "dry_run": True,
                "nested_learning_decisions": decisions,
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="capsule_summarize_failed"
            )


class CapsuleApplyTool(AgentTool):
    needs_call_id = True
    spec = ToolSpec(
        name="capsule.apply",
        description="Apply accepted learning signals from a completed run capsule. Requires auto-consolidation config and approval before writing.",
        parameters={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "dry_run": {"type": "boolean"},
                "include_policy": {"type": "boolean"},
            },
            "required": ["run_id"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("task-capsule", "nested-learning", "memory-write"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        public_arguments = {
            key: value for key, value in arguments.items() if not str(key).startswith("_")
        }
        call_id = str(arguments.get("_tool_call_id") or "")
        call = (
            ToolCall(name=self.spec.name, arguments=public_arguments, id=call_id)
            if call_id
            else ToolCall(name=self.spec.name, arguments=public_arguments)
        )
        run_id = str(arguments.get("run_id", "")).strip()
        if not run_id:
            return self._result(
                call, success=False, content="Missing run_id", error="missing_run_id"
            )
        dry_run = bool(arguments.get("dry_run", False))
        include_policy = bool(arguments.get("include_policy", False))
        try:
            summary = summarize_run_capsule(
                runs_dir=context.config.memory_dir.parent / "runs",
                run_id=run_id,
                backend=context.config.backend,
            )
            plan = _capsule_apply_plan(summary, context=context, include_policy=include_policy)
            if dry_run:
                payload = {
                    **summary.to_payload(),
                    "dry_run": True,
                    "applied": False,
                    "decisions": plan,
                }
                return self._result(
                    call, success=True, content=json.dumps(payload, indent=2), data=payload
                )

            if not context.config.enable_auto_consolidation:
                payload = {
                    **summary.to_payload(),
                    "dry_run": False,
                    "applied": False,
                    "decisions": plan,
                    "auto_consolidation_enabled": False,
                }
                return self._result(
                    call,
                    success=False,
                    content=json.dumps(payload, indent=2),
                    data=payload,
                    error="auto_consolidation_disabled",
                )

            wrote = False
            for item in plan:
                if item.get("will_write") is not True:
                    continue
                signal_index = item.get("signal_index")
                if not isinstance(signal_index, int):
                    continue
                signal = summary.learning_signals[signal_index]
                decision = NestedLearningKernel().decide(signal)
                if decision.target_layer is None:
                    continue
                record = NestedLearningKernel().to_memory_record(signal, decision)
                if _memory_has_content_hash(context, record.layer, record.content_hash):
                    item["skipped"] = "duplicate_content_hash"
                    item["will_write"] = False
                    continue
                item["record_id"] = context.memory.put(record)
                wrote = True
            if wrote:
                context.memory.seal_all()
            payload = {
                **summary.to_payload(),
                "dry_run": False,
                "applied": wrote,
                "decisions": plan,
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="capsule_apply_failed")


class MemoryConflictsTool(AgentTool):
    spec = ToolSpec(
        name="memory.conflicts",
        description="Search for conflicting memories around a claim/query.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
        capabilities=("pseudo-context", "conflict-detection"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        try:
            layers = _layers_arg(arguments.get("layers")) or tuple(MemoryLayer)
            k = int(arguments.get("k", 8))
            packed = ContextPacker(context.memory).pack(
                ContextPackRequest(
                    objective=f"Find conflicting memories for: {query}",
                    query=query,
                    allowed_layers=layers,
                    token_budget=context.config.context_pack_token_budget,
                    k_per_layer=k,
                    include_telemetry=True,
                )
            )
            hits = context.memory.retrieve(
                RetrievalQuery(query=query, layers=layers, k_per_layer=k)
            )
            possible_conflicts = []
            for hit in hits[:k]:
                metadata = hit.record.metadata
                possible_conflicts.append(
                    {
                        "record_id": hit.record.id,
                        "frame_id": metadata.get("frame_id") or hit.frame_id,
                        "layer": hit.record.layer.value,
                        "kind": hit.record.kind.value,
                        "title": hit.record.title,
                        "confidence": hit.record.confidence,
                        "importance": hit.record.importance,
                        "score": hit.score,
                        "conflict_group_id": metadata.get("conflict_group_id"),
                        "snippet": hit.snippet or hit.record.content[:500],
                    }
                )
            payload = {
                "query": query,
                "conflict_warnings": list(packed.conflict_warnings),
                "possible_conflicts": possible_conflicts,
                "recommended_action": "Expand raw evidence and validate the conflicting claim before writing or promoting memory."
                if packed.conflict_warnings
                else "No conflict metadata detected; still validate high-impact claims against evidence.",
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="memory_conflicts_failed"
            )


class MemvidVerifyTool(AgentTool):
    spec = ToolSpec(
        name="memvid.verify",
        description="Verify every nested memory layer.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del arguments
        call = ToolCall(name=self.spec.name, arguments={})
        results = context.memory.verify_all()
        rows = {layer.value: ok for layer, ok in results.items()}
        return self._result(
            call,
            success=all(rows.values()),
            content=json.dumps(rows, indent=2),
            data={"layers": rows},
        )


class MemvidDoctorTool(AgentTool):
    spec = ToolSpec(
        name="memvid.doctor",
        description="Run dry-run doctor checks on memory layers when the backend supports it.",
        parameters={
            "type": "object",
            "properties": {"dry_run": {"type": "boolean"}},
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        dry_run = bool(arguments.get("dry_run", True))
        rows: dict[str, object] = {}
        for layer, backend in context.memory.backends.items():
            doctor = getattr(backend, "doctor", None)
            if callable(doctor):
                rows[layer.value] = doctor(dry_run=dry_run)
            else:
                rows[layer.value] = {"ok": backend.verify(), "doctor_available": False}
        return self._result(
            call,
            success=True,
            content=json.dumps(rows, indent=2, default=str),
            data={"layers": rows},
        )


class MemvidStatsTool(AgentTool):
    spec = ToolSpec(
        name="memvid.stats",
        description="Return backend statistics for memory layers when available.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del arguments
        call = ToolCall(name=self.spec.name, arguments={})
        rows: dict[str, object] = {}
        for layer, backend in context.memory.backends.items():
            stats = getattr(backend, "stats", None)
            if callable(stats):
                rows[layer.value] = stats()
            else:
                rows[layer.value] = {"ok": backend.verify(), "stats_available": False}
        return self._result(
            call,
            success=True,
            content=json.dumps(rows, indent=2, default=str),
            data={"layers": rows},
        )


class MemoryLedgerTool(AgentTool):
    spec = ToolSpec(
        name="memory.ledger",
        description="Summarize promotion ledger outcomes without changing memory gates.",
        parameters={
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "target_layer": {"type": "string"},
                "outcome": {"type": "string"},
            },
        },
        capabilities=("nested-learning", "memory-diagnostics"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            since = _datetime_arg(arguments.get("since"))
            target_layer = _layer_arg(arguments.get("target_layer"))
            outcome = _outcome_arg(arguments.get("outcome"))
            ledger = context.memory.ledger or PromotionLedger(AgentStateStore(context.config.state_path))
            payload = ledger.summarize(since=since, target_layer=target_layer, outcome=outcome).to_payload()
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001 - tool boundary returns structured diagnostics
            return self._result(call, success=False, content=str(exc), error="memory_ledger_failed")


class MemoryConsolidateTool(AgentTool):
    spec = ToolSpec(
        name="memory.consolidate",
        description="Promote a validated memory candidate through the nested consolidation pipeline.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source_layer": {"type": "string"},
                "validation_evidence": {"type": "object"},
                "validation_score": {"type": "number", "minimum": 0, "maximum": 1},
                "repeat_count": {"type": "integer", "minimum": 1},
                "explicit_instruction": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["query"],
        },
        risk="medium",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        try:
            source_layer = _layer_arg(arguments.get("source_layer"))
            layers = (source_layer,) if source_layer else tuple(MemoryLayer)
            hits = context.memory.retrieve(
                RetrievalQuery(query=query, layers=layers, k_per_layer=1)
            )
            if not hits:
                return self._result(
                    call,
                    success=False,
                    content="No consolidation candidate found.",
                    error="candidate_not_found",
                )
            validation_evidence = _validation_evidence_arg(arguments)
            validation_score = (
                compute_validation_score(validation_evidence)
                if validation_evidence is not None
                else float(arguments.get("validation_score", 0.7))
            )
            repeat_count = int(arguments.get("repeat_count", 1))
            explicit_instruction = bool(arguments.get("explicit_instruction", False))
            candidate = Consolidator().propose(
                hits[0].record,
                validation_score=validation_score,
                repeat_count=repeat_count,
                explicit_instruction=explicit_instruction,
            )
            if candidate is None:
                return self._result(
                    call, success=True, content="No promotion proposed.", data={"promoted": False}
                )
            if (
                candidate.target_layer == MemoryLayer.POLICY
                and not context.config.allow_policy_writes
            ):
                return self._result(
                    call,
                    success=False,
                    content="Policy promotion is disabled by default.",
                    error="policy_write_disabled",
                )
            promoted = Consolidator().promote(candidate)
            dry_run = bool(arguments.get("dry_run", False))
            record_id = None if dry_run else context.memory.put(promoted)
            if record_id is not None:
                context.memory.seal_all()
            payload = {
                "promoted": True,
                "dry_run": dry_run,
                "record_id": record_id,
                "source_record_id": candidate.source.id,
                "source_layer": candidate.source.layer.value,
                "target_layer": candidate.target_layer.value,
                "reason": candidate.reason,
                "confidence": candidate.promoted_confidence,
                "context_flow": candidate.flow.to_metadata(),
                "optimizer_trace": candidate.optimizer_trace.to_metadata(),
                "validation_score": validation_score,
                "validation_evidence": _validation_evidence_payload_for_output(
                    validation_evidence, validation_score
                ),
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="memory_consolidate_failed"
            )


class MemoryLearnTool(AgentTool):
    spec = ToolSpec(
        name="memory.learn",
        description="Compress a validated learning signal into the correct nested memory layer using context-flow gates.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "kind": {"type": "string"},
                "source_layer": {"type": "string"},
                "target_layer": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "validation_evidence": {"type": "object"},
                "validation_score": {"type": "number", "minimum": 0, "maximum": 1},
                "repeat_count": {"type": "integer", "minimum": 1},
                "explicit_instruction": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
                "source": {"type": "string"},
                "locator": {"type": "string"},
            },
            "required": ["title", "content"],
        },
        risk="medium",
        capabilities=("nested-learning", "continuum-memory"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        title = str(arguments.get("title", "")).strip()
        content = str(arguments.get("content", "")).strip()
        if not title:
            return self._result(call, success=False, content="Missing title", error="missing_title")
        if not content:
            return self._result(
                call, success=False, content="Missing content", error="missing_content"
            )
        try:
            source_layer = _layer_arg(arguments.get("source_layer")) or MemoryLayer.WORKING
            target_layer = _layer_arg(arguments.get("target_layer"))
            kind = MemoryKind(str(arguments.get("kind", MemoryKind.OBSERVATION.value)))
            validation_evidence = _validation_evidence_arg(arguments)
            signal = LearningSignal(
                title=title,
                content=content,
                kind=kind,
                source_layer=source_layer,
                confidence=float(arguments.get("confidence", 0.6)),
                importance=float(arguments.get("importance", 0.5)),
                validation_score=None
                if validation_evidence is not None
                else float(arguments.get("validation_score", 0.7)),
                validation_evidence=validation_evidence,
                repeat_count=int(arguments.get("repeat_count", 1)),
                explicit_instruction=bool(arguments.get("explicit_instruction", False)),
                source=str(arguments.get("source", "tool.memory.learn")),
                locator=str(arguments.get("locator", context.session_id)),
                metadata={"session_id": context.session_id, "run_id": context.run_id},
                requested_target_layer=target_layer,
            )
            kernel = NestedLearningKernel()
            decision = kernel.decide(signal)
            if not decision.accepted:
                return self._result(
                    call,
                    success=True,
                    content=json.dumps(decision.to_payload(), indent=2),
                    data=decision.to_payload(),
                )
            if (
                decision.target_layer == MemoryLayer.POLICY
                and not context.config.allow_policy_writes
            ):
                payload = decision.to_payload()
                payload["policy_write_enabled"] = False
                return self._result(
                    call,
                    success=False,
                    content=json.dumps(payload, indent=2),
                    data=payload,
                    error="policy_write_disabled",
                )
            record = kernel.to_memory_record(signal, decision)
            dry_run = bool(arguments.get("dry_run", False))
            record_id = None if dry_run else context.memory.put(record)
            if record_id is not None:
                context.memory.seal_all()
            payload = {
                **decision.to_payload(),
                "dry_run": dry_run,
                "record_id": record_id,
                "validation_score": signal.computed_validation_score,
                "validation_evidence": _validation_evidence_payload_for_output(
                    validation_evidence,
                    signal.computed_validation_score,
                ),
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="memory_learn_failed")


class MemoryInspectTool(AgentTool):
    spec = ToolSpec(
        name="memory.inspect",
        description="Inspect matching memory records with provenance and metadata.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 50},
                "include_inactive": {"type": "boolean"},
            },
            "required": ["query"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        layers = _layers_arg(arguments.get("layers")) or tuple(MemoryLayer)
        hits = context.memory.retrieve(
            RetrievalQuery(
                query=query,
                layers=layers,
                k_per_layer=int(arguments.get("k", 8)),
                include_inactive=bool(arguments.get("include_inactive", False)),
            )
        )
        rows = [_memory_hit_payload(hit) for hit in hits]
        return self._result(
            call, success=True, content=json.dumps(rows, indent=2), data={"hits": rows}
        )


class MemoryCorrectTool(AgentTool):
    spec = ToolSpec(
        name="memory.correct",
        description="Write a correction frame for a target memory record and supersede the target by default.",
        parameters={
            "type": "object",
            "properties": {
                "target_record_id": {"type": "string"},
                "correction_text": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "object"}},
                "dry_run": {"type": "boolean"},
            },
            "required": ["target_record_id", "correction_text"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        target_id = str(arguments.get("target_record_id", "")).strip()
        correction_text = str(arguments.get("correction_text", "")).strip()
        if not target_id:
            return self._result(
                call,
                success=False,
                content="Missing target_record_id",
                error="missing_target_record_id",
            )
        if not correction_text:
            return self._result(
                call,
                success=False,
                content="Missing correction_text",
                error="missing_correction_text",
            )
        target = context.memory.get_record(None, target_id, include_inactive=True)
        if target is None:
            return self._result(
                call,
                success=False,
                content="Target memory record not found.",
                error="target_not_found",
            )
        evidence = _evidence_refs_arg(arguments.get("evidence"))
        dry_run = bool(arguments.get("dry_run", False))
        frame = make_correction_frame(
            target_record_id=target.id,
            layer=target.layer,
            correction_text=correction_text,
            evidence=evidence,
            title=f"Correction: {target.title}",
        )
        record_id = None
        if not dry_run:
            record_id = context.memory.put_frame(frame)
            context.memory.tombstone(
                target.layer, target.id, reason="corrected", superseded_by=str(record_id)
            )
            context.memory.seal_all()
        payload = {
            "corrected": True,
            "dry_run": dry_run,
            "target_record_id": target.id,
            "target_layer": target.layer.value,
            "correction_record_id": record_id,
            "correction_frame_id": frame.id,
            "evidence": [ref.__dict__ for ref in evidence],
        }
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class MemoryCompactTool(AgentTool):
    spec = ToolSpec(
        name="memory.compact",
        description="Compact TTL-eligible working/episodic memory. Dry-run by default.",
        parameters={
            "type": "object",
            "properties": {
                "layer": {"type": "string"},
                "apply": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
            },
        },
        risk="medium",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        layer = _layer_arg(arguments.get("layer")) or MemoryLayer.WORKING
        dry_run = bool(arguments.get("dry_run", not bool(arguments.get("apply", False))))
        report = RetentionCompactor(context.memory).compact_layer(layer, dry_run=dry_run)
        if not dry_run:
            context.memory.seal_all()
        return self._result(
            call, success=True, content=json.dumps(report, indent=2, default=str), data=report
        )


class MemoryExportTool(AgentTool):
    spec = ToolSpec(
        name="memory.export",
        description="Export memory records as structured JSON. Use query for backends without full record iteration.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "layers": {"type": "array", "items": {"type": "string"}},
                "k": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        layers = _layers_arg(arguments.get("layers")) or tuple(MemoryLayer)
        rows: list[dict[str, object]] = []
        query = str(arguments.get("query", "")).strip()
        if query:
            hits = context.memory.retrieve(
                RetrievalQuery(query=query, layers=layers, k_per_layer=int(arguments.get("k", 20)))
            )
            rows = [_memory_record_payload(hit.record) for hit in hits]
        else:
            for layer in layers:
                backend = context.memory.backends.get(layer)
                records = getattr(backend, "records", None)
                if isinstance(records, list):
                    rows.extend(_memory_record_payload(record) for record in records)
        return self._result(
            call, success=True, content=json.dumps(rows, indent=2), data={"records": rows}
        )


class MemoryImportTool(AgentTool):
    spec = ToolSpec(
        name="memory.import",
        description="Import explicit memory records. Requires approval and keeps policy writes gated separately.",
        parameters={
            "type": "object",
            "properties": {
                "records": {"type": "array", "items": {"type": "object"}},
                "dry_run": {"type": "boolean"},
            },
            "required": ["records"],
        },
        risk="critical",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        raw_records = arguments.get("records")
        if not isinstance(raw_records, list):
            return self._result(
                call, success=False, content="records must be a list", error="bad_records"
            )
        if not all(isinstance(item, dict) for item in raw_records):
            return self._result(
                call, success=False, content="Every record must be an object", error="bad_records"
            )
        try:
            records = [_memory_record_from_payload(item) for item in raw_records]
        except Exception as exc:  # noqa: BLE001 - import payload validation boundary
            return self._result(call, success=False, content=str(exc), error="bad_records")
        if (
            any(record.layer == MemoryLayer.POLICY for record in records)
            and not context.config.allow_policy_writes
        ):
            return self._result(
                call,
                success=False,
                content="Policy memory import is disabled by default.",
                error="policy_write_disabled",
            )
        dry_run = bool(arguments.get("dry_run", False))
        ids: list[str] = []
        if not dry_run:
            try:
                for record in records:
                    ids.append(context.memory.put(record))
                context.memory.seal_all()
            except Exception as exc:  # noqa: BLE001 - import should report failed writes structurally
                return self._result(
                    call, success=False, content=str(exc), error="memory_import_failed"
                )
        payload = {"dry_run": dry_run, "imported": len(records), "record_ids": ids}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class SkillInstallTool(AgentTool):
    spec = ToolSpec(
        name="skill.install",
        description="Install or update a local skill capsule under the configured skills directory. Requires file-write enablement and approval.",
        parameters={
            "type": "object",
            "properties": {
                "manifest": {"type": "object"},
                "instructions": {"type": "string"},
                "overwrite": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["manifest", "instructions"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("skill-install", "tool-upload", "provenance"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        manifest_raw = arguments.get("manifest")
        instructions = str(arguments.get("instructions", ""))
        if not isinstance(manifest_raw, dict):
            return self._result(
                call, success=False, content="manifest must be an object", error="bad_manifest"
            )
        if not instructions.strip():
            return self._result(
                call,
                success=False,
                content="instructions cannot be empty",
                error="missing_instructions",
            )
        manifest = dict(manifest_raw)
        skill_id = str(manifest.get("id", "")).strip()
        if not skill_id:
            return self._result(
                call, success=False, content="manifest.id is required", error="missing_skill_id"
            )
        if not _safe_skill_id(skill_id):
            return self._result(
                call, success=False, content=f"Unsafe skill id: {skill_id}", error="unsafe_skill_id"
            )
        validation = validate_skill_manifest(manifest)
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True)
        payload: dict[str, Any] = {
            "skill_id": skill_id,
            "validation": validation,
            "manifest_sha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
            "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            "dry_run": bool(arguments.get("dry_run", False)),
        }
        if validation["errors"]:
            return self._result(
                call,
                success=False,
                content=json.dumps(payload, indent=2),
                data=payload,
                error="invalid_skill_manifest",
            )

        try:
            skill_dir = _safe_path(context.config.skills_dir, skill_id)
            payload["path"] = str(skill_dir)
            if payload["dry_run"]:
                return self._result(
                    call, success=True, content=json.dumps(payload, indent=2), data=payload
                )
            if skill_dir.exists() and not bool(arguments.get("overwrite", False)):
                return self._result(
                    call,
                    success=False,
                    content=json.dumps(payload, indent=2),
                    data=payload,
                    error="skill_exists",
                )
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "skill.json").write_text(manifest_text + "\n", encoding="utf-8")
            (skill_dir / "SKILL.md").write_text(instructions, encoding="utf-8")
            payload["installed"] = True
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="skill_install_failed")


class PluginInstallTool(AgentTool):
    spec = ToolSpec(
        name="plugin.install",
        description="Install a public GitHub plugin repo into the local plugin registry. Requires plugin-install enablement and exact approval.",
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "ref": {"type": "string"},
                "enable": {"type": "boolean"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["source"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("plugin-install", "github", "provenance"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        source = str(arguments.get("source", "")).strip()
        if not source:
            return self._result(
                call, success=False, content="source is required", error="missing_source"
            )
        state = AgentStateStore(context.config.state_path)
        manager = PluginManager(context.config.plugins_dir, state)
        try:
            plugin = manager.install(
                source,
                ref=str(arguments["ref"]).strip() if arguments.get("ref") else None,
                enable=bool(arguments.get("enable", False)),
                overwrite=bool(arguments.get("overwrite", False)),
            )
            record_id = manager.write_audit_memory(context.memory, action="install", plugin=plugin)
            payload = {**plugin, "memory_record_id": record_id}
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001 - plugin install boundary reports structured failure
            return self._result(
                call, success=False, content=str(exc), error="plugin_install_failed"
            )


class PluginReviewTool(AgentTool):
    spec = ToolSpec(
        name="plugin.review",
        description="Review a public GitHub plugin repo without installing it. Requires plugin-install enablement and exact approval.",
        parameters={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "ref": {"type": "string"},
            },
            "required": ["source"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("plugin-review", "github", "provenance", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        source = str(arguments.get("source", "")).strip()
        if not source:
            return self._result(
                call, success=False, content="source is required", error="missing_source"
            )
        state = AgentStateStore(context.config.state_path)
        manager = PluginManager(context.config.plugins_dir, state)
        try:
            review = manager.review(
                source,
                ref=str(arguments["ref"]).strip() if arguments.get("ref") else None,
            )
            return self._result(
                call, success=True, content=json.dumps(review, indent=2), data=review
            )
        except Exception as exc:  # noqa: BLE001 - plugin review boundary reports structured failure
            return self._result(
                call, success=False, content=str(exc), error="plugin_review_failed"
            )


class SelfInspectTool(AgentTool):
    spec = ToolSpec(
        name="self.inspect",
        description="Inspect Kestrel's non-secret self model, capabilities, tools, memory layers, skills, plugins, and MCP state.",
        parameters={
            "type": "object",
            "properties": {
                "include_tools": {"type": "boolean"},
                "include_state": {"type": "boolean"},
            },
        },
        capabilities=("self-model", "introspection", "soul"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        payload = _self_snapshot(
            context,
            include_tools=bool(arguments.get("include_tools", False)),
            include_state=bool(arguments.get("include_state", True)),
        )
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class SelfReflectTool(AgentTool):
    spec = ToolSpec(
        name="self.reflect",
        description="Summarize what Kestrel knows about itself and the user from the Soul/self memory layer.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 12},
            },
        },
        risk="medium",
        capabilities=("self-model", "reflection", "soul"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(
            arguments.get("query") or "Kestrel identity capabilities user workflow preferences"
        ).strip()
        k = max(1, min(int(arguments.get("k", 6)), 12))
        hits = context.memory.retrieve(
            RetrievalQuery(query=query, layers=(MemoryLayer.SELF,), k_per_layer=k)
        )
        rows = [_memory_hit_payload(hit) for hit in hits[:k]]
        payload = {
            "identity": _self_identity(),
            "query": query,
            "self_memory_hits": rows,
            "reflection": _self_reflection_text(rows),
        }
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class SelfRememberTool(AgentTool):
    spec = ToolSpec(
        name="self.remember",
        description="Write a validated self-model record to the Soul/self .mv2 layer with provenance and nested-learning metadata.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "schema": {"type": "string"},
                "validation_status": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "source": {"type": "string"},
                "locator": {"type": "string"},
            },
            "required": ["title", "content", "schema", "validation_status"],
        },
        risk="medium",
        capabilities=("self-model", "memory-write", "soul"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        title = str(arguments.get("title", "")).strip()
        content = str(arguments.get("content", "")).strip()
        schema = str(arguments.get("schema", "")).strip()
        validation_status = str(arguments.get("validation_status", "")).strip()
        if not title:
            return self._result(call, success=False, content="Missing title", error="missing_title")
        if not content:
            return self._result(
                call, success=False, content="Missing content", error="missing_content"
            )
        if schema not in _SELF_SCHEMAS:
            return self._result(
                call,
                success=False,
                content=f"Unknown self schema: {schema}",
                error="invalid_self_schema",
            )
        if not validation_status:
            return self._result(
                call,
                success=False,
                content="Missing validation_status",
                error="missing_validation_status",
            )
        confidence = float(arguments.get("confidence", 0.82))
        importance = float(arguments.get("importance", 0.72))
        source = str(arguments.get("source") or "self.remember")
        locator = str(arguments.get("locator") or context.run_id or context.session_id)
        signal = LearningSignal(
            title=title,
            content=content,
            kind=MemoryKind.FACT,
            source_layer=MemoryLayer.EPISODIC,
            confidence=confidence,
            importance=importance,
            validation_score=confidence,
            repeat_count=1,
            explicit_instruction=validation_status
            in {"user_confirmed", "operator_confirmed", "explicit_request"},
            source=source,
            locator=locator,
            tags={"self_schema": schema},
            metadata={
                "self_schema": schema,
                "validation_status": validation_status,
                "provenance": source,
                "session_id": context.session_id,
                "run_id": context.run_id,
            },
            requested_target_layer=MemoryLayer.SELF,
        )
        kernel = NestedLearningKernel()
        decision = kernel.decide(signal, action="write")
        if not decision.accepted:
            return self._result(
                call,
                success=False,
                content=decision.reason,
                data=decision.to_payload(),
                error="self_memory_rejected",
            )
        record = kernel.to_memory_record(signal, decision)
        record.metadata.update(
            {
                "self_schema": schema,
                "validation_status": validation_status,
                "provenance": source,
                "frame_type": "self_model",
            }
        )
        try:
            record_id = context.memory.put(record)
            context.memory.seal_all()
        except Exception as exc:  # noqa: BLE001 - tool boundary
            return self._result(
                call,
                success=False,
                content=str(exc),
                data=decision.to_payload(),
                error="self_remember_failed",
            )
        payload = {"record_id": record_id, "decision": decision.to_payload(), "self_schema": schema}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class SelfProposeChangeTool(AgentTool):
    spec = ToolSpec(
        name="self.propose_change",
        description="Record an approval-gated self-change request for Kestrel without applying code changes directly.",
        parameters={
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["request"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("self-modification", "safe-repair", "soul"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        request = str(arguments.get("request", "")).strip()
        if not request:
            return self._result(
                call, success=False, content="Missing request", error="missing_request"
            )
        payload = {
            "request": request,
            "rationale": str(arguments.get("rationale", "")).strip(),
            "target_workspace": str(context.workspace),
            "required_gates": [
                "repair.prepare",
                "repair.apply_patch",
                "repair.validate",
                "repair.review",
                "git.commit",
            ],
            "approval_required_before_execution": True,
            "push_or_merge_allowed": False,
        }
        remember = SelfRememberTool().run(
            {
                "title": "Self-change request",
                "content": json.dumps(payload, indent=2),
                "schema": "self_change_request",
                "validation_status": "operator_requested",
                "confidence": 0.86,
                "source": "self.propose_change",
                "locator": call.id,
            },
            context,
        )
        data = {
            **payload,
            "memory_record_id": remember.data.get("record_id"),
            "memory_error": remember.error,
        }
        return self._result(
            call,
            success=remember.success,
            content=json.dumps(data, indent=2),
            data=data,
            error=remember.error,
        )


def build_default_tools(enabled_names: tuple[str, ...] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    enabled = {name.strip() for name in enabled_names or () if name.strip()}

    def register(tool: AgentTool) -> None:
        if enabled and tool.spec.name not in enabled:
            return
        registry.register(tool)

    register(ToolRegistryTool())
    register(SkillDiscoverTool())
    register(SkillInspectTool())
    register(PluginRegistryTool())
    register(McpRegistryTool())
    register(ProjectScriptsTool())
    register(DiagnosisClassifyTool())
    register(DiagnosisRecallTool())
    register(RepairPrepareTool())
    register(RepairStatusTool())
    register(RepairApplyPatchTool())
    register(RepairValidateTool())
    register(RepairOrchestrateValidateTool())
    register(RepairReviewTool())
    register(RepairRollbackTool())
    register(SelfInspectTool())
    register(SelfReflectTool())
    register(SelfRememberTool())
    register(SelfProposeChangeTool())
    register(WebSearchTool())
    register(WebFetchTool())
    register(MemorySearchTool())
    register(MemoryWriteTool())
    register(ContextPackTool())
    register(ContextExpandTool())
    register(CapsuleSummarizeTool())
    register(CapsuleApplyTool())
    register(MemoryConflictsTool())
    register(ListFilesTool())
    register(ReadFileTool())
    register(FindFilesTool())
    register(FileStatTool())
    register(WriteFileTool())
    register(ShellRunTool())
    register(CodexExecTool())
    register(RepoSearchTool())
    register(RepoMapTool())
    register(PatchApplyTool())
    register(TestRunTool())
    register(LintRunTool())
    register(GitStatusTool())
    register(GitDiffTool())
    register(GitExportPatchTool())
    register(GitBranchTool())
    register(GitCreateLocalBranchTool())
    register(GitLogTool())
    register(GitShowTool())
    register(GitCommitTool())
    register(MemvidVerifyTool())
    register(MemvidDoctorTool())
    register(MemvidStatsTool())
    register(MemoryLedgerTool())
    register(MemoryLearnTool())
    register(MemoryConsolidateTool())
    register(MemoryCorrectTool())
    register(MemoryCompactTool())
    register(MemoryInspectTool())
    register(MemoryExportTool())
    register(MemoryImportTool())
    register(SkillInstallTool())
    register(PluginReviewTool())
    register(PluginInstallTool())
    return registry


_SELF_SCHEMAS = {
    "identity_summary",
    "capability_snapshot",
    "user_profile",
    "agent_persona",
    "user_workflow_preference",
    "self_change_request",
    "validation_metadata",
}


def _self_identity() -> dict[str, str]:
    return {
        "name": "Kestrel",
        "display_name": "Soul",
        "description": "A local-first, memory-native engineering agent runtime with nested .mv2 memory layers.",
    }


def _self_snapshot(
    context: ToolContext, *, include_tools: bool, include_state: bool
) -> dict[str, Any]:
    config = context.config
    state = AgentStateStore(config.state_path)
    secret_broker = build_secret_broker(config.secret_store_path, backend=config.secret_backend)
    memory_layers = [
        {
            "layer": layer.value,
            "mv2_file": spec.mv2_file,
            "description": spec.description,
            "update_cadence": spec.update_cadence,
            "min_write_confidence": spec.min_write_confidence,
            "promotion_threshold": spec.promotion_threshold,
        }
        for layer, spec in context.memory.iter_layers()
    ]
    payload: dict[str, Any] = {
        "identity": _self_identity(),
        "provider": {
            "provider": config.provider,
            "model": config.model,
            "fallback_provider": config.fallback_provider,
            "fallback_model": config.fallback_model,
            "base_url_configured": bool(config.base_url),
            "api_key_env": config.api_key_env,
            "api_key_configured": bool(config.api_key_env and os.getenv(config.api_key_env)),
        },
        "config": {
            "backend": config.backend,
            "workspace": str(config.workspace),
            "memory_dir": str(config.memory_dir),
            "secret_store_path": str(config.secret_store_path),
            "secret_backend": config.secret_backend,
            "allow_shell": config.allow_shell,
            "allow_file_write": config.allow_file_write,
            "allow_policy_writes": config.allow_policy_writes,
            "allow_codex_cli": config.allow_codex_cli,
            "allow_plugin_install": config.allow_plugin_install,
            "allow_git_commit": config.allow_git_commit,
            "allow_git_push": config.allow_git_push,
            "allow_remote_mutation": config.allow_remote_mutation,
            "git_write_mode": config.git_write_mode,
            "protected_branches": list(config.protected_branches),
            "allow_memory_import": config.allow_memory_import,
            "allow_executable_skills": config.allow_executable_skills,
            "allow_mcp_network_endpoints": config.allow_mcp_network_endpoints,
            "allow_web": config.allow_web,
            "allow_self_modification": config.allow_self_modification,
            "require_approval_for_high_risk_tools": config.require_approval_for_high_risk_tools,
            "web_backend": config.web_backend,
            "web_timeout_seconds": config.web_timeout_seconds,
            "web_max_results": config.web_max_results,
            "web_max_bytes": config.web_max_bytes,
        },
        "memory_layers": memory_layers,
    }
    if include_tools:
        payload["tools"] = [spec.to_public_dict() for spec in build_default_tools().specs()]
    else:
        payload["tool_count"] = len(build_default_tools().specs())
    if include_state:
        payload["skills"] = _safe_state_list(state.list_skills)
        payload["plugins"] = _safe_state_list(state.list_plugins)
        payload["mcp_servers"] = [
            _redact_mcp_server(server, secret_broker)
            for server in _safe_state_list(state.list_mcp_servers)
        ]
    return payload


def _safe_state_list(loader: Any) -> list[dict[str, Any]]:
    try:
        rows = loader()
    except Exception:
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _redact_mcp_server(server: dict[str, Any], secret_broker: SecretBroker) -> dict[str, Any]:
    safe = dict(server)
    safe.pop("env", None)
    secret_env = safe.pop("secret_env", {})
    if isinstance(secret_env, dict):
        safe["secret_env_status"] = {
            str(key): {
                "env": str(value),
                "secret_ref": str(value) if is_secret_ref(str(value)) else None,
                "configured": bool(secret_broker.resolve(str(value))),
            }
            for key, value in secret_env.items()
        }
    return safe


def _self_reflection_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No validated Soul/self memory matched the query yet."
    titles = [
        str(row.get("record", {}).get("title") or row.get("title") or "self memory")
        for row in rows[:5]
    ]
    return "Relevant Soul/self memory: " + "; ".join(titles)


def _safe_skill_id(skill_id: str) -> bool:
    return skill_id.replace("_", "-").replace("-", "").isalnum()


def _memory_hit_payload(hit: Any) -> dict[str, object]:
    return {
        "score": hit.score,
        "frame_id": hit.frame_id,
        "source_backend": hit.source_backend,
        "snippet": hit.snippet,
        "record": _memory_record_payload(hit.record),
    }


def _memory_record_payload(record: MemoryRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "layer": record.layer.value,
        "kind": record.kind.value,
        "title": record.title,
        "content": record.content,
        "confidence": record.confidence,
        "importance": record.importance,
        "tags": record.tags,
        "metadata": record.metadata,
        "evidence": [
            {"source": evidence.source, "locator": evidence.locator, "quote": evidence.quote}
            for evidence in record.evidence
        ],
    }


def _memory_record_from_payload(item: dict[str, Any]) -> MemoryRecord:
    record = MemoryRecord(
        layer=MemoryLayer(str(item.get("layer", MemoryLayer.WORKING.value))),
        kind=MemoryKind(str(item.get("kind", MemoryKind.OBSERVATION.value))),
        title=str(item.get("title", "Imported memory")),
        content=str(item.get("content", "")),
        confidence=float(item.get("confidence", 0.8)),
        importance=float(item.get("importance", 0.5)),
        tags=dict(item.get("tags", {})) if isinstance(item.get("tags"), dict) else {},
        metadata=dict(item.get("metadata", {})) if isinstance(item.get("metadata"), dict) else {},
    )
    record_id = str(item.get("id") or "").strip()
    if record_id:
        record.id = record_id
    return record


def _layer_arg(value: object) -> MemoryLayer | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return MemoryLayer(text)


def _datetime_arg(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _outcome_arg(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text not in OUTCOME_KINDS:
        raise ValueError(f"Unknown promotion outcome: {text}")
    return text


def _layers_arg(value: object) -> tuple[MemoryLayer, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    layers: list[MemoryLayer] = []
    for item in value:
        text = str(item).strip()
        if text:
            layers.append(MemoryLayer(text))
    return tuple(layers) if layers else None


def _find_memory_by_id(context: ToolContext, lookup_id: str) -> Any | None:
    for backend in context.memory.backends.values():
        records = getattr(backend, "records", None)
        if isinstance(records, list):
            for record in records:
                metadata = getattr(record, "metadata", {})
                if record.id == lookup_id or str(metadata.get("frame_id", "")) == lookup_id:
                    return type("_Hit", (), {"record": record})()
    hits = context.memory.retrieve(RetrievalQuery(query=lookup_id, k_per_layer=5))
    for hit in hits:
        metadata = hit.record.metadata
        if (
            hit.record.id == lookup_id
            or str(metadata.get("frame_id", "")) == lookup_id
            or hit.frame_id == lookup_id
        ):
            return hit
    return None


def _related_frames(
    context: ToolContext,
    frame: Any,
    *,
    include_children: bool,
    include_parents: bool,
) -> list[dict[str, object]]:
    wanted: set[str] = set()
    if include_children:
        wanted.update(frame.child_ids)
    if include_parents:
        wanted.update(frame.parent_ids)
    if not wanted:
        return []
    related: list[dict[str, object]] = []
    for item_id in sorted(wanted):
        hit = _find_memory_by_id(context, item_id)
        if hit is None:
            related.append({"id": item_id, "found": False})
            continue
        record = hit.record
        related.append(
            {
                "id": item_id,
                "found": True,
                "title": record.title,
                "layer": record.layer.value,
                "kind": record.kind.value,
                "snippet": record.content[:500],
            }
        )
    return related


def _truncate_by_tokens(text: str, max_tokens: int) -> str:
    max_chars = max(max_tokens * 4, 0)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[TRUNCATED_BY_CONTEXT_EXPAND]"


def _capsule_apply_plan(
    summary: Any, *, context: ToolContext, include_policy: bool
) -> list[dict[str, object]]:
    kernel = NestedLearningKernel()
    plan: list[dict[str, object]] = []
    for index, signal in enumerate(summary.learning_signals):
        decision = kernel.decide(signal)
        payload = decision.to_payload()
        payload["signal_index"] = index
        payload["signal_title"] = signal.title
        payload["signal_kind"] = signal.kind.value
        payload["requested_target_layer"] = (
            signal.requested_target_layer.value if signal.requested_target_layer else None
        )
        payload["will_write"] = False
        if not decision.accepted or decision.target_layer is None:
            payload["blocked"] = (
                "policy_requires_explicit_instruction"
                if signal.requested_target_layer == MemoryLayer.POLICY
                and not signal.explicit_instruction
                else "nested_learning_rejected"
            )
        elif decision.target_layer == MemoryLayer.POLICY:
            if not include_policy:
                payload["blocked"] = "policy_excluded_from_capsule_apply"
            elif not context.config.allow_policy_writes:
                payload["blocked"] = "policy_write_disabled"
            elif not signal.explicit_instruction:
                payload["blocked"] = "policy_requires_explicit_instruction"
            else:
                payload["will_write"] = True
        else:
            record = kernel.to_memory_record(signal, decision)
            if _memory_has_content_hash(context, decision.target_layer, record.content_hash):
                payload["blocked"] = "duplicate_content_hash"
            else:
                payload["will_write"] = True
        plan.append(payload)
    return plan


def _memory_has_content_hash(context: ToolContext, layer: MemoryLayer, content_hash: str) -> bool:
    backend = context.memory.backends.get(layer)
    records = getattr(backend, "records", None)
    if isinstance(records, list):
        return any(getattr(record, "content_hash", None) == content_hash for record in records)
    hits = context.memory.retrieve(
        RetrievalQuery(query=content_hash, layers=(layer,), k_per_layer=3)
    )
    return any(
        hit.record.content_hash == content_hash
        or hit.record.metadata.get("content_hash") == content_hash
        for hit in hits
    )
