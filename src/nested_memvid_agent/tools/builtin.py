from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
from fnmatch import fnmatchcase
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from ..cognition import RetryPolicy
from ..consolidation import Consolidator
from ..context_frames import default_frame_type_for_memory, estimate_tokens, from_memory_record
from ..context_packer import ContextPacker, ContextPackRequest
from ..diagnosis import classify_failure
from ..models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from ..nested_learning import LearningSignal, NestedLearningKernel
from ..plugin_manager import PluginManager
from ..runtime_models import StrategyProposal, ToolCall, ToolExecution, ToolSpec
from ..secret_broker import SecretBroker, is_secret_ref
from ..skill_manager import validate_skill_manifest
from ..state_store import AgentStateStore
from ..task_capsule import summarize_run_capsule
from .base import AgentTool, ToolContext
from .registry import ToolRegistry


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
            layers = tuple(MemoryLayer(value) for value in layer_values) if isinstance(layer_values, list) else tuple(MemoryLayer)
        except ValueError as exc:
            return self._result(
                call,
                success=False,
                content=f"Unknown memory layer: {exc}",
                error="invalid_tool_arguments",
            )
        k = int(arguments.get("k", 8))
        hits = context.memory.retrieve(RetrievalQuery(query=query, layers=layers, k_per_layer=k))
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
        description="Write a validated memory record to a nested memory layer. Policy writes require config enablement.",
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
        try:
            frame_type = str(arguments.get("frame_type") or default_frame_type_for_memory(kind, layer))
            parent_ids_arg = arguments.get("parent_ids")
            child_ids_arg = arguments.get("child_ids")
            parent_ids = [str(item) for item in parent_ids_arg] if isinstance(parent_ids_arg, list) else []
            child_ids = [str(item) for item in child_ids_arg] if isinstance(child_ids_arg, list) else []
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
            return self._result(call, success=True, content=f"Wrote memory {record_id}", data={"record_id": record_id})
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
                    token_budget=int(arguments.get("token_budget", context.config.context_pack_token_budget)),
                    allowed_layers=layers,
                    expand_raw=bool(arguments.get("expand_raw", context.config.context_pack_expand_raw)),
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
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
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
            return self._result(call, success=False, content="Missing frame_id or record_id", error="missing_id")
        try:
            hit = _find_memory_by_id(context, lookup_id)
            if hit is None:
                return self._result(call, success=False, content=f"No memory found for {lookup_id}", error="not_found")
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
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="context_expand_failed")


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
            return self._result(call, success=False, content="Missing run_id", error="missing_run_id")
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
            payload = {**summary.to_payload(), "dry_run": True, "nested_learning_decisions": decisions}
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="capsule_summarize_failed")


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
        public_arguments = {key: value for key, value in arguments.items() if not str(key).startswith("_")}
        call_id = str(arguments.get("_tool_call_id") or "")
        call = (
            ToolCall(name=self.spec.name, arguments=public_arguments, id=call_id)
            if call_id
            else ToolCall(name=self.spec.name, arguments=public_arguments)
        )
        run_id = str(arguments.get("run_id", "")).strip()
        if not run_id:
            return self._result(call, success=False, content="Missing run_id", error="missing_run_id")
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
                payload = {**summary.to_payload(), "dry_run": True, "applied": False, "decisions": plan}
                return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)

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
            payload = {**summary.to_payload(), "dry_run": False, "applied": wrote, "decisions": plan}
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
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
            hits = context.memory.retrieve(RetrievalQuery(query=query, layers=layers, k_per_layer=k))
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
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="memory_conflicts_failed")


class ListFilesTool(AgentTool):
    spec = ToolSpec(
        name="file.list",
        description="List files under the configured workspace root.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            path = _safe_path(context.workspace, str(arguments.get("path", ".")))
            max_entries = int(arguments.get("max_entries", 80))
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:max_entries]
            data = [{"name": p.name, "type": "dir" if p.is_dir() else "file"} for p in entries]
            return self._result(call, success=True, content=json.dumps(data, indent=2), data={"entries": data})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_list_failed")


class ReadFileTool(AgentTool):
    spec = ToolSpec(
        name="file.read",
        description="Read a text file under the configured workspace root.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}},
            "required": ["path"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            path = _safe_path(context.workspace, str(arguments.get("path", "")))
            max_chars = int(arguments.get("max_chars", 20_000))
            text = path.read_text(errors="replace")[:max_chars]
            return self._result(call, success=True, content=text, data={"path": str(path), "chars": len(text)})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_read_failed")


class WriteFileTool(AgentTool):
    spec = ToolSpec(
        name="file.write",
        description="Write a text file under workspace. Disabled unless allow_file_write is true.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            path = _safe_path(context.workspace, str(arguments.get("path", "")))
            path.parent.mkdir(parents=True, exist_ok=True)
            text = str(arguments.get("content", ""))
            path.write_text(text)
            return self._result(call, success=True, content=f"Wrote {path}", data={"path": str(path), "chars": len(text)})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="file_write_failed")


class ShellRunTool(AgentTool):
    spec = ToolSpec(
        name="shell.run",
        description="Run an allowlisted shell command in the workspace. Disabled unless allow_shell is true.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "integer"}},
            "required": ["command"],
        },
        risk="high",
        requires_approval=True,
    )
    allowed_first_tokens = {"echo", "pwd", "python", "python3", "pytest", "ruff", "mypy", "ls", "cat"}

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command")
        if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
            return self._result(call, success=False, content="command must be list[str]", error="bad_command")
        command = list(command_raw)
        remote_mutation = _remote_mutation_violation(command)
        git_push_blocked = remote_mutation == "git push" and not context.config.allow_git_push
        if remote_mutation and (git_push_blocked or not context.config.allow_remote_mutation):
            return self._result(
                call,
                success=False,
                content=f"Remote mutation command blocked: {remote_mutation}",
                error="remote_mutation_blocked",
                data={
                    "violation": remote_mutation,
                    "allow_git_push": context.config.allow_git_push,
                    "allow_remote_mutation": context.config.allow_remote_mutation,
                },
            )
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(call, success=False, content="Command is not allowlisted", error="command_not_allowlisted")
        command = _normalize_python_command(command)
        try:
            completed = subprocess.run(  # noqa: S603 - intentionally allowlisted
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=int(arguments.get("timeout", 30)),
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode},
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="shell_failed")


class CodexExecTool(AgentTool):
    spec = ToolSpec(
        name="codex.exec",
        description="Delegate a bounded non-interactive task to the local Codex CLI in this workspace.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "model": {"type": "string"},
                "sandbox": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write"],
                    "default": "read-only",
                },
                "timeout": {"type": "integer", "minimum": 30, "maximum": 3600},
                "ephemeral": {"type": "boolean", "default": True},
                "json_events": {"type": "boolean", "default": False},
                "skip_git_repo_check": {"type": "boolean", "default": False},
                "max_output_chars": {"type": "integer", "minimum": 1000, "maximum": 100000},
            },
            "required": ["prompt"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("codex-cli", "delegation"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return self._result(call, success=False, content="Missing prompt", error="missing_prompt")

        sandbox = str(arguments.get("sandbox", "read-only"))
        if sandbox not in {"read-only", "workspace-write"}:
            return self._result(call, success=False, content="Unsupported Codex sandbox", error="bad_sandbox")

        timeout = max(30, min(int(arguments.get("timeout", 600)), 3600))
        max_output_chars = max(1000, min(int(arguments.get("max_output_chars", 40_000)), 100_000))
        command = [
            "codex",
            "exec",
            "--cd",
            str(context.workspace.resolve()),
            "--sandbox",
            sandbox,
            "--color",
            "never",
        ]
        model = str(arguments.get("model", "")).strip()
        if model:
            command.extend(["--model", model])
        if bool(arguments.get("ephemeral", True)):
            command.append("--ephemeral")
        if bool(arguments.get("json_events", False)):
            command.append("--json")
        if bool(arguments.get("skip_git_repo_check", False)):
            command.append("--skip-git-repo-check")
        command.append(prompt)

        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable and argument vector
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout = _truncate(completed.stdout, max_output_chars)
            stderr = _truncate(completed.stderr, max_output_chars)
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "returncode": completed.returncode,
                    "sandbox": sandbox,
                    "model": model or None,
                    "stdout_truncated": len(completed.stdout) > max_output_chars,
                    "stderr_truncated": len(completed.stderr) > max_output_chars,
                },
                error=None if completed.returncode == 0 else "codex_nonzero_exit",
            )
        except FileNotFoundError:
            return self._result(call, success=False, content="Codex CLI not found on PATH.", error="codex_cli_not_found")
        except subprocess.TimeoutExpired as exc:
            return self._result(call, success=False, content=f"Codex CLI timed out after {timeout}s: {exc}", error="codex_timeout")
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="codex_cli_failed")


class RepoSearchTool(AgentTool):
    spec = ToolSpec(
        name="repo.search",
        description="Search text files under the configured workspace root without leaving the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_file_bytes": {"type": "integer", "minimum": 256, "maximum": 1000000},
            },
            "required": ["query"],
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        try:
            root = _safe_path(context.workspace, str(arguments.get("path", ".")))
            max_results = int(arguments.get("max_results", 25))
            max_file_bytes = int(arguments.get("max_file_bytes", 300_000))
            rows: list[dict[str, object]] = []
            query_lower = query.lower()
            for path in _iter_repo_files(root, context.workspace, max_file_bytes=max_file_bytes):
                rel = path.relative_to(context.workspace.resolve())
                try:
                    for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
                        if query_lower in line.lower():
                            rows.append({"path": str(rel), "line": lineno, "text": line[:400]})
                            if len(rows) >= max_results:
                                return self._result(
                                    call,
                                    success=True,
                                    content=json.dumps(rows, indent=2),
                                    data={"matches": rows},
                                )
                except UnicodeDecodeError:
                    continue
            return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"matches": rows})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repo_search_failed")


class RepoMapTool(AgentTool):
    spec = ToolSpec(
        name="repo.map",
        description="Return a bounded file tree for the configured workspace root.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 500},
                "max_depth": {"type": "integer", "minimum": 0, "maximum": 8},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            root = _safe_path(context.workspace, str(arguments.get("path", ".")))
            workspace = context.workspace.resolve()
            max_entries = int(arguments.get("max_entries", 120))
            max_depth = int(arguments.get("max_depth", 3))
            rows: list[dict[str, object]] = []
            for current_root, dirs, files in os.walk(root):
                current = Path(current_root)
                rel_current = current.relative_to(workspace)
                depth = 0 if str(rel_current) == "." else len(rel_current.parts)
                dirs[:] = sorted(d for d in dirs if not _skip_repo_name(d))
                if depth > max_depth:
                    dirs[:] = []
                    continue
                for dirname in dirs:
                    rows.append({"path": str((current / dirname).relative_to(workspace)), "type": "dir"})
                    if len(rows) >= max_entries:
                        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"entries": rows})
                for filename in sorted(files):
                    if _skip_repo_name(filename):
                        continue
                    item = current / filename
                    rows.append({"path": str(item.relative_to(workspace)), "type": "file", "bytes": item.stat().st_size})
                    if len(rows) >= max_entries:
                        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"entries": rows})
            return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"entries": rows})
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repo_map_failed")


class PatchApplyTool(AgentTool):
    spec = ToolSpec(
        name="patch.apply",
        description="Apply a unified diff inside the workspace. Disabled unless file writes are enabled.",
        parameters={
            "type": "object",
            "properties": {
                "patch": {"type": "string"},
                "check": {"type": "boolean"},
            },
            "required": ["patch"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        patch_text = str(arguments.get("patch", ""))
        check_only = bool(arguments.get("check", False))
        if not patch_text.strip():
            return self._result(call, success=False, content="Missing patch", error="missing_patch")
        try:
            _validate_patch_paths(context.workspace, patch_text)
            command = ["git", "apply", "--check"] if check_only else ["git", "apply", "--whitespace=nowarn"]
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments
                command,
                cwd=context.workspace,
                input=patch_text,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode, "check": check_only},
                error=None if completed.returncode == 0 else "patch_apply_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="patch_apply_failed")


class TestRunTool(AgentTool):
    spec = ToolSpec(
        name="test.run",
        description="Run a bounded test command in the workspace. Disabled unless shell execution is enabled.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
            },
        },
        risk="high",
        requires_approval=True,
    )
    allowed_first_tokens = {"pytest", "python", "python3"}

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command", ["pytest", "-q"])
        if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
            return self._result(call, success=False, content="command must be list[str]", error="bad_command")
        command = list(command_raw)
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(call, success=False, content="Command is not allowlisted", error="command_not_allowlisted")
        command = _normalize_python_command(command)
        try:
            completed = subprocess.run(  # noqa: S603 - intentionally allowlisted
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=int(arguments.get("timeout", 120)),
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode},
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="test_run_failed")


class LintRunTool(AgentTool):
    spec = ToolSpec(
        name="lint.run",
        description="Run a bounded lint/typecheck command in the workspace. Disabled unless shell execution is enabled.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
            },
        },
        risk="high",
        requires_approval=True,
    )
    allowed_first_tokens = {"ruff", "mypy", "python", "python3"}

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command", ["ruff", "check", "."])
        if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
            return self._result(call, success=False, content="command must be list[str]", error="bad_command")
        command = list(command_raw)
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(call, success=False, content="Command is not allowlisted", error="command_not_allowlisted")
        command = _normalize_python_command(command)
        try:
            completed = subprocess.run(  # noqa: S603 - intentionally allowlisted
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=int(arguments.get("timeout", 120)),
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode},
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="lint_run_failed")


class RepairPrepareTool(AgentTool):
    spec = ToolSpec(
        name="repair.prepare",
        description="Prepare an isolated repair branch from the current clean workspace and record the base SHA. Requires approval.",
        parameters={
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "allow_dirty": {"type": "boolean"},
            },
            "required": ["branch"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "git-isolation"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        branch = str(arguments.get("branch", "")).strip()
        if not branch:
            return self._result(call, success=False, content="Missing branch", error="missing_branch")
        if not _safe_branch_name(branch):
            return self._result(call, success=False, content=f"Unsafe branch name: {branch}", error="unsafe_branch_name")
        try:
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if base.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=f"Unable to resolve base SHA. STDERR:\n{base.stderr}",
                    error="git_base_failed",
                    data={"returncode": base.returncode},
                )
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if status.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=f"Unable to inspect worktree. STDERR:\n{status.stderr}",
                    error="git_status_failed",
                    data={"returncode": status.returncode},
                )
            if status.stdout.strip() and not bool(arguments.get("allow_dirty", False)):
                return self._result(
                    call,
                    success=False,
                    content="Refusing to prepare repair branch with uncommitted changes.",
                    error="dirty_worktree",
                    data={"dirty_status": status.stdout},
                )
            created = subprocess.run(
                ["git", "switch", "-c", branch],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={created.returncode}\nSTDOUT:\n{created.stdout}\nSTDERR:\n{created.stderr}"
            success = created.returncode == 0
            return self._result(
                call,
                success=success,
                content=content,
                data={
                    "mode": "branch",
                    "branch": branch,
                    "base_sha": base.stdout.strip(),
                    "returncode": created.returncode,
                    "approval_required_before_commit": True,
                },
                error=None if success else "repair_prepare_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repair_prepare_failed")


class RepairStatusTool(AgentTool):
    spec = ToolSpec(
        name="repair.status",
        description="Report whether the workspace is on a repair branch, changed files, and optional base SHA trace metadata.",
        parameters={
            "type": "object",
            "properties": {"base_sha": {"type": "string"}},
        },
        capabilities=("safe-repair", "git-isolation"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            head = _git_output(context.workspace, ["git", "rev-parse", "HEAD"])
            status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            changed_files = _changed_files_from_status(status)
            base_sha = str(arguments.get("base_sha", "")).strip() or None
            payload = {
                "branch": branch,
                "head_sha": head,
                "base_sha": base_sha,
                "active_repair_branch": _is_repair_branch(branch),
                "dirty": bool(status.strip()),
                "changed_files": changed_files,
                "raw_status": status,
            }
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repair_status_failed")


class RepairApplyPatchTool(AgentTool):
    spec = ToolSpec(
        name="repair.apply_patch",
        description="Apply a repair patch only while on an active repair branch. Requires approval and file-write capability.",
        parameters={
            "type": "object",
            "properties": {"patch": {"type": "string"}, "check": {"type": "boolean"}},
            "required": ["patch"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "patching"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        patch_text = str(arguments.get("patch", ""))
        if not patch_text.strip():
            return self._result(call, success=False, content="Missing patch", error="missing_patch")
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(
                    call,
                    success=False,
                    content=f"Refusing to apply repair patch on non-repair branch: {branch}",
                    error="not_repair_branch",
                    data={"branch": branch},
                )
            _validate_patch_paths(context.workspace, patch_text)
            command = ["git", "apply", "--check"] if bool(arguments.get("check", False)) else ["git", "apply", "--whitespace=nowarn"]
            completed = subprocess.run(
                command,
                cwd=context.workspace,
                input=patch_text,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"branch": branch, "returncode": completed.returncode, "check": bool(arguments.get("check", False))},
                error=None if completed.returncode == 0 else "repair_patch_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repair_patch_failed")


class RepairValidateTool(AgentTool):
    spec = ToolSpec(
        name="repair.validate",
        description="Run a bounded repair validation command on an active repair branch and classify failures.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "integer"}},
            "required": ["command"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "validation", "self-diagnosis"),
    )
    allowed_first_tokens = {"pytest", "python", "python3", "ruff", "mypy"}

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command")
        if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
            return self._result(call, success=False, content="command must be list[str]", error="bad_command")
        command = list(command_raw)
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(call, success=False, content="Command is not allowlisted", error="command_not_allowlisted")
        command = _normalize_python_command(command)
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(call, success=False, content=f"Not on a repair branch: {branch}", error="not_repair_branch")
            completed = subprocess.run(
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=int(arguments.get("timeout", 120)),
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            diagnosis = classify_failure(content, source="repair.validate").to_payload() if completed.returncode != 0 else None
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"branch": branch, "returncode": completed.returncode, "diagnosis": diagnosis},
                error=None if completed.returncode == 0 else "repair_validation_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repair_validation_failed")


class RepairOrchestrateValidateTool(AgentTool):
    spec = ToolSpec(
        name="repair.orchestrate_validate",
        description="Run repair validation on an active repair branch, classify failures, recall prior lessons, and gate unchanged retries.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
                "previous_command": {"type": "array", "items": {"type": "string"}},
                "proposed_strategy": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["command"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "validation", "self-diagnosis", "failure-recall"),
    )
    allowed_first_tokens = RepairValidateTool.allowed_first_tokens

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command")
        if not isinstance(command_raw, list) or not all(isinstance(item, str) for item in command_raw):
            return self._result(call, success=False, content="command must be list[str]", error="bad_command")
        command = list(command_raw)
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(call, success=False, content="Command is not allowlisted", error="command_not_allowlisted")
        command = _normalize_python_command(command)
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(call, success=False, content=f"Not on a repair branch: {branch}", error="not_repair_branch", data={"branch": branch})
            status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            completed = subprocess.run(  # noqa: S603 - intentionally allowlisted repair validation command
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=int(arguments.get("timeout", 120)),
                check=False,
            )
            validation_content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            validation = {
                "success": completed.returncode == 0,
                "returncode": completed.returncode,
                "content": validation_content,
            }
            diagnosis = None
            recall: dict[str, Any] = {"hits": [], "query": "", "retry_guidance": {"must_change_strategy_before_retry": False}}
            retry_gate: dict[str, Any] = {
                "retry_allowed": True,
                "must_change_strategy_before_retry": False,
                "reason": "Validation passed; no retry needed." if completed.returncode == 0 else "No similar lesson was found; follow the diagnostic playbook.",
                "strategy_changed": True,
            }
            next_action = "create_repair_review_before_commit" if completed.returncode == 0 else "retry_with_diagnostic_playbook"
            if completed.returncode != 0:
                classification = classify_failure(validation_content, source="repair.orchestrate_validate")
                diagnosis = classification.to_payload()
                recall = _recall_failure_lessons(context, classification.category, validation_content, max(1, min(int(arguments.get("k", 5)), 10)))
                previous = arguments.get("previous_command")
                previous_command = previous if isinstance(previous, list) and all(isinstance(item, str) for item in previous) else []
                previous_command = _normalize_python_command(list(previous_command))
                proposed_strategy = str(arguments.get("proposed_strategy", "")).strip()
                has_lessons = bool(recall["hits"])
                command_repeated = previous_command == command
                must_change = has_lessons and command_repeated
                strategy = (
                    StrategyProposal(changed_strategy=proposed_strategy)
                    if proposed_strategy
                    else None
                )
                retry_decision = RetryPolicy().assess_actions(
                    previous_action=" ".join(previous_command),
                    new_action=" ".join(command),
                    strategy=strategy,
                    require_change=must_change,
                    similar_lessons=_recall_hit_titles(recall),
                )
                retry_allowed = retry_decision.retry_allowed
                retry_gate = {
                    "retry_allowed": retry_allowed,
                    "must_change_strategy_before_retry": must_change,
                    "strategy_changed": bool(
                        retry_decision.strategy_diff
                        and retry_decision.strategy_diff.is_meaningfully_different
                    ),
                    "command_repeated": command_repeated,
                    "reason": retry_decision.reason,
                    "required_change": retry_decision.required_change,
                    "strategy_diff": retry_decision.strategy_diff.to_payload()
                    if retry_decision.strategy_diff
                    else None,
                }
                next_action = "apply_changed_strategy_then_retry" if retry_allowed and must_change else "change_strategy_before_retry" if not retry_allowed else "retry_with_diagnostic_playbook"
            payload = {
                "branch": branch,
                "active_repair_branch": True,
                "changed_files": _changed_files_from_status(status),
                "validation": validation,
                "diagnosis": diagnosis,
                "recall": recall,
                "retry_gate": retry_gate,
                "next_action": next_action,
                "commit_allowed": False,
                "approval_required_before_commit": True,
            }
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repair_orchestration_failed")


class RepairReviewTool(AgentTool):
    spec = ToolSpec(
        name="repair.review",
        description="Create a durable reviewer gate artifact for a validated repair diff before commit.",
        parameters={
            "type": "object",
            "properties": {
                "validation": {"type": "object"},
                "summary": {"type": "string"},
                "risks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["validation"],
        },
        risk="medium",
        capabilities=("safe-repair", "review-gate"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        validation = arguments.get("validation")
        if not isinstance(validation, dict):
            return self._result(call, success=False, content="validation must be an object", error="bad_validation")
        if validation.get("success") is not True:
            return self._result(call, success=False, content="Repair review requires successful validation.", error="validation_not_successful")
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(call, success=False, content=f"Not on a repair branch: {branch}", error="not_repair_branch", data={"branch": branch})
            diff = _git_output(context.workspace, ["git", "diff", "HEAD", "--"])
            if not diff.strip():
                return self._result(call, success=False, content="No repair diff found to review.", error="empty_repair_diff", data={"branch": branch})
            status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            head = _git_output(context.workspace, ["git", "rev-parse", "HEAD"])
            diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
            review_id = f"repair_review_{diff_hash[:16]}"
            risks_arg = arguments.get("risks")
            risks = [str(item) for item in risks_arg] if isinstance(risks_arg, list) else []
            payload = {
                "review_id": review_id,
                "branch": branch,
                "head_sha": head,
                "diff_hash": diff_hash,
                "changed_files": _changed_files_from_status(status),
                "summary": str(arguments.get("summary", "")).strip(),
                "risks": risks,
                "validation": validation,
                "commit_gate": {
                    "commit_allowed": True,
                    "approval_required_before_commit": True,
                    "reason": "Successful validation and reviewer artifact are present; commit still requires exact-call approval.",
                },
            }
            review_dir = context.workspace / ".nest" / "repair_reviews"
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / f"{review_id}.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repair_review_failed")


class RepairRollbackTool(AgentTool):
    spec = ToolSpec(
        name="repair.rollback",
        description="Rollback uncommitted changes on an active repair branch. Requires approval and never runs on main/master.",
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "review_id": {"type": "string"},
            },
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "rollback"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(call, success=False, content=f"Not on a repair branch: {branch}", error="not_repair_branch")
            before_status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            before_diff = _git_output(context.workspace, ["git", "diff", "HEAD", "--"])
            before_payload = {
                "status": before_status,
                "changed_files": [path for path in _changed_files_from_status(before_status) if not path.startswith(".nest/") and path != ".nest"],
                "diff_hash": hashlib.sha256(before_diff.encode("utf-8")).hexdigest() if before_diff else "",
            }
            reset = subprocess.run(["git", "checkout", "--", "."], cwd=context.workspace, capture_output=True, text=True, timeout=30, check=False)
            clean = subprocess.run(["git", "clean", "-fd"], cwd=context.workspace, capture_output=True, text=True, timeout=30, check=False)
            after_status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            success = reset.returncode == 0 and clean.returncode == 0
            reason = str(arguments.get("reason", "manual_rollback")).strip() or "manual_rollback"
            review_id = str(arguments.get("review_id", "")).strip()
            artifact_payload = {
                "branch": branch,
                "reason": reason,
                "review_id": review_id or None,
                "before": before_payload,
                "after": {"status": after_status, "changed_files": _changed_files_from_status(after_status)},
                "commands": {
                    "reset": {"returncode": reset.returncode, "stdout": reset.stdout, "stderr": reset.stderr},
                    "clean": {"returncode": clean.returncode, "stdout": clean.stdout, "stderr": clean.stderr},
                },
                "success": success,
            }
            artifact_seed = json.dumps({"branch": branch, "reason": reason, "review_id": review_id, "before": before_payload}, sort_keys=True)
            artifact_id = f"repair_rollback_{hashlib.sha256(artifact_seed.encode('utf-8')).hexdigest()[:16]}"
            artifact_relpath = Path(".nest") / "repair_rollbacks" / f"{artifact_id}.json"
            artifact_path = context.workspace / artifact_relpath
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(json.dumps(artifact_payload, indent=2, sort_keys=True), encoding="utf-8")
            content = (
                f"reset_exit_code={reset.returncode}\nRESET_STDOUT:\n{reset.stdout}\nRESET_STDERR:\n{reset.stderr}\n"
                f"clean_exit_code={clean.returncode}\nCLEAN_STDOUT:\n{clean.stdout}\nCLEAN_STDERR:\n{clean.stderr}\n"
                f"rollback_artifact={artifact_relpath.as_posix()}"
            )
            return self._result(
                call,
                success=success,
                content=content,
                data={
                    "branch": branch,
                    "reason": reason,
                    "review_id": review_id or None,
                    "reset_returncode": reset.returncode,
                    "clean_returncode": clean.returncode,
                    "rollback_artifact": artifact_relpath.as_posix(),
                    "before": before_payload,
                    "after": artifact_payload["after"],
                },
                error=None if success else "repair_rollback_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repair_rollback_failed")


class GitStatusTool(AgentTool):
    spec = ToolSpec(
        name="git.status",
        description="Return read-only git status for the workspace.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del arguments
        call = ToolCall(name=self.spec.name, arguments={})
        return _git_read(call, context, ["git", "status", "--short", "--branch"], "git_status_failed")


class GitDiffTool(AgentTool):
    spec = ToolSpec(
        name="git.diff",
        description="Return read-only git diff for the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command = ["git", "diff", "--cached"] if bool(arguments.get("staged", False)) else ["git", "diff"]
        result = _git_read(call, context, command, "git_diff_failed")
        max_chars = int(arguments.get("max_chars", 40_000))
        if len(result.content) > max_chars:
            return self._result(
                call,
                success=result.success,
                content=result.content[:max_chars] + "\n... truncated ...",
                data={**result.data, "truncated": True},
                error=result.error,
            )
        return result


class GitExportPatchTool(AgentTool):
    spec = ToolSpec(
        name="git.export_patch",
        description="Export the current git diff to a local .kestrel/improvements patch file. Never pushes.",
        parameters={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean"},
                "path": {"type": "string"},
            },
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command = ["git", "diff", "--cached"] if bool(arguments.get("staged", False)) else ["git", "diff"]
        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=f"Unable to export patch. STDERR:\n{completed.stderr}",
                    error="git_export_patch_failed",
                    data={"returncode": completed.returncode},
                )
            patch = completed.stdout
            if not patch.strip():
                return self._result(call, success=False, content="No diff to export.", error="empty_diff")
            path_arg = str(arguments.get("path", "")).strip()
            if path_arg:
                patch_path = _safe_path(context.workspace, path_arg)
                relpath = patch_path.relative_to(context.workspace.resolve())
                if relpath.parts[:2] != (".kestrel", "improvements"):
                    return self._result(
                        call,
                        success=False,
                        content="Patch exports must stay under .kestrel/improvements/.",
                        error="invalid_patch_path",
                    )
            else:
                patch_id = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]
                relpath = Path(".kestrel") / "improvements" / f"improvement_{patch_id}" / "diff.patch"
                patch_path = context.workspace.resolve() / relpath
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text(patch, encoding="utf-8")
            return self._result(
                call,
                success=True,
                content=f"Exported patch to {relpath.as_posix()}",
                data={"path": relpath.as_posix(), "chars": len(patch), "staged": bool(arguments.get("staged", False))},
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="git_export_patch_failed")


class GitBranchTool(AgentTool):
    spec = ToolSpec(
        name="git.branch",
        description="Return read-only branch information for the workspace.",
        parameters={"type": "object", "properties": {"all": {"type": "boolean"}}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        command = ["git", "branch", "--all"] if bool(arguments.get("all", False)) else ["git", "branch", "--show-current"]
        return _git_read(ToolCall(name=self.spec.name, arguments=arguments), context, command, "git_branch_failed")


class GitCreateLocalBranchTool(AgentTool):
    spec = ToolSpec(
        name="git.create_local_branch",
        description="Create a local git branch in the workspace. Never pushes or tracks a remote.",
        parameters={
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "checkout": {"type": "boolean", "default": True},
            },
            "required": ["branch"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        branch = str(arguments.get("branch", "")).strip()
        checkout = bool(arguments.get("checkout", True))
        if not _safe_branch_name(branch):
            return self._result(call, success=False, content=f"Invalid branch name: {branch}", error="invalid_branch")
        if _is_protected_branch(branch, context.config.protected_branches):
            return self._result(
                call,
                success=False,
                content=f"Refusing to create protected branch name: {branch}",
                error="protected_branch",
                data={"branch": branch, "protected_branches": list(context.config.protected_branches)},
            )
        command = ["git", "switch", "-c", branch] if checkout else ["git", "branch", branch]
        try:
            before_branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"branch": branch, "checkout": checkout, "previous_branch": before_branch, "returncode": completed.returncode},
                error=None if completed.returncode == 0 else "git_create_branch_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="git_create_branch_failed")


class GitCommitTool(AgentTool):
    spec = ToolSpec(
        name="git.commit",
        description="Commit already-staged workspace changes. Requires explicit approval and never pushes.",
        parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}, "repair_review_id": {"type": "string"}},
            "required": ["message"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        message = str(arguments.get("message", "")).strip()
        if not message:
            return self._result(call, success=False, content="Missing commit message", error="missing_message")
        try:
            repair_review_id: str | None = None
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if _is_protected_branch(branch, context.config.protected_branches):
                return self._result(
                    call,
                    success=False,
                    content=f"Refusing to commit on protected branch: {branch}",
                    error="protected_branch",
                    data={"branch": branch, "protected_branches": list(context.config.protected_branches)},
                )
            if _is_repair_branch(branch):
                review_check = _validate_repair_review_gate(context.workspace, branch, str(arguments.get("repair_review_id", "")).strip())
                if not review_check["ok"]:
                    return self._result(
                        call,
                        success=False,
                        content=str(review_check["content"]),
                        error=str(review_check["error"]),
                        data={key: value for key, value in review_check.items() if key not in {"ok", "content", "error"}},
                    )
                repair_review_id = str(review_check["review_id"])
                changed_files = [str(path) for path in review_check.get("changed_files", []) if str(path).strip()]
                if changed_files:
                    staged = subprocess.run(
                        ["git", "add", "--", *changed_files],
                        cwd=context.workspace,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    if staged.returncode != 0:
                        return self._result(
                            call,
                            success=False,
                            content=f"Unable to stage reviewed repair files. STDERR:\n{staged.stderr}",
                            error="repair_stage_failed",
                            data={"branch": branch, "repair_review_id": repair_review_id, "changed_files": changed_files, "returncode": staged.returncode},
                        )
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments
                ["git", "commit", "-m", message],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            data: dict[str, Any] = {"returncode": completed.returncode}
            if completed.returncode == 0:
                sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=context.workspace,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if sha.returncode == 0:
                    data["commit_sha"] = sha.stdout.strip()
            if repair_review_id:
                data["repair_review_id"] = repair_review_id
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data=data,
                error=None if completed.returncode == 0 else "git_commit_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="git_commit_failed")


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
        return self._result(call, success=all(rows.values()), content=json.dumps(rows, indent=2), data={"layers": rows})


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
        return self._result(call, success=True, content=json.dumps(rows, indent=2, default=str), data={"layers": rows})


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
        return self._result(call, success=True, content=json.dumps(rows, indent=2, default=str), data={"layers": rows})


class MemoryConsolidateTool(AgentTool):
    spec = ToolSpec(
        name="memory.consolidate",
        description="Promote a validated memory candidate through the nested consolidation pipeline.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source_layer": {"type": "string"},
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
            hits = context.memory.retrieve(RetrievalQuery(query=query, layers=layers, k_per_layer=1))
            if not hits:
                return self._result(call, success=False, content="No consolidation candidate found.", error="candidate_not_found")
            validation_score = float(arguments.get("validation_score", 0.7))
            repeat_count = int(arguments.get("repeat_count", 1))
            explicit_instruction = bool(arguments.get("explicit_instruction", False))
            candidate = Consolidator().propose(
                hits[0].record,
                validation_score=validation_score,
                repeat_count=repeat_count,
                explicit_instruction=explicit_instruction,
            )
            if candidate is None:
                return self._result(call, success=True, content="No promotion proposed.", data={"promoted": False})
            if candidate.target_layer == MemoryLayer.POLICY and not context.config.allow_policy_writes:
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
            }
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="memory_consolidate_failed")


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
            return self._result(call, success=False, content="Missing content", error="missing_content")
        try:
            source_layer = _layer_arg(arguments.get("source_layer")) or MemoryLayer.WORKING
            target_layer = _layer_arg(arguments.get("target_layer"))
            kind = MemoryKind(str(arguments.get("kind", MemoryKind.OBSERVATION.value)))
            signal = LearningSignal(
                title=title,
                content=content,
                kind=kind,
                source_layer=source_layer,
                confidence=float(arguments.get("confidence", 0.6)),
                importance=float(arguments.get("importance", 0.5)),
                validation_score=float(arguments.get("validation_score", 0.7)),
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
            if decision.target_layer == MemoryLayer.POLICY and not context.config.allow_policy_writes:
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
            }
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
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
            RetrievalQuery(query=query, layers=layers, k_per_layer=int(arguments.get("k", 8)))
        )
        rows = [_memory_hit_payload(hit) for hit in hits]
        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"hits": rows})


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
        return self._result(call, success=True, content=json.dumps(rows, indent=2), data={"records": rows})


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
            return self._result(call, success=False, content="records must be a list", error="bad_records")
        if not all(isinstance(item, dict) for item in raw_records):
            return self._result(call, success=False, content="Every record must be an object", error="bad_records")
        try:
            records = [_memory_record_from_payload(item) for item in raw_records]
        except Exception as exc:  # noqa: BLE001 - import payload validation boundary
            return self._result(call, success=False, content=str(exc), error="bad_records")
        if any(record.layer == MemoryLayer.POLICY for record in records) and not context.config.allow_policy_writes:
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
                return self._result(call, success=False, content=str(exc), error="memory_import_failed")
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
            return self._result(call, success=False, content="manifest must be an object", error="bad_manifest")
        if not instructions.strip():
            return self._result(call, success=False, content="instructions cannot be empty", error="missing_instructions")
        manifest = dict(manifest_raw)
        skill_id = str(manifest.get("id", "")).strip()
        if not skill_id:
            return self._result(call, success=False, content="manifest.id is required", error="missing_skill_id")
        if not _safe_skill_id(skill_id):
            return self._result(call, success=False, content=f"Unsafe skill id: {skill_id}", error="unsafe_skill_id")
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
            return self._result(call, success=False, content=json.dumps(payload, indent=2), data=payload, error="invalid_skill_manifest")

        try:
            skill_dir = _safe_path(context.config.skills_dir, skill_id)
            payload["path"] = str(skill_dir)
            if payload["dry_run"]:
                return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
            if skill_dir.exists() and not bool(arguments.get("overwrite", False)):
                return self._result(call, success=False, content=json.dumps(payload, indent=2), data=payload, error="skill_exists")
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "skill.json").write_text(manifest_text + "\n", encoding="utf-8")
            (skill_dir / "SKILL.md").write_text(instructions, encoding="utf-8")
            payload["installed"] = True
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
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
            return self._result(call, success=False, content="source is required", error="missing_source")
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
            return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)
        except Exception as exc:  # noqa: BLE001 - plugin install boundary reports structured failure
            return self._result(call, success=False, content=str(exc), error="plugin_install_failed")


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
        query = str(arguments.get("query") or "Kestrel identity capabilities user workflow preferences").strip()
        k = max(1, min(int(arguments.get("k", 6)), 12))
        hits = context.memory.retrieve(RetrievalQuery(query=query, layers=(MemoryLayer.SELF,), k_per_layer=k))
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
            return self._result(call, success=False, content="Missing content", error="missing_content")
        if schema not in _SELF_SCHEMAS:
            return self._result(call, success=False, content=f"Unknown self schema: {schema}", error="invalid_self_schema")
        if not validation_status:
            return self._result(call, success=False, content="Missing validation_status", error="missing_validation_status")
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
            explicit_instruction=validation_status in {"user_confirmed", "operator_confirmed", "explicit_request"},
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
            return self._result(call, success=False, content=decision.reason, data=decision.to_payload(), error="self_memory_rejected")
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
            return self._result(call, success=False, content=str(exc), data=decision.to_payload(), error="self_remember_failed")
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
            return self._result(call, success=False, content="Missing request", error="missing_request")
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
        data = {**payload, "memory_record_id": remember.data.get("record_id"), "memory_error": remember.error}
        return self._result(call, success=remember.success, content=json.dumps(data, indent=2), data=data, error=remember.error)


class WebSearchTool(AgentTool):
    spec = ToolSpec(
        name="web.search",
        description="Search the public web for read-only outside context. Disabled unless allow_web is enabled.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
        risk="medium",
        capabilities=("web", "outside-context", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        query = str(arguments.get("query", "")).strip()
        if not query:
            return self._result(call, success=False, content="Missing query", error="missing_query")
        max_results = max(1, min(int(arguments.get("max_results", context.config.web_max_results)), 10))
        try:
            results = _mock_search_results(query, max_results) if context.config.web_backend == "mock" else _direct_web_search(query, context, max_results)
        except Exception as exc:  # noqa: BLE001 - web boundary
            return self._result(call, success=False, content=str(exc), error="web_search_failed")
        payload = {"query": query, "backend": context.config.web_backend, "results": results}
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


class WebFetchTool(AgentTool):
    spec = ToolSpec(
        name="web.fetch",
        description="Fetch a public HTTP(S) page for read-only outside context. Private and local network URLs are rejected.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 1000000},
            },
            "required": ["url"],
        },
        risk="medium",
        capabilities=("web", "outside-context", "read-only"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        url = str(arguments.get("url", "")).strip()
        if not url:
            return self._result(call, success=False, content="Missing url", error="missing_url")
        max_bytes = max(1024, min(int(arguments.get("max_bytes", context.config.web_max_bytes)), 1_000_000))
        parsed = urlparse(url)
        if context.config.web_backend == "mock" and parsed.hostname == "mock.kestrel.local":
            content = _mock_fetch_content(url)
            payload = {"url": url, "backend": "mock", "bytes": len(content.encode("utf-8")), "citation": url}
            return self._result(call, success=True, content=content, data=payload)
        safe, reason = _public_web_url_allowed(url)
        if not safe:
            return self._result(call, success=False, content=reason, error="unsafe_url")
        try:
            content, final_url = _fetch_public_text(url, timeout=context.config.web_timeout_seconds, max_bytes=max_bytes)
        except Exception as exc:  # noqa: BLE001 - web boundary
            return self._result(call, success=False, content=str(exc), error="web_fetch_failed")
        payload = {"url": final_url, "backend": context.config.web_backend, "bytes": len(content.encode("utf-8")), "citation": final_url}
        return self._result(call, success=True, content=content, data=payload)


class DiagnosisClassifyTool(AgentTool):
    spec = ToolSpec(
        name="diagnosis.classify",
        description="Classify a runtime/tool/test/provider failure and return the matching diagnostic playbook.",
        parameters={
            "type": "object",
            "properties": {
                "failure_text": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["failure_text"],
        },
        capabilities=("self-diagnosis", "failure-classification"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del context
        call = ToolCall(name=self.spec.name, arguments=arguments)
        failure_text = str(arguments.get("failure_text", "")).strip()
        if not failure_text:
            return self._result(call, success=False, content="Missing failure_text", error="missing_failure_text")
        classification = classify_failure(failure_text, source=str(arguments.get("source", "")))
        payload = classification.to_payload()
        content = json.dumps(payload, indent=2)
        return self._result(call, success=True, content=content, data=payload)


class DiagnosisRecallTool(AgentTool):
    spec = ToolSpec(
        name="diagnosis.recall",
        description="Classify a failure and retrieve similar prior failure lessons from procedural/episodic memory before retrying.",
        parameters={
            "type": "object",
            "properties": {
                "failure_text": {"type": "string"},
                "source": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["failure_text"],
        },
        capabilities=("self-diagnosis", "failure-recall", "nested-memory"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        failure_text = str(arguments.get("failure_text", "")).strip()
        if not failure_text:
            return self._result(call, success=False, content="Missing failure_text", error="missing_failure_text")
        classification = classify_failure(failure_text, source=str(arguments.get("source", "")))
        k = max(1, min(int(arguments.get("k", 5)), 10))
        recall = _recall_failure_lessons(context, classification.category, failure_text, k)
        payload = {
            **classification.to_payload(),
            **recall,
        }
        return self._result(call, success=True, content=json.dumps(payload, indent=2), data=payload)


def build_default_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(DiagnosisClassifyTool())
    registry.register(DiagnosisRecallTool())
    registry.register(RepairPrepareTool())
    registry.register(RepairStatusTool())
    registry.register(RepairApplyPatchTool())
    registry.register(RepairValidateTool())
    registry.register(RepairOrchestrateValidateTool())
    registry.register(RepairReviewTool())
    registry.register(RepairRollbackTool())
    registry.register(SelfInspectTool())
    registry.register(SelfReflectTool())
    registry.register(SelfRememberTool())
    registry.register(SelfProposeChangeTool())
    registry.register(WebSearchTool())
    registry.register(WebFetchTool())
    registry.register(MemorySearchTool())
    registry.register(MemoryWriteTool())
    registry.register(ContextPackTool())
    registry.register(ContextExpandTool())
    registry.register(CapsuleSummarizeTool())
    registry.register(CapsuleApplyTool())
    registry.register(MemoryConflictsTool())
    registry.register(ListFilesTool())
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ShellRunTool())
    registry.register(CodexExecTool())
    registry.register(RepoSearchTool())
    registry.register(RepoMapTool())
    registry.register(PatchApplyTool())
    registry.register(TestRunTool())
    registry.register(LintRunTool())
    registry.register(GitStatusTool())
    registry.register(GitDiffTool())
    registry.register(GitExportPatchTool())
    registry.register(GitBranchTool())
    registry.register(GitCreateLocalBranchTool())
    registry.register(GitCommitTool())
    registry.register(MemvidVerifyTool())
    registry.register(MemvidDoctorTool())
    registry.register(MemvidStatsTool())
    registry.register(MemoryLearnTool())
    registry.register(MemoryConsolidateTool())
    registry.register(MemoryInspectTool())
    registry.register(MemoryExportTool())
    registry.register(MemoryImportTool())
    registry.register(SkillInstallTool())
    registry.register(PluginInstallTool())
    return registry


_SELF_SCHEMAS = {
    "identity_summary",
    "capability_snapshot",
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


def _self_snapshot(context: ToolContext, *, include_tools: bool, include_state: bool) -> dict[str, Any]:
    config = context.config
    state = AgentStateStore(config.state_path)
    secret_broker = SecretBroker(config.secret_store_path)
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
        payload["mcp_servers"] = [_redact_mcp_server(server, secret_broker) for server in _safe_state_list(state.list_mcp_servers)]
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
    titles = [str(row.get("record", {}).get("title") or row.get("title") or "self memory") for row in rows[:5]]
    return "Relevant Soul/self memory: " + "; ".join(titles)


def _mock_search_results(query: str, max_results: int) -> list[dict[str, Any]]:
    slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "query"
    return [
        {
            "title": f"Mock web result {index + 1}: {query}",
            "url": f"https://mock.kestrel.local/search/{slug}/{index + 1}",
            "snippet": f"Deterministic outside context for {query}.",
            "source": "mock",
            "citation": f"https://mock.kestrel.local/search/{slug}/{index + 1}",
        }
        for index in range(max_results)
    ]


def _mock_fetch_content(url: str) -> str:
    return f"Mock web page for Kestrel\nURL: {url}\nThis deterministic page supplies outside context without network access."


def _direct_web_search(query: str, context: ToolContext, max_results: int) -> list[dict[str, Any]]:
    search_url = "https://duckduckgo.com/html/?" + urlencode({"q": query})
    html, final_url = _fetch_public_text(search_url, timeout=context.config.web_timeout_seconds, max_bytes=context.config.web_max_bytes)
    del final_url
    results: list[dict[str, Any]] = []
    pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
    for match in pattern.finditer(html):
        href = unescape(match.group(1))
        title = _strip_html(match.group(2))
        url = _unwrap_duckduckgo_url(href)
        safe, _reason = _public_web_url_allowed(url)
        if not safe:
            continue
        results.append({"title": title, "url": url, "snippet": "", "source": "duckduckgo", "citation": url})
        if len(results) >= max_results:
            break
    return results


def _fetch_public_text(url: str, *, timeout: int, max_bytes: int) -> tuple[str, str]:
    safe, reason = _public_web_url_allowed(url)
    if not safe:
        raise ValueError(reason)
    request = Request(url, headers={"User-Agent": "Kestrel/0.1 (+local-first-agent)"})
    with urlopen(request, timeout=max(timeout, 1)) as response:  # noqa: S310 - URL is validated before fetching.
        raw = response.read(max_bytes + 1)
        final_url = str(response.geturl())
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
    safe_final, final_reason = _public_web_url_allowed(final_url)
    if not safe_final:
        raise ValueError(final_reason)
    encoding = response.headers.get_content_charset() or "utf-8"
    return raw.decode(encoding, errors="replace"), final_url


def _public_web_url_allowed(url: str) -> tuple[bool, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "Only http:// and https:// URLs are allowed."
    host = parsed.hostname
    if not host:
        return False, "URL must include a host."
    lowered = host.lower()
    if lowered in {"localhost"} or lowered.endswith(".localhost") or lowered.endswith(".local"):
        return False, "Local hostnames are not allowed."
    try:
        ip = ipaddress.ip_address(lowered)
        return _public_ip_allowed(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(lowered, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return False, f"Unable to resolve host: {exc}"
    for info in infos:
        address = info[4][0]
        try:
            allowed, reason = _public_ip_allowed(ipaddress.ip_address(address))
        except ValueError:
            return False, f"Resolved invalid IP address: {address}"
        if not allowed:
            return False, reason
    return True, ""


def _public_ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str]:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False, "Private, local, link-local, multicast, reserved, and unspecified addresses are not allowed."
    return True, ""


def _unwrap_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        values = parse_qs(parsed.query).get("uddg")
        if values:
            return values[0]
    return url


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", unescape(value))).strip()


def _recall_failure_lessons(context: ToolContext, category: str, failure_text: str, k: int) -> dict[str, Any]:
    query = f"{category} {failure_text}"
    hits = context.memory.retrieve(
        RetrievalQuery(
            query=query,
            layers=(MemoryLayer.PROCEDURAL, MemoryLayer.EPISODIC, MemoryLayer.WORKING),
            k_per_layer=k,
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
    return {
        "query": query,
        "hits": rows,
        "retry_guidance": {
            "must_change_strategy_before_retry": bool(rows),
            "reason": "Similar prior failures were found; use recalled lessons before repeating the action."
            if rows
            else "No prior lesson found; follow the diagnostic playbook and record validated findings.",
        },
    }


def _recall_hit_titles(recall: dict[str, Any]) -> tuple[str, ...]:
    hits = recall.get("hits", [])
    if not isinstance(hits, list):
        return ()
    return tuple(str(hit.get("title", "")) for hit in hits if isinstance(hit, dict))


def _safe_branch_name(name: str) -> bool:
    if not name or name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")):
        return False
    if ".." in name or "//" in name or "@{" in name or "\\" in name:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")
    return all(char in allowed for char in name)


def _is_repair_branch(name: str) -> bool:
    return name.startswith(("codex/", "repair/", "fix/")) and name not in {"main", "master"}


def _is_protected_branch(name: str, protected_patterns: tuple[str, ...]) -> bool:
    branch = name.strip()
    return bool(branch) and any(fnmatchcase(branch, pattern) for pattern in protected_patterns)


def _remote_mutation_violation(command: list[str]) -> str | None:
    if not command:
        return None
    lowered = [part.lower() for part in command]
    executable = Path(lowered[0]).name
    if executable == "git" and len(lowered) >= 2:
        if lowered[1] == "push":
            return "git push"
        if lowered[1] == "tag":
            return "git tag"
        if lowered[1:3] == ["remote", "set-url"]:
            return "git remote set-url"
    if executable == "gh" and len(lowered) >= 3:
        if lowered[1:3] == ["repo", "edit"]:
            return "gh repo edit"
        if lowered[1:3] == ["secret", "set"]:
            return "gh secret set"
        if lowered[1:3] == ["workflow", "enable"]:
            return "gh workflow enable"
    joined = " ".join(lowered)
    searchable = re.sub(r"[^a-z0-9_./*-]+", " ", joined)
    checks = (
        ("git push", "git push"),
        ("git tag", "git tag"),
        ("git remote set-url", "git remote set-url"),
        ("gh repo edit", "gh repo edit"),
        ("gh secret set", "gh secret set"),
        ("gh workflow enable", "gh workflow enable"),
        ("rm -rf .git", "rm -rf .git"),
        (".git/config", ".git/config"),
    )
    for needle, label in checks:
        if needle in searchable:
            return label
    return None


def _git_output(workspace: Path, command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git command failed ({completed.returncode}): {' '.join(command)}\n{completed.stderr}")
    return completed.stdout.strip()


def _validate_repair_review_gate(workspace: Path, branch: str, review_id: str) -> dict[str, Any]:
    if not review_id:
        return {
            "ok": False,
            "error": "repair_review_required",
            "content": "Repair branch commits require a repair_review_id from repair.review.",
            "branch": branch,
        }
    if not review_id.startswith("repair_review_") or "/" in review_id or ".." in review_id:
        return {"ok": False, "error": "invalid_repair_review_id", "content": f"Invalid repair_review_id: {review_id}", "branch": branch}
    path = workspace / ".nest" / "repair_reviews" / f"{review_id}.json"
    if not path.exists():
        return {"ok": False, "error": "repair_review_not_found", "content": f"Repair review artifact not found: {review_id}", "branch": branch}
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": "repair_review_invalid", "content": f"Repair review artifact is invalid JSON: {exc}", "branch": branch}
    if review.get("branch") != branch:
        return {
            "ok": False,
            "error": "repair_review_branch_mismatch",
            "content": f"Repair review was created for {review.get('branch')}, not {branch}.",
            "branch": branch,
            "review_id": review_id,
        }
    if review.get("validation", {}).get("success") is not True or review.get("commit_gate", {}).get("commit_allowed") is not True:
        return {
            "ok": False,
            "error": "repair_review_not_approved",
            "content": "Repair review does not contain a successful validation commit gate.",
            "branch": branch,
            "review_id": review_id,
        }
    diff = _git_output(workspace, ["git", "diff", "HEAD", "--"])
    diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    if review.get("diff_hash") != diff_hash:
        return {
            "ok": False,
            "error": "repair_review_stale",
            "content": "Repair diff changed after review; run repair.review again before committing.",
            "branch": branch,
            "review_id": review_id,
            "expected_diff_hash": review.get("diff_hash"),
            "actual_diff_hash": diff_hash,
        }
    return {"ok": True, "review_id": review_id, "diff_hash": diff_hash, "branch": branch, "changed_files": review.get("changed_files", [])}


def _changed_files_from_status(status: str) -> list[str]:
    files: list[str] = []
    for line in status.splitlines():
        if not line:
            continue
        path = line[3:] if len(line) > 3 and line[2] == " " else line[2:]
        if " -> " in path:
            path = path.split(" -> ", maxsplit=1)[1]
        files.append(path.strip())
    return files


def _safe_path(root: Path, relative: str) -> Path:
    root_resolved = root.resolve()
    path = (root_resolved / relative).resolve()
    if root_resolved not in path.parents and path != root_resolved:
        raise ValueError(f"Path escapes workspace: {relative}")
    return path


def _safe_skill_id(skill_id: str) -> bool:
    return skill_id.replace("_", "-").replace("-", "").isalnum()


def _normalize_python_command(command: list[str]) -> list[str]:
    if command and Path(command[0]).name in {"python", "python3"}:
        return [sys.executable, *command[1:]]
    return command


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... truncated ..."


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


def _skip_repo_name(name: str) -> bool:
    return name in {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"} or name.startswith(".")


def _iter_repo_files(root: Path, workspace: Path, *, max_file_bytes: int) -> list[Path]:
    workspace_root = workspace.resolve()
    files: list[Path] = []
    if root.is_file():
        if root.stat().st_size <= max_file_bytes:
            return [root]
        return []
    for current_root, dirs, filenames in os.walk(root):
        dirs[:] = sorted(dirname for dirname in dirs if not _skip_repo_name(dirname))
        current = Path(current_root)
        for filename in sorted(filenames):
            if _skip_repo_name(filename):
                continue
            path = current / filename
            try:
                path.relative_to(workspace_root)
            except ValueError:
                continue
            if path.stat().st_size <= max_file_bytes:
                files.append(path)
    return files


def _validate_patch_paths(workspace: Path, patch_text: str) -> None:
    for line in patch_text.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw = line[4:].split("\t", maxsplit=1)[0].strip()
        if raw == "/dev/null":
            continue
        if raw.startswith(("a/", "b/")):
            raw = raw[2:]
        _safe_path(workspace, raw)


def _git_read(call: ToolCall, context: ToolContext, command: list[str], error_code: str) -> ToolExecution:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed read-only git commands
            command,
            cwd=context.workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        return ToolExecution(
            call=call,
            success=completed.returncode == 0,
            content=content,
            data={"returncode": completed.returncode},
            error=None if completed.returncode == 0 else error_code,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolExecution(call=call, success=False, content=str(exc), error=error_code)


def _layer_arg(value: object) -> MemoryLayer | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return MemoryLayer(text)


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
        if hit.record.id == lookup_id or str(metadata.get("frame_id", "")) == lookup_id or hit.frame_id == lookup_id:
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


def _capsule_apply_plan(summary: Any, *, context: ToolContext, include_policy: bool) -> list[dict[str, object]]:
    kernel = NestedLearningKernel()
    plan: list[dict[str, object]] = []
    for index, signal in enumerate(summary.learning_signals):
        decision = kernel.decide(signal)
        payload = decision.to_payload()
        payload["signal_index"] = index
        payload["signal_title"] = signal.title
        payload["signal_kind"] = signal.kind.value
        payload["requested_target_layer"] = signal.requested_target_layer.value if signal.requested_target_layer else None
        payload["will_write"] = False
        if not decision.accepted or decision.target_layer is None:
            payload["blocked"] = (
                "policy_requires_explicit_instruction"
                if signal.requested_target_layer == MemoryLayer.POLICY and not signal.explicit_instruction
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
    hits = context.memory.retrieve(RetrievalQuery(query=content_hash, layers=(layer,), k_per_layer=3))
    return any(hit.record.content_hash == content_hash or hit.record.metadata.get("content_hash") == content_hash for hit in hits)
