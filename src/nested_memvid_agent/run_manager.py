from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from typing import Any
from uuid import uuid4

from .agent import NestedMV2Agent, _sanitize_tool_execution
from .app_factory import build_agent
from .capability_policy import CapabilityPolicy, tool_spec_digest
from .config import AgentConfig
from .diagnosis import classify_failure
from .event_bus import RunEventBus
from .event_log import redact_secrets
from .graph_runtime import DurableOrchestrationRuntime, GraphRuntimeServices
from .mcp_manager import MCPManager
from .models import MemoryLayer
from .nested_learning import NestedLearningKernel
from .plugin_manager import PluginManager
from .process_liveness import process_is_alive
from .retention import RetentionCompactor
from .runtime_models import AgentTurnResult, LLMStreamEvent, ToolCall, ToolExecution, ToolSpec
from .runtime_settings import RuntimeSettings, apply_runtime_settings, runtime_settings_snapshot
from .skill_manager import SkillManager
from .state_store import (
    AgentStateStore,
    ApprovalConflictError,
    RunRecord,
    StateCapacityError,
    TaskNodeRecord,
    utc_now,
)
from .task_capsule import summarize_run_capsule, write_turn_capsule
from .tools.base import ToolContext
from .tools.builtin import build_default_tools
from .tools.process_tools import cancel_subprocesses_for_run
from .tools.registry import ToolRegistry
from .tracing import SpanRecorder
from .worker_isolation import prepare_git_worktree

_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
_TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "skipped"}
_PUBLICATION_FENCE_WAIT_SECONDS = 30.0


class RunCapacityError(RuntimeError):
    """Raised when both primary worker slots and the durable admission queue are full."""


class RunManager:
    """Background run orchestration for the web UI and API."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        state: AgentStateStore,
        events: RunEventBus,
        mcp: MCPManager,
        skills: SkillManager,
        plugins: PluginManager | None = None,
        secret_resolver: Callable[[str | None], str | None] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.events = events
        self.mcp = mcp
        self.skills = skills
        self.plugins = plugins or PluginManager(config.plugins_dir, state)
        self.capabilities = CapabilityPolicy(state, lambda: self.config)
        self.mcp.capability_policy = self.capabilities
        self.skills.capability_policy = self.capabilities
        self.secret_resolver = secret_resolver
        self._lock = Lock()
        self._approval_lock = Lock()
        self._approval_call_arguments: dict[str, tuple[str, dict[str, Any]]] = {}
        self._threads: dict[str, Thread] = {}
        self._publication_events: dict[str, Event] = {}
        self._active_primary_runs: set[str] = set()
        self._reserved_primary_runs: set[str] = set()
        self._queued_primary_runs: deque[tuple[str, Any, tuple[Any, ...], Event]] = deque()
        self._cancelled: set[str] = set()
        self._lost_run_leases: set[str] = set()
        self._admission_rejections = 0
        self._lease_owner = f"manager_{os.getpid()}_{uuid4().hex}"
        self._startup_queued_run_ids: list[str] = []
        self.reconcile_capabilities()
        self.startup_recovery = self._reconcile_startup()
        self.startup_worker_recovery = self._reconcile_startup_workers()
        self._resume_startup_queued_runs()

    def reconcile_capabilities(self) -> None:
        """Reconcile extension inventory at startup or an explicit refresh.

        Catalog reads and registry construction deliberately do not call this:
        GET endpoints must remain read-only, and operator overrides must not be
        rewritten as a side effect of listing tools.
        """

        self.plugins.sync_all()
        self.skills.discover()

    def _reconcile_startup(self) -> dict[str, list[str]]:
        """Fail interrupted work while preserving intentional approval waits."""
        pending_runs = {str(item["run_id"]) for item in self.state.list_approvals(status="pending")}
        report: dict[str, list[str]] = {"failed": [], "preserved": []}
        for run in self.state.list_nonterminal_runs():
            if _run_has_fresh_lease(run) and _lease_owner_is_alive(run.lease_owner) is not False:
                self.events.publish(
                    run.run_id,
                    "run.recovery_deferred_live_lease",
                    {"lease_owner": run.lease_owner, "lease_expires_at": run.lease_expires_at},
                )
                report["preserved"].append(run.run_id)
                continue
            if run.run_id in pending_runs:
                if run.recovery_reason == "preserved_pending_approval" and run.lease_owner is None:
                    continue
                self.state.transition_run(
                    run.run_id,
                    "blocked",
                    stop_reason="approval_required",
                    recovery_reason="preserved_pending_approval",
                )
                self.events.publish(run.run_id, "run.recovered_waiting_approval", {"status": "blocked"})
                report["preserved"].append(run.run_id)
                continue
            if run.status == "queued":
                self._startup_queued_run_ids.append(run.run_id)
                self.events.publish(run.run_id, "run.recovered_queued", {"status": "queued"})
                report["preserved"].append(run.run_id)
                continue
            interrupted_at = utc_now()
            recovered = self.state.transition_run(
                run.run_id,
                "failed",
                stop_reason="interrupted_by_restart",
                error="Run was interrupted before the runtime restarted; automatic replay was suppressed to avoid duplicate side effects.",
                interrupted_at=interrupted_at,
                recovery_reason=f"startup_reconciliation:{run.status}",
            )
            if recovered.status == "failed":
                self.events.publish(
                    run.run_id,
                    "run.interrupted",
                    {"previous_status": run.status, "interrupted_at": interrupted_at},
                )
                report["failed"].append(run.run_id)
        return report

    def _resume_startup_queued_runs(self) -> None:
        for run_id in self._startup_queued_run_ids:
            run = self.state.get_run(run_id)
            if run.status != "queued":
                continue
            try:
                config = self._config_for_run(run)
                self._reserve_primary_run(run_id)
                self._schedule_primary_run(
                    run_id,
                    self._run_agent_turn,
                    config,
                    run.message,
                    run.session_id,
                )
            except Exception as exc:  # noqa: BLE001 - startup must terminally reconcile failed retries
                self._abort_primary_admission(run_id, exc)

    def _reconcile_startup_workers(self) -> dict[str, list[str]]:
        report: dict[str, list[str]] = {"failed": [], "preserved": []}
        for subagent in self.state.list_nonterminal_subagent_runs():
            task = self.state.get_task_node(subagent.task_id) if subagent.task_id else None
            task_result = task.result if task and isinstance(task.result, dict) else {}
            owner = str(task_result.get("worker_owner") or "")
            heartbeat_at = str(task_result.get("worker_heartbeat_at") or "")
            if _worker_is_live(
                owner,
                heartbeat_at,
                ttl_seconds=self.config.run_lease_ttl_seconds,
            ):
                report["preserved"].append(subagent.subagent_id)
                continue
            error = "Interrupted worker was reconciled during startup"
            self.state.update_subagent_run(
                subagent.subagent_id,
                status="failed",
                error=error,
            )
            if subagent.task_id and task is not None:
                if task.status not in _TERMINAL_TASK_STATUSES:
                    self.state.update_task_node(
                        subagent.task_id,
                        status="failed",
                        failure_reason=error,
                    )
            self.events.publish(
                subagent.run_id,
                "subagent.recovered_failed",
                {"subagent_id": subagent.subagent_id, "reason": "startup_reconciliation"},
            )
            report["failed"].append(subagent.subagent_id)
        return report

    @contextmanager
    def _run_lease(self, run_id: str, config: AgentConfig) -> Iterator[RunRecord | None]:
        lease = self.state.acquire_run_lease(
            run_id,
            owner=self._lease_owner,
            ttl_seconds=config.run_lease_ttl_seconds,
        )
        if lease is None:
            self.events.publish(run_id, "run.lease_rejected", {"owner": self._lease_owner})
            yield None
            return
        with self._lock:
            self._lost_run_leases.discard(run_id)
        stop = Event()
        interval = max(0.01, min(config.run_heartbeat_interval_seconds, config.run_lease_ttl_seconds / 3))

        def mark_lost(reason: str, error: Exception | None = None) -> None:
            with self._lock:
                self._lost_run_leases.add(run_id)
            cancel_subprocesses_for_run(run_id)
            payload: dict[str, Any] = {
                "owner": self._lease_owner,
                "generation": lease.lease_generation,
                "reason": reason,
            }
            if error is not None:
                payload["error_type"] = type(error).__name__
            try:
                terminal = self.state.transition_run(
                    run_id,
                    "failed",
                    lease_owner=self._lease_owner,
                    lease_generation=lease.lease_generation,
                    stop_reason="run_lease_lost",
                    error=f"Execution lease lost: {reason}",
                    recovery_reason=f"execution_fence:{reason}",
                )
                payload["terminalized"] = terminal.status == "failed"
                if terminal.status == "failed":
                    payload["cancelled_task_ids"] = self.state.cancel_tasks_for_run(run_id)
                    payload["cancelled_subagent_ids"] = self.state.cancel_subagents_for_run(run_id)
            except Exception:
                payload["terminalized"] = False
            try:
                self.events.publish(run_id, "run.lease_lost", payload)
            except Exception:
                pass

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    renewed = self.state.renew_run_lease(
                        run_id,
                        owner=self._lease_owner,
                        generation=lease.lease_generation,
                        ttl_seconds=config.run_lease_ttl_seconds,
                    )
                except Exception as exc:  # noqa: BLE001 - a lost heartbeat must stop side effects
                    mark_lost("heartbeat_error", exc)
                    return
                if renewed is not None:
                    continue
                try:
                    current_status = self.state.get_run(run_id).status
                except Exception as exc:  # noqa: BLE001 - unreadable state is a failed execution fence
                    mark_lost("state_unavailable", exc)
                    return
                if current_status not in _TERMINAL_RUN_STATUSES | {"blocked"}:
                    mark_lost("lease_rejected")
                return

        heartbeat_thread = Thread(target=heartbeat, name=f"kestrel-heartbeat-{run_id}", daemon=True)
        heartbeat_thread.start()
        try:
            yield lease
        finally:
            stop.set()
            heartbeat_thread.join(timeout=max(interval * 2, 0.1))
            self.state.release_run_lease(
                run_id,
                owner=self._lease_owner,
                generation=lease.lease_generation,
            )

    @contextmanager
    def _worker_heartbeat(
        self,
        task_id: str | None,
        config: AgentConfig,
        *,
        run_id: str,
        worker_owner: str,
        worker_claim_id: str,
    ) -> Iterator[Event]:
        lost = Event()
        if task_id is None:
            yield lost
            return
        stop = Event()
        interval = max(0.01, config.run_heartbeat_interval_seconds)

        def mark_lost(reason: str, error: Exception | None = None) -> None:
            lost.set()
            cancel_subprocesses_for_run(run_id)
            payload: dict[str, Any] = {
                "task_id": task_id,
                "worker_owner": worker_owner,
                "worker_claim_id": worker_claim_id,
                "reason": reason,
            }
            if error is not None:
                payload["error_type"] = type(error).__name__
            try:
                _, revoked = self.state.transition_task_claim(
                    task_id,
                    "failed",
                    run_id=run_id,
                    worker_owner=worker_owner,
                    worker_claim_id=worker_claim_id,
                    increment_attempt=True,
                    failure_reason=f"Worker heartbeat lost: {reason}",
                    result={"error": f"Worker heartbeat lost: {reason}"},
                )
                payload["claim_revoked"] = revoked
            except Exception:
                payload["claim_revoked"] = False
            try:
                self.events.publish(run_id, "worker.heartbeat_lost", payload)
            except Exception:
                pass

        def heartbeat() -> None:
            while not stop.wait(interval):
                try:
                    renewed = self.state.heartbeat_task_claim(
                        task_id,
                        run_id=run_id,
                        worker_owner=worker_owner,
                        worker_claim_id=worker_claim_id,
                    )
                except Exception as exc:  # noqa: BLE001 - heartbeat errors revoke the worker fence
                    mark_lost("heartbeat_error", exc)
                    return
                if not renewed:
                    mark_lost("claim_rejected")
                    return

        try:
            renewed = self.state.heartbeat_task_claim(
                task_id,
                run_id=run_id,
                worker_owner=worker_owner,
                worker_claim_id=worker_claim_id,
            )
        except Exception as exc:  # noqa: BLE001 - the initial renewal is part of the execution fence
            mark_lost("heartbeat_error", exc)
        else:
            if not renewed:
                mark_lost("claim_rejected")

        thread: Thread | None = None
        if not lost.is_set():
            thread = Thread(target=heartbeat, name=f"kestrel-worker-heartbeat-{task_id}", daemon=True)
            thread.start()
        try:
            yield lost
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=max(interval * 2, 0.1))

    def create_run(
        self,
        *,
        message: str,
        session_id: str | None = None,
        workspace: Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        autonomy_mode: str = "background",
    ) -> RunRecord:
        run_id = f"run_{uuid4().hex}"
        self._reserve_primary_run(run_id)
        try:
            normalized_autonomy = (
                autonomy_mode
                if autonomy_mode in {"background", "manual", "autonomous"}
                else "background"
            )
            run_config = replace(
                self.config,
                workspace=(workspace or self.config.workspace),
                provider=provider or self.config.provider,
                model=model or self.config.model,
                enable_autonomous_scheduler=normalized_autonomy == "autonomous"
                or (
                    normalized_autonomy == "background"
                    and self.config.enable_autonomous_scheduler
                ),
            )
            config_snapshot = _effective_config_snapshot(run_config)
            run = self._create_admitted_run(
                run_id=run_id,
                message=message,
                session_id=session_id or run_id,
                workspace=str(run_config.workspace),
                provider=run_config.provider,
                model=run_config.model,
                config_revision=str(config_snapshot["revision"]),
                config_snapshot=config_snapshot,
                max_nonterminal_runs=max(1, run_config.max_concurrent_runs)
                + max(0, run_config.max_queued_runs),
            )
            self._initialize_primary_run(
                run=run,
                message=message,
                autonomy_mode=normalized_autonomy,
                run_config=run_config,
            )
        except StateCapacityError as exc:
            self._abort_primary_admission(run_id, exc)
            with self._lock:
                self._admission_rejections += 1
            raise RunCapacityError("run_capacity_exhausted") from exc
        except Exception as exc:
            self._abort_primary_admission(run_id, exc)
            raise
        return run

    def _initialize_primary_run(
        self,
        *,
        run: RunRecord,
        message: str,
        autonomy_mode: str,
        run_config: AgentConfig,
    ) -> None:
        root = self.state.create_task_node(
            task_id=f"task_{uuid4().hex}",
            run_id=run.run_id,
            title="Root objective",
            goal=message,
            profile="planner",
            status="queued",
            approved=True,
            plan={
                "autonomy_mode": autonomy_mode,
                "decomposition": "initial",
                "provider": run_config.provider,
                "model": run_config.model,
            },
            acceptance_criteria=["User objective is addressed or explicitly blocked with next steps."],
        )
        recent_messages = [
            prior.message
            for prior in self.state.list_runs_for_session(run.session_id)
            if prior.run_id != run.run_id and prior.message.strip()
        ][-5:]
        planned_tasks = (
            _initial_task_plan(message, recent_messages=recent_messages)
            if autonomy_mode != "manual"
            else []
        )
        for planned in planned_tasks:
            dependencies = [
                root.task_id if dependency == "root" else dependency
                for dependency in planned["dependencies"]
            ]
            self.state.create_task_node(
                task_id=str(planned["task_id"]),
                run_id=run.run_id,
                parent_id=root.task_id,
                title=str(planned["title"]),
                goal=str(planned["goal"]),
                profile=str(planned["profile"]),
                status="queued",
                approved=planned["risk"] == "low",
                dependencies=dependencies,
                required_tools=planned["required_tools"],
                risk=str(planned["risk"]),
                acceptance_criteria=planned["acceptance_criteria"],
            )
        self.events.publish(
            run.run_id,
            "run.queued",
            {
                "message": message,
                "session_id": run.session_id,
                "provider": run_config.provider,
                "model": run_config.model,
                "autonomy_mode": autonomy_mode,
            },
        )
        self._schedule_primary_run(
            run.run_id,
            self._run_agent_turn,
            run_config,
            message,
            run.session_id,
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.state.get_run(run_id)
        payload = self._public_run_payload(run, wait_for_publication=True)
        approvals = [
            approval for approval in self.list_approvals() if approval["run_id"] == run_id
        ]
        return {**payload, "approvals": approvals}

    def list_runs(self) -> list[dict[str, Any]]:
        return [
            self._public_run_payload(run, wait_for_publication=False)
            for run in self.state.list_runs()
        ]

    def list_approvals(self, status: str | None = None) -> list[dict[str, Any]]:
        newly_expired = self.state.expire_pending_approvals()
        self._finalize_expired_approvals(newly_expired)
        return self.state.list_approvals(status=status, expire=False)

    def list_runs_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return [
            self._public_run_payload(run, wait_for_publication=False)
            for run in self.state.list_runs_for_session(session_id)
        ]

    def _public_run_payload(
        self,
        run: RunRecord,
        *,
        wait_for_publication: bool,
    ) -> dict[str, Any]:
        """Hide durable terminal state until its owning cycle is fully published."""

        if run.status not in _TERMINAL_RUN_STATUSES | {"blocked"}:
            return asdict(run)
        with self._lock:
            publication = self._publication_events.get(run.run_id)
            owner_thread = self._threads.get(run.run_id)
        if publication is None or owner_thread is current_thread():
            return asdict(run)
        published = publication.is_set()
        if wait_for_publication and not published:
            published = publication.wait(timeout=_PUBLICATION_FENCE_WAIT_SECONDS)
        if published:
            return asdict(self.state.get_run(run.run_id))
        latest = self.state.get_run(run.run_id)
        if latest.status not in _TERMINAL_RUN_STATUSES | {"blocked"}:
            return asdict(latest)
        payload = asdict(latest)
        payload.update(
            {
                "status": "running",
                "stop_reason": "publication_pending",
                "publication_pending": True,
            }
        )
        return payload

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.state.list_sessions()

    def run_trace(self, run_id: str, *, limit: int = 1000) -> dict[str, Any]:
        run = self.get_run(run_id)
        timeline = self.state.list_run_steps(run_id, limit=limit)
        traces: dict[str, list[dict[str, Any]]] = {
            "tool": [],
            "memory": [],
            "context": [],
            "provider": [],
            "approval": [],
            "error": [],
            "span": [],
            "lifecycle": [],
        }
        for event in timeline:
            traces[_trace_category(event)].append(event)
        spans = [asdict(span) for span in self.state.list_trace_spans(run_id)]
        first = timeline[0]["created_at"] if timeline else None
        last = timeline[-1]["created_at"] if timeline else None
        return {
            "run": run,
            "summary": {
                "event_count": len(timeline),
                "span_count": len(spans),
                "first_event_at": first,
                "last_event_at": last,
                "trace_counts": {name: len(events) for name, events in traces.items()},
                "span_counts": _span_counts(spans),
            },
            "timeline": timeline,
            "spans": spans,
            "traces": traces,
        }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        removed_from_queue = False
        cancelled_now = False
        queued_publication: Event | None = None
        with self._lock:
            self._cancelled.add(run_id)
            current = self.state.get_run(run_id)
            if current.status in _TERMINAL_RUN_STATUSES:
                result = asdict(current)
            else:
                retained = deque(item for item in self._queued_primary_runs if item[0] != run_id)
                removed_from_queue = len(retained) != len(self._queued_primary_runs)
                self._queued_primary_runs = retained
                if removed_from_queue:
                    self._reserved_primary_runs.discard(run_id)
                    queued_publication = self._publication_events.get(run_id)
                run = self.state.transition_run(run_id, "cancelled", stop_reason="cancelled")
                cancelled_now = run.status == "cancelled"
                result = asdict(run)
        if cancelled_now:
            cancelled_tasks = self.state.cancel_tasks_for_run(run_id)
            cancelled_subagents = self.state.cancel_subagents_for_run(run_id)
            self.events.publish(
                run_id,
                "run.cancelled",
                {
                    "cancelled_task_ids": cancelled_tasks,
                    "cancelled_subagent_ids": cancelled_subagents,
                },
            )
        self._forget_approval_arguments_for_run(run_id)
        cancel_subprocesses_for_run(run_id)
        if queued_publication is not None:
            self._finish_publication(run_id, queued_publication)
        return result

    def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        arguments: dict[str, Any] | None = None,
        principal: str = "owner",
    ) -> dict[str, Any]:
        newly_expired = self.state.expire_pending_approvals(approval_id=approval_id)
        if newly_expired:
            self._finalize_expired_approvals(newly_expired)
            return newly_expired[0]
        approval = self.state.get_approval(approval_id, expire=False)
        if approval["status"] != "pending":
            return approval
        status = "approved" if approved else "denied"
        stored_arguments = dict(approval["arguments"])
        approved_arguments = self._resolve_approval_arguments(
            approval,
            supplied_arguments=arguments,
            approved=approved,
        )
        decision = {
            "approved": approved,
            "arguments": stored_arguments,
            "principal": principal,
        }
        updated, applied = self.state.decide_approval_once(
            approval_id,
            status=status,
            decision=decision,
            principal=principal,
        )
        if updated["status"] == "expired":
            self._finalize_expired_approvals([updated])
            return updated
        if not applied:
            return updated
        self.events.publish(updated["run_id"], f"approval.{status}", updated)
        if updated["status"] == "approved":
            try:
                self._reserve_primary_run(str(updated["run_id"]))
            except RunCapacityError:
                self._forget_approval_arguments(approval_id)
                failed = self.state.transition_run(
                    updated["run_id"],
                    "failed",
                    error="Approval resume capacity unavailable",
                    stop_reason="approval_resume_capacity",
                )
                if failed.status == "failed":
                    self.events.publish(
                        updated["run_id"],
                        "run.failed",
                        {"error": "Approval resume capacity unavailable"},
                    )
                return updated
            self._resume_after_approval(updated, approved_arguments)
            updated = self.state.get_approval(approval_id)
        else:
            self._forget_approval_arguments(approval_id)
            self.state.transition_run(updated["run_id"], "failed", error="Approval denied", stop_reason="approval_denied")
            self.events.publish(updated["run_id"], "run.failed", {"error": "Approval denied"})
        return updated

    def revoke_pending_approvals_for_tools(
        self,
        tool_names: set[str],
        *,
        reason: str = "capability_disabled",
    ) -> int:
        """Deny pending grants before a newly disabled capability can resume."""

        revoked = 0
        for approval in self.state.list_approvals(status="pending"):
            if str(approval.get("tool_name")) not in tool_names:
                continue
            updated, applied = self.state.decide_approval_once(
                str(approval["approval_id"]),
                status="denied",
                decision={
                    "approved": False,
                    "arguments": dict(approval.get("arguments", {})),
                    "principal": str(approval.get("principal", "owner")),
                    "reason": reason,
                },
                principal=str(approval.get("principal", "owner")),
            )
            if not applied:
                continue
            revoked += 1
            self._forget_approval_arguments(str(updated["approval_id"]))
            self.events.publish(str(updated["run_id"]), "approval.revoked", updated)
            try:
                failed = self.state.transition_run(
                    str(updated["run_id"]),
                    "failed",
                    error="Capability disabled while approval was pending.",
                    stop_reason=reason,
                )
                if failed.status == "failed":
                    self.events.publish(
                        str(updated["run_id"]),
                        "run.failed",
                        {"error": "Capability disabled while approval was pending."},
                    )
            except KeyError:
                pass
        return revoked

    def tool_resource_digest(self, spec: ToolSpec) -> str:
        """Bind approvals to the live tool spec, parent resource, and policy."""

        decision = self.capabilities.tool_decision(spec)
        payload: dict[str, Any] = {
            "tool_spec": tool_spec_digest(spec),
            "tool_revision": decision.revision,
            "enablement_flag": decision.enablement_flag,
            "global_gate": (
                None
                if decision.enablement_flag is None
                else bool(getattr(self.config, decision.enablement_flag, False))
            ),
            "launch_allowlist": list(self.config.enabled_tools),
        }
        if spec.source == "mcp" and spec.server_id:
            row = self.state.get_mcp_server(spec.server_id)
            parent = self.capabilities.parent_decision(
                "mcp_server", spec.server_id, entity_enabled=bool(row.get("enabled", False))
            )
            payload["parent"] = {
                "kind": "mcp_server",
                "id": spec.server_id,
                "revision": parent.revision,
                "configured_enabled": parent.configured_enabled,
                "transport": row.get("transport"),
                "command": row.get("command"),
                "args": row.get("args", []),
                "env": row.get("env", {}),
                "secret_env": row.get("secret_env", {}),
                "url": row.get("url"),
                "risk_policy": row.get("risk_policy"),
                "tools": row.get("tools", []),
            }
        elif spec.source == "skill" and spec.skill_id:
            row = self.state.get_skill(spec.skill_id)
            parent = self.capabilities.parent_decision(
                "skill", spec.skill_id, entity_enabled=bool(row.get("enabled", False))
            )
            payload["parent"] = {
                "kind": "skill",
                "id": spec.skill_id,
                "revision": parent.revision,
                "configured_enabled": parent.configured_enabled,
                "manifest": row.get("manifest", {}),
                "path": row.get("path", ""),
            }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _finalize_expired_approvals(self, approvals: list[dict[str, Any]]) -> None:
        for approval in approvals:
            run_id = str(approval["run_id"])
            self._forget_approval_arguments(str(approval["approval_id"]))
            try:
                failed = self.state.transition_run(
                    run_id,
                    "failed",
                    expected_statuses=("blocked",),
                    expected_stop_reason="approval_required",
                    error="Exact-call approval expired before an owner decision.",
                    stop_reason="approval_expired",
                )
            except KeyError:
                continue
            if failed.status != "failed" or failed.stop_reason != "approval_expired":
                continue
            with self._lock:
                self._cancelled.add(run_id)
            self._release_primary_reservation(run_id)
            cancel_subprocesses_for_run(run_id)
            self.state.cancel_tasks_for_run(run_id)
            self.state.cancel_subagents_for_run(run_id)
            self.events.publish(run_id, "approval.expired", approval)
            self.events.publish(
                run_id,
                "run.failed",
                {"error": "Exact-call approval expired before an owner decision."},
            )

    def invoke_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str = "manual",
        run_id: str | None = None,
    ) -> ToolExecution:
        active_config = self.config
        if run_id:
            run = self.state.get_run(run_id)
            active_config = self._config_for_run(run)
        registry = self.build_registry(active_config)
        agent = build_agent(active_config, tools=registry, state=self.state, secret_resolver=self.secret_resolver)
        try:
            call = ToolCall(name=tool_name, arguments=arguments)
            spans = SpanRecorder(state=self.state, events=self.events)
            if run_id:
                self.events.publish(run_id, "tool.started", {"tool": tool_name, "tool_call_id": call.id})
            if run_id:
                with spans.start(
                    run_id=run_id,
                    span_type="tool.call",
                    name=tool_name,
                    metadata={"tool_call_id": call.id, "manual": True},
                ):
                    execution = registry.execute(
                        call,
                        ToolContext(
                            memory=agent.memory,
                            config=agent.config,
                            workspace=agent.config.workspace,
                            event_log=agent.event_log,
                            session_id=session_id,
                            run_id=run_id,
                            approval_handler=self._approval_handler if run_id else None,
                        ),
                    )
            else:
                execution = registry.execute(
                    call,
                    ToolContext(
                        memory=agent.memory,
                        config=agent.config,
                        workspace=agent.config.workspace,
                        event_log=agent.event_log,
                        session_id=session_id,
                        run_id=run_id,
                        approval_handler=self._approval_handler if run_id else None,
                    ),
                )
            execution = _sanitize_tool_execution(execution)
            if run_id:
                self.events.publish(run_id, "tool.executed", _execution_payload(execution))
                self.events.publish(
                    run_id,
                    "tool.completed" if execution.success else "tool.failed",
                    _execution_payload(execution),
                )
            return execution
        finally:
            agent.close()

    def task_graph(self, run_id: str) -> dict[str, Any]:
        self.state.get_run(run_id)
        return {
            "tasks": [_task_payload(task) for task in self.state.list_task_nodes(run_id)],
            "ready_tasks": self.ready_tasks(run_id),
            "approval_blocked_tasks": self.approval_blocked_tasks(run_id),
            "subagents": [asdict(subagent) for subagent in self.state.list_subagent_runs(run_id)],
        }

    def ready_tasks(self, run_id: str) -> list[dict[str, Any]]:
        self.state.get_run(run_id)
        tasks = self.state.list_task_nodes(run_id)
        by_id = {task.task_id: task for task in tasks}
        ready: list[dict[str, Any]] = []
        for task in tasks:
            reason = _task_scheduler_reason(task, by_id)
            if reason is None:
                continue
            payload = _task_payload(task)
            payload["scheduler_reason"] = reason
            ready.append(payload)
        return ready

    def approval_blocked_tasks(self, run_id: str) -> list[dict[str, Any]]:
        self.state.get_run(run_id)
        tasks = self.state.list_task_nodes(run_id)
        by_id = {task.task_id: task for task in tasks}
        blocked: list[dict[str, Any]] = []
        for task in tasks:
            if task.approved or task.status not in {"queued", "approved"}:
                continue
            if not _dependencies_completed(task, by_id):
                continue
            payload = _task_payload(task)
            payload["scheduler_reason"] = "task_approval_required"
            blocked.append(payload)
        return blocked

    def run_scheduler_step(self, run_id: str, *, max_tasks: int | None = None) -> dict[str, Any]:
        """Execute currently ready approved task nodes through normal agent gates."""
        run = self._require_run_accepts_work(run_id, operation="scheduler")
        run_config = self._config_for_run(run)
        limit = max(1, max_tasks or run_config.max_scheduler_tasks)
        executed: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        terminal_status: str | None = None

        while len(executed) < limit:
            current = self.state.get_run(run_id)
            if current.status in _TERMINAL_RUN_STATUSES:
                terminal_status = current.status
                break
            executable: TaskNodeRecord | None = None
            for task_payload in self.ready_tasks(run_id):
                task = self.state.get_task_node(str(task_payload["task_id"]))
                if _is_root_objective_task(task):
                    if not any(item["task_id"] == task.task_id for item in skipped):
                        skipped.append({"task_id": task.task_id, "reason": "root_objective_tracking_node"})
                    continue
                executable = task
                break
            if executable is None:
                break
            result = self._execute_ready_task(run, executable)
            if result["status"] == "skipped":
                skipped.append(result)
                continue
            executed.append(result)
            if result["status"] == "blocked":
                blocked.append(result)
                break

        self._maybe_complete_root_task(run_id)
        payload = {
            "run_id": run_id,
            "executed": executed,
            "blocked": blocked,
            "skipped": skipped,
            "remaining_ready_tasks": self.ready_tasks(run_id),
            "approval_blocked_tasks": self.approval_blocked_tasks(run_id),
            "terminal_status": terminal_status,
        }
        self.events.publish(run_id, "scheduler.step", payload)
        return payload

    def run_scheduler_until_idle(
        self,
        run_id: str,
        *,
        max_tasks: int | None = None,
        max_cycles: int | None = None,
    ) -> dict[str, Any]:
        """Drain scheduler-selected tasks until idle, blocked, failed, or bounded."""
        run = self._require_run_accepts_work(run_id, operation="scheduler")
        run_config = self._config_for_run(run)
        cycle_limit = max(1, max_cycles or run_config.max_scheduler_cycles)
        task_limit = max(1, max_tasks or run_config.max_scheduler_tasks)
        steps: list[dict[str, Any]] = []
        stop_reason = "idle"

        for _ in range(cycle_limit):
            current = self.state.get_run(run_id)
            if current.status in _TERMINAL_RUN_STATUSES:
                stop_reason = f"run_{current.status}"
                break
            if self.approval_blocked_tasks(run_id) and not self._executable_ready_tasks(run_id):
                stop_reason = "task_approval_required"
                break
            if not self._executable_ready_tasks(run_id):
                stop_reason = "idle"
                break

            step = self.run_scheduler_step(run_id, max_tasks=task_limit)
            steps.append(step)
            executed_statuses = {str(item.get("status")) for item in step["executed"]}
            if "failed" in executed_statuses:
                stop_reason = "task_failed"
                break
            if step["blocked"]:
                stop_reason = "tool_approval_required"
                break
            if step["approval_blocked_tasks"] and not self._executable_ready_tasks(run_id):
                stop_reason = "task_approval_required"
                break
            if not step["executed"]:
                stop_reason = "idle"
                break
        else:
            stop_reason = "cycle_limit_reached"

        payload = {
            "run_id": run_id,
            "cycles": len(steps),
            "max_cycles": cycle_limit,
            "max_tasks_per_cycle": task_limit,
            "stop_reason": stop_reason,
            "steps": steps,
            "executed": [item for step in steps for item in step["executed"]],
            "blocked": [item for step in steps for item in step["blocked"]],
            "remaining_ready_tasks": self.ready_tasks(run_id),
            "approval_blocked_tasks": self.approval_blocked_tasks(run_id),
        }
        self.events.publish(run_id, "scheduler.run", payload)
        return payload

    def _executable_ready_tasks(self, run_id: str) -> list[dict[str, Any]]:
        executable: list[dict[str, Any]] = []
        for task_payload in self.ready_tasks(run_id):
            task = self.state.get_task_node(str(task_payload["task_id"]))
            if not _is_root_objective_task(task):
                executable.append(task_payload)
        return executable

    def approve_task(self, run_id: str, task_id: str) -> dict[str, Any]:
        self._require_run_accepts_work(run_id, operation="task_approval")
        existing = self.state.get_task_node(task_id)
        if existing.run_id != run_id:
            raise ValueError("task_does_not_belong_to_run")
        task = self.state.approve_task_node(task_id, run_id=run_id)
        if task is None:
            raise ValueError(f"task_not_approvable:{existing.status}")
        self.events.publish(run_id, "task.approved", asdict(task))
        payload = asdict(task)
        if self._run_uses_autonomous_scheduler(run_id):
            self.state.transition_run(run_id, "running", stop_reason="task_approved")
            scheduler = self.run_scheduler_until_idle(run_id)
            final_status, stop_reason = _scheduler_run_outcome(scheduler)
            self.state.transition_run(run_id, final_status, stop_reason=stop_reason)
            self.events.publish(run_id, f"run.{final_status}", {"scheduler": scheduler})
            payload["scheduler"] = scheduler
        return payload

    def create_subagent(self, *, run_id: str, profile: str, goal: str, task_id: str | None = None) -> dict[str, Any]:
        run = self._require_run_accepts_work(run_id, operation="subagent_creation")
        profile = profile if profile in {"planner", "worker", "reviewer"} else "worker"
        created_task = False
        if task_id is None:
            task = self.state.create_task_node(
                task_id=f"task_{uuid4().hex}",
                run_id=run_id,
                title=f"{profile.title()} subtask",
                goal=goal,
                profile=profile,
                status="queued",
                approved=True,
            )
            task_id = task.task_id
            created_task = True
        task_record = self.state.get_task_node(task_id)
        if task_record.run_id != run_id:
            raise ValueError("task_does_not_belong_to_run")
        subagent_id = f"subagent_{uuid4().hex}"
        claimed = self.state.claim_task_node(
            task_id,
            run_id=run_id,
            worker_owner=self._lease_owner,
            worker_claim_id=subagent_id,
        )
        if claimed is None:
            if created_task and self.state.get_run(run_id).status in _TERMINAL_RUN_STATUSES:
                self.state.update_task_node(task_id, status="cancelled")
            raise ValueError(f"task_not_claimable:{task_record.status}")
        try:
            subagent = self.state.create_subagent_run_for_claim(
                subagent_id=subagent_id,
                run_id=run_id,
                task_id=task_id,
                profile=profile,
                goal=goal,
                status="queued",
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
            )
        except Exception as exc:
            error_text = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
            self.state.transition_task_claim(
                task_id,
                "failed",
                run_id=run_id,
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
                increment_attempt=True,
                failure_reason=error_text,
                result={"error": error_text},
            )
            raise
        if subagent is None:
            current = self.state.get_run(run_id)
            if current.status == "completed":
                self.state.transition_task_claim(
                    task_id,
                    "cancelled",
                    run_id=run_id,
                    worker_owner=self._lease_owner,
                    worker_claim_id=subagent_id,
                )
            raise ValueError(f"subagent_creation_fence_lost:{current.status}")
        self.events.publish(run_id, "subagent.queued", asdict(subagent))
        config = self._config_for_run(run)
        try:
            self._start_thread(
                subagent.subagent_id,
                self._run_subagent,
                config,
                subagent.subagent_id,
                run_id,
                run.session_id,
            )
        except Exception as exc:
            error_text = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
            self.state.transition_task_claim(
                task_id,
                "failed",
                run_id=run_id,
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
                increment_attempt=True,
                failure_reason=error_text,
                result={"error": error_text},
            )
            self.state.transition_subagent_run(
                subagent_id,
                "failed",
                expected_statuses=("queued",),
                error=error_text,
            )
            raise
        return asdict(subagent)

    def _run_agent_turn(self, run_id: str, config: AgentConfig, message: str, session_id: str) -> None:
        if self._is_cancelled(run_id):
            return
        with self._run_lease(run_id, config) as lease:
            if lease is None:
                return
            run = self.state.get_run(run_id)

            def transition(active_run_id: str, status: str, **fields: Any) -> RunRecord:
                return self.state.transition_run(
                    active_run_id,
                    status,
                    lease_owner=self._lease_owner,
                    lease_generation=lease.lease_generation,
                    **fields,
                )

            def cancelled(active_run_id: str) -> bool:
                return self._is_cancelled(active_run_id) or not self.state.run_lease_matches(
                    active_run_id,
                    owner=self._lease_owner,
                    generation=lease.lease_generation,
                )

            services = GraphRuntimeServices(
                state=self.state,
                transition_run=transition,
                events=self.events,
                spans=SpanRecorder(state=self.state, events=self.events),
                build_agent=self._build_agent,
                approval_handler=self._approval_handler,
                stream_handler_factory=self._stream_handler,
                progress_handler_factory=self._progress_handler,
                publish_turn_observability=self._publish_turn_observability,
                publish_tool_executions=self._publish_tool_execution_events,
                complete_capsule=self._complete_capsule,
                run_scheduler_until_idle=lambda active_run_id, max_tasks, max_cycles: self.run_scheduler_until_idle(
                    active_run_id,
                    max_tasks=max_tasks,
                    max_cycles=max_cycles,
                ),
                scheduler_outcome=_scheduler_run_outcome,
                is_cancelled=cancelled,
            )
            try:
                DurableOrchestrationRuntime(services).run_chat_turn(run=run, config=config, message=message)
            except Exception as exc:  # noqa: BLE001
                if cancelled(run_id):
                    return
                error_text = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
                failed = transition(run_id, "failed", error=error_text, stop_reason="error")
                if failed.status == "failed":
                    self.events.publish(run_id, "run.failed", {"error": error_text})

    def _resume_after_approval(self, approval: dict[str, Any], arguments: dict[str, Any]) -> None:
        run_id = str(approval["run_id"])
        if self._is_cancelled(run_id):
            self._forget_approval_arguments(str(approval["approval_id"]))
            self._release_primary_reservation(run_id)
            return
        current_approval = self._validated_approval_continuation(approval, arguments)
        if current_approval is None:
            self._forget_approval_arguments(str(approval["approval_id"]))
            failed = self.state.transition_run(
                run_id,
                "failed",
                expected_statuses=("blocked", "queued"),
                error="Approval was no longer valid before continuation.",
                stop_reason="approval_invalid_before_continuation",
            )
            if failed.status == "failed":
                self.events.publish(
                    run_id,
                    "run.failed",
                    {"error": "Approval was no longer valid before continuation."},
                )
            self._release_primary_reservation(run_id)
            return
        approval = current_approval
        run = self.state.get_run(run_id)
        config = self._config_for_run(run)
        if run.status in _TERMINAL_RUN_STATUSES:
            self._release_primary_reservation(run_id)
            if run.status == "completed":
                self._run_approved_tool_for_terminal_run(config, approval, arguments, run.session_id)
            else:
                self._forget_approval_arguments(str(approval["approval_id"]))
            return
        queued = (
            run
            if run.status == "running"
            else self.state.transition_run(
                run_id,
                "queued",
                expected_statuses=("blocked", "queued"),
                stop_reason="queued_after_approval",
            )
        )
        if queued.status not in {"queued", "running"}:
            self._forget_approval_arguments(str(approval["approval_id"]))
            self._release_primary_reservation(run_id)
            return
        try:
            self._schedule_primary_run(
                run_id,
                self._run_approved_tool_then_continue,
                config,
                approval,
                arguments,
                run.session_id,
            )
        except Exception:
            self._forget_approval_arguments(str(approval["approval_id"]))
            self._release_primary_reservation(run_id)
            raise

    def _run_approved_tool_for_terminal_run(
        self,
        config: AgentConfig,
        approval: dict[str, Any],
        arguments: dict[str, Any],
        session_id: str,
    ) -> None:
        """Execute one atomically claimed manual approval after a terminal run."""
        run_id = str(approval["run_id"])
        current_approval = self._validated_approval_continuation(approval, arguments)
        if current_approval is None:
            self._forget_approval_arguments(str(approval["approval_id"]))
            return
        approval = current_approval
        agent: NestedMV2Agent | None = None
        try:
            agent = self._build_agent(config)
            self._execute_approved_tool(agent, approval, arguments, session_id)
        except Exception as exc:  # noqa: BLE001
            error_text = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
            payload = {
                "tool": str(approval["tool_name"]),
                "tool_call_id": str(approval["tool_call_id"]),
                "arguments": _approval_storage_arguments(arguments),
                "success": False,
                "content": error_text,
                "data": {},
                "error": "approved_tool_failed",
            }
            self.state.record_approval_result(str(approval["approval_id"]), payload)
            self.events.publish(run_id, "tool.failed", payload)
        finally:
            if agent is not None:
                agent.close()
            self._forget_approval_arguments(str(approval["approval_id"]))

    def _run_approved_tool_then_continue(
        self,
        run_id: str,
        config: AgentConfig,
        approval: dict[str, Any],
        arguments: dict[str, Any],
        session_id: str,
    ) -> None:
        with self._run_lease(run_id, config) as lease:
            if lease is None:
                self._forget_approval_arguments(str(approval["approval_id"]))
                return
            running = self.state.transition_run(
                run_id,
                "running",
                stop_reason="resuming_after_approval",
                lease_owner=lease.lease_owner,
                lease_generation=lease.lease_generation,
            )
            if running.status != "running":
                self._forget_approval_arguments(str(approval["approval_id"]))
                return
            agent: NestedMV2Agent | None = None
            try:
                agent = self._build_agent(config)
                if self._is_cancelled(run_id):
                    return
                current_approval = self._validated_approval_continuation(
                    approval,
                    arguments,
                )
                if current_approval is None:
                    failed = self.state.transition_run(
                        run_id,
                        "failed",
                        lease_owner=self._lease_owner,
                        lease_generation=lease.lease_generation,
                        error="Approval was no longer valid before continuation.",
                        stop_reason="approval_invalid_before_continuation",
                    )
                    if failed.status == "failed":
                        self.events.publish(
                            run_id,
                            "run.failed",
                            {"error": "Approval was no longer valid before continuation."},
                        )
                    return
                approval = current_approval
                call, execution = self._execute_approved_tool(agent, approval, arguments, session_id)
                if self._is_cancelled(run_id) or not self.state.run_lease_matches(
                    run_id,
                    owner=self._lease_owner,
                    generation=lease.lease_generation,
                ):
                    return
                if self._validated_approval_continuation(approval, arguments) is None:
                    failed = self.state.transition_run(
                        run_id,
                        "failed",
                        lease_owner=self._lease_owner,
                        lease_generation=lease.lease_generation,
                        error="Approval was no longer valid before continuation.",
                        stop_reason="approval_invalid_before_continuation",
                    )
                    if failed.status == "failed":
                        self.events.publish(
                            run_id,
                            "run.failed",
                            {"error": "Approval was no longer valid before continuation."},
                        )
                    return
                continuation = (
                    f"Continue the previous run after approved tool `{call.name}`.\n\n"
                    f"Tool success: {execution.success}\n"
                    f"Tool result:\n{execution.content[:4000]}"
                )
                result = agent.chat(
                    continuation,
                    session_id=session_id,
                    run_id=run_id,
                    approval_handler=self._approval_handler,
                    stream_handler=self._stream_handler(run_id),
                    progress_handler=self._progress_handler(run_id),
                )
                if self._is_cancelled(run_id) or not self.state.run_lease_matches(
                    run_id,
                    owner=self._lease_owner,
                    generation=lease.lease_generation,
                ):
                    return
                self._publish_turn_observability(run_id, result)
                self._finish_agent_turn(
                    run_id,
                    config,
                    agent,
                    result,
                    tool_count_offset=1,
                    lease_generation=lease.lease_generation,
                )
            except Exception as exc:  # noqa: BLE001
                if self._is_cancelled(run_id):
                    return
                error_text = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
                failed = self.state.transition_run(
                    run_id,
                    "failed",
                    lease_owner=self._lease_owner,
                    lease_generation=lease.lease_generation,
                    error=error_text,
                    stop_reason="error",
                )
                if failed.status == "failed":
                    self.events.publish(run_id, "run.failed", {"error": error_text})
            finally:
                if agent is not None:
                    agent.close()
                self._forget_approval_arguments(str(approval["approval_id"]))

    def _validated_approval_continuation(
        self,
        approval: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Re-read and validate the durable exact-call grant before side effects."""

        try:
            current = self.state.get_approval(str(approval["approval_id"]), expire=False)
        except KeyError:
            return None
        decision = current.get("decision")
        if not isinstance(decision, dict) or decision.get("approved") is not True:
            return None
        expected_fields = (
            "approval_id",
            "run_id",
            "tool_call_id",
            "tool_name",
            "risk",
            "principal",
            "capability_revision",
            "resource_digest",
        )
        if current.get("status") != "approved":
            return None
        if any(current.get(field) != approval.get(field) for field in expected_fields):
            return None
        stored_arguments = _approval_storage_arguments(arguments)
        if current.get("arguments") != approval.get("arguments"):
            return None
        if current.get("arguments") != stored_arguments or decision.get("arguments") != stored_arguments:
            return None
        if not self._arguments_match_volatile_grant(
            str(approval["approval_id"]),
            arguments,
            stored_arguments=stored_arguments,
        ):
            return None
        if decision.get("principal") != current.get("principal"):
            return None
        registry = self.build_registry()
        spec = registry.spec_for(str(current["tool_name"]))
        if spec is None:
            return None
        capability = self.capabilities.tool_decision(spec)
        if not capability.effective_enabled:
            return None
        if int(current.get("capability_revision", 0)) != capability.revision:
            return None
        if str(current.get("resource_digest", "")) != self.tool_resource_digest(spec):
            return None
        return current

    def _execute_approved_tool(
        self,
        agent: NestedMV2Agent,
        approval: dict[str, Any],
        arguments: dict[str, Any],
        session_id: str,
    ) -> tuple[ToolCall, ToolExecution]:
        run_id = str(approval["run_id"])
        call = ToolCall(name=str(approval["tool_name"]), arguments=arguments, id=str(approval["tool_call_id"]))
        execution = agent.tools.execute(
            call,
            ToolContext(
                memory=agent.memory,
                config=agent.config,
                workspace=agent.config.workspace,
                event_log=agent.event_log,
                session_id=session_id,
                run_id=run_id,
                approved_tool_call_ids=frozenset({call.id}),
                approved_tool_call_arguments={call.id: arguments},
            ),
        )
        safe_execution = _sanitize_tool_execution(execution)
        payload = _execution_payload(safe_execution)
        self.state.record_approval_result(str(approval["approval_id"]), payload)
        self.events.publish(run_id, "tool.executed", payload)
        self.events.publish(run_id, "tool.completed" if execution.success else "tool.failed", payload)
        return call, safe_execution

    def _finish_agent_turn(
        self,
        run_id: str,
        config: AgentConfig,
        agent: NestedMV2Agent,
        result: AgentTurnResult,
        *,
        tool_count_offset: int = 0,
        lease_generation: int,
    ) -> None:
        status = "blocked" if result.stop_reason == "approval_required" else "completed"
        run_status = "running" if status == "completed" and config.enable_autonomous_scheduler else status
        stop_reason = "scheduler_running" if run_status == "running" and status == "completed" else result.stop_reason
        transitioned = self.state.transition_run(
            run_id,
            run_status,
            lease_owner=self._lease_owner,
            lease_generation=lease_generation,
            assistant_message=result.assistant_message,
            context_chars=result.context_chars,
            tool_count=len(result.tool_executions) + tool_count_offset,
            stop_reason=stop_reason,
        )
        if transitioned.status != run_status:
            return
        if status == "completed":
            self._complete_capsule(run_id, config, agent, result)
        event_type = "run.blocked" if status == "blocked" else "run.turn_completed" if config.enable_autonomous_scheduler else "run.completed"
        self.events.publish(run_id, event_type, _turn_payload(result))
        if status == "completed" and config.enable_autonomous_scheduler:
            scheduler = self.run_scheduler_until_idle(
                run_id,
                max_tasks=config.max_scheduler_tasks,
                max_cycles=config.max_scheduler_cycles,
            )
            final_status, scheduler_stop_reason = _scheduler_run_outcome(scheduler)
            finalized = self.state.transition_run(
                run_id,
                final_status,
                lease_owner=self._lease_owner,
                lease_generation=lease_generation,
                stop_reason=scheduler_stop_reason,
            )
            if finalized.status == final_status:
                self.events.publish(
                    run_id,
                    f"run.{final_status}",
                    {"scheduler": scheduler, "turn": _turn_payload(result)},
                )

    def _run_subagent(
        self,
        thread_key: str,
        config: AgentConfig,
        subagent_id: str,
        run_id: str,
        session_id: str,
    ) -> None:
        del thread_key
        subagent = self.state.get_subagent_run(subagent_id)
        task_id = subagent.task_id
        if task_id is None:
            self.state.transition_subagent_run(
                subagent_id,
                "failed",
                expected_statuses=("queued",),
                error="Subagent is missing its task claim",
            )
            return
        if not self.state.task_claim_matches(
            task_id,
            run_id=run_id,
            worker_owner=self._lease_owner,
            worker_claim_id=subagent_id,
        ):
            claimed = self.state.claim_task_node(
                task_id,
                run_id=run_id,
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
            )
            if claimed is None:
                return
        if self._is_cancelled(run_id) or self.state.get_run(run_id).status in _TERMINAL_RUN_STATUSES:
            self.state.transition_task_claim(
                task_id,
                "cancelled",
                run_id=run_id,
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
            )
            self.state.transition_subagent_run(
                subagent_id,
                "cancelled",
                expected_statuses=("queued", "running"),
            )
            return
        running, started = self.state.transition_subagent_run(
            subagent_id,
            "running",
            expected_statuses=("queued",),
        )
        if not started or not self.state.task_claim_matches(
            task_id,
            run_id=run_id,
            worker_owner=self._lease_owner,
            worker_claim_id=subagent_id,
        ):
            return
        self.events.publish(run_id, "subagent.started", asdict(running))
        worker_isolation: dict[str, str] | None = None
        agent: NestedMV2Agent | None = None
        worker_lost = Event()
        try:
            with self._worker_heartbeat(
                task_id,
                config,
                run_id=run_id,
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
            ) as worker_lost:
                if worker_lost.is_set():
                    raise RuntimeError("worker_execution_fence_lost")
                config, worker_isolation = self._worker_config(
                    config,
                    run_id=run_id,
                    worker_id=subagent_id,
                    task_id=task_id,
                )
                agent = self._build_agent(config)
                result = agent.chat(
                    _subagent_prompt(subagent.profile, subagent.goal),
                    session_id=session_id,
                    run_id=run_id,
                    approval_handler=self._approval_handler,
                    stream_handler=self._stream_handler(run_id),
                    progress_handler=self._progress_handler(
                        run_id,
                        cancellation_handler=worker_lost.is_set,
                    ),
                )
            if (
                worker_lost.is_set()
                or self._is_cancelled(run_id)
                or not self.state.task_claim_matches(
                    task_id,
                    run_id=run_id,
                    worker_owner=self._lease_owner,
                    worker_claim_id=subagent_id,
                )
            ):
                raise RuntimeError("worker_execution_fence_lost")
            self._publish_turn_observability(run_id, result)
            task = self.state.get_task_node(task_id)
            validation = _validate_task_completion(
                task,
                result,
                allow_mock_provider=config.provider == "mock",
            )
            if not validation["passed"]:
                codes = ",".join(str(code) for code in validation["failure_codes"])
                raise RuntimeError(f"subagent acceptance validation failed: {codes}")
            updated_task, task_applied = self.state.transition_task_claim(
                task_id,
                "completed",
                run_id=run_id,
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
                result={
                    "assistant_message": result.assistant_message,
                    "stop_reason": result.stop_reason,
                    "acceptance_validation": validation,
                    "worker_isolation": worker_isolation,
                },
            )
            if not task_applied:
                raise RuntimeError("worker_execution_fence_lost")
            updated, subagent_applied = self.state.transition_subagent_run(
                subagent_id,
                "completed",
                expected_statuses=("running",),
                result=result.assistant_message,
            )
            if subagent_applied:
                self.events.publish(run_id, "task.completed", _task_payload(updated_task))
                self.events.publish(run_id, "subagent.completed", asdict(updated))
        except Exception as exc:  # noqa: BLE001
            error_text = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
            cancelled = self._is_cancelled(run_id) or self.state.get_run(run_id).status == "cancelled"
            if cancelled:
                self.state.transition_task_claim(
                    task_id,
                    "cancelled",
                    run_id=run_id,
                    worker_owner=self._lease_owner,
                    worker_claim_id=subagent_id,
                )
                updated, applied = self.state.transition_subagent_run(
                    subagent_id,
                    "cancelled",
                    expected_statuses=("queued", "running"),
                    error=error_text,
                )
                if applied:
                    self.events.publish(run_id, "subagent.cancelled", asdict(updated))
            else:
                diagnosis = classify_failure(error_text, source="subagent")
                diagnosis_payload = diagnosis.to_payload()
                failed_task, task_applied = self.state.transition_task_claim(
                    task_id,
                    "failed",
                    run_id=run_id,
                    worker_owner=self._lease_owner,
                    worker_claim_id=subagent_id,
                    increment_attempt=True,
                    failure_reason=error_text,
                    diagnosis=diagnosis_payload,
                    retry_strategy={
                        "requires_changed_strategy": True,
                        "retry_allowed": False,
                        "reason": "subagent failure must be diagnosed and strategy must change before retry",
                    },
                    result={"error": error_text},
                )
                updated, subagent_applied = self.state.transition_subagent_run(
                    subagent_id,
                    "failed",
                    expected_statuses=("queued", "running"),
                    error=error_text,
                )
                if task_applied:
                    self.events.publish(run_id, "task.failed", _task_payload(failed_task))
                    self.events.publish(
                        run_id,
                        "diagnosis.classified",
                        {"task_id": task_id, "source": "subagent", **diagnosis_payload},
                    )
                if subagent_applied:
                    self.events.publish(run_id, "subagent.failed", asdict(updated))
        finally:
            if agent is not None:
                agent.close()

    def _execute_ready_task(self, run: RunRecord, task: TaskNodeRecord) -> dict[str, Any]:
        subagent_id = f"subagent_{uuid4().hex}"
        running = self.state.claim_task_node(
            task.task_id,
            run_id=run.run_id,
            worker_owner=self._lease_owner,
            worker_claim_id=subagent_id,
        )
        if running is None:
            current = self.state.get_task_node(task.task_id)
            return {
                "task_id": task.task_id,
                "status": "skipped",
                "reason": "task_claim_unavailable",
                "current_status": current.status,
            }
        try:
            subagent = self.state.create_subagent_run_for_claim(
                subagent_id=subagent_id,
                run_id=run.run_id,
                task_id=task.task_id,
                profile=task.profile,
                goal=task.goal,
                status="running",
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
            )
        except Exception as exc:
            error_text = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
            self.state.transition_task_claim(
                task.task_id,
                "failed",
                run_id=run.run_id,
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
                increment_attempt=True,
                failure_reason=error_text,
                result={"error": error_text},
            )
            raise
        if subagent is None:
            current_run = self.state.get_run(run.run_id)
            if current_run.status == "completed":
                self.state.transition_task_claim(
                    task.task_id,
                    "cancelled",
                    run_id=run.run_id,
                    worker_owner=self._lease_owner,
                    worker_claim_id=subagent_id,
                )
            return {
                "task_id": task.task_id,
                "status": "skipped",
                "reason": "subagent_creation_fence_lost",
                "current_status": current_run.status,
            }
        self.events.publish(run.run_id, "task.started", _task_payload(running))
        self.events.publish(run.run_id, "subagent.started", asdict(subagent))
        config = self._config_for_run(run)
        worker_isolation: dict[str, str] | None = None
        agent: NestedMV2Agent | None = None
        worker_lost = Event()
        try:
            with self._worker_heartbeat(
                task.task_id,
                config,
                run_id=run.run_id,
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
            ) as worker_lost:
                if worker_lost.is_set():
                    raise RuntimeError("worker_execution_fence_lost")
                config, worker_isolation = self._worker_config(
                    config,
                    run_id=run.run_id,
                    worker_id=subagent.subagent_id,
                    task_id=task.task_id,
                )
                agent = self._build_agent(config)
                result = agent.chat(
                    _task_execution_prompt(task),
                    session_id=run.session_id,
                    run_id=run.run_id,
                    approval_handler=self._approval_handler,
                    stream_handler=self._stream_handler(run.run_id),
                    progress_handler=self._progress_handler(
                        run.run_id,
                        cancellation_handler=worker_lost.is_set,
                    ),
                )
            if (
                worker_lost.is_set()
                or self._is_cancelled(run.run_id)
                or not self.state.task_claim_matches(
                    task.task_id,
                    run_id=run.run_id,
                    worker_owner=self._lease_owner,
                    worker_claim_id=subagent_id,
                )
            ):
                raise RuntimeError("worker_execution_fence_lost")
            self._publish_turn_observability(run.run_id, result)
            validation = _validate_task_completion(
                task,
                result,
                allow_mock_provider=config.provider == "mock",
            )
            status = "blocked" if result.stop_reason == "approval_required" else (
                "completed" if validation["passed"] else "failed"
            )
            task_result = {
                "assistant_message": result.assistant_message,
                "stop_reason": result.stop_reason,
                "context_chars": result.context_chars,
                "tool_count": len(result.tool_executions),
                "memory_writes": list(result.memory_writes),
                "acceptance_validation": validation,
                "worker_isolation": worker_isolation,
            }
            failure_reason: str | None = None
            if status == "failed":
                failure_reason = "Task acceptance validation failed: " + ",".join(
                    str(code) for code in validation["failure_codes"]
                )
                diagnosis = classify_failure(failure_reason, source="scheduler_validation").to_payload()
                updated_task, task_applied = self.state.transition_task_claim(
                    task.task_id,
                    "failed",
                    run_id=run.run_id,
                    worker_owner=self._lease_owner,
                    worker_claim_id=subagent_id,
                    increment_attempt=True,
                    failure_reason=failure_reason,
                    diagnosis=diagnosis,
                    retry_strategy={
                        "requires_changed_strategy": True,
                        "retry_allowed": False,
                        "reason": "acceptance validation failed",
                    },
                    result=task_result,
                )
            else:
                updated_task, task_applied = self.state.transition_task_claim(
                    task.task_id,
                    status,
                    run_id=run.run_id,
                    worker_owner=self._lease_owner,
                    worker_claim_id=subagent_id,
                    result=task_result,
                )
            if not task_applied:
                return {
                    "task_id": task.task_id,
                    "subagent_id": subagent.subagent_id,
                    "status": "skipped",
                    "reason": "task_execution_fence_lost",
                    "current_status": updated_task.status,
                }
            updated_subagent, subagent_applied = self.state.transition_subagent_run(
                subagent.subagent_id,
                status,
                expected_statuses=("running",),
                result=result.assistant_message,
                error=failure_reason,
            )
            for execution in result.tool_executions:
                self.events.publish(run.run_id, "tool.executed", _execution_payload(execution))
                self.events.publish(
                    run.run_id,
                    "tool.completed" if execution.success else "tool.failed",
                    _execution_payload(execution),
                )
            event_type = {
                "blocked": "task.blocked",
                "failed": "task.failed",
            }.get(status, "task.completed")
            self.events.publish(run.run_id, event_type, _task_payload(updated_task))
            if subagent_applied:
                self.events.publish(
                    run.run_id,
                    {
                        "blocked": "subagent.blocked",
                        "failed": "subagent.failed",
                    }.get(status, "subagent.completed"),
                    asdict(updated_subagent),
                )
            return {
                "task_id": task.task_id,
                "subagent_id": subagent.subagent_id,
                "status": status,
                "result": task_result,
                "worker_isolation": worker_isolation,
            }
        except Exception as exc:  # noqa: BLE001
            error_text = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
            cancelled = self._is_cancelled(run.run_id) or self.state.get_run(run.run_id).status == "cancelled"
            if cancelled:
                cancelled_task, task_applied = self.state.transition_task_claim(
                    task.task_id,
                    "cancelled",
                    run_id=run.run_id,
                    worker_owner=self._lease_owner,
                    worker_claim_id=subagent_id,
                    result={"error": error_text},
                )
                cancelled_subagent, subagent_applied = self.state.transition_subagent_run(
                    subagent.subagent_id,
                    "cancelled",
                    expected_statuses=("queued", "running", "blocked"),
                    error=error_text,
                )
                if task_applied:
                    self.events.publish(run.run_id, "task.cancelled", _task_payload(cancelled_task))
                if subagent_applied:
                    self.events.publish(run.run_id, "subagent.cancelled", asdict(cancelled_subagent))
                return {
                    "task_id": task.task_id,
                    "subagent_id": subagent.subagent_id,
                    "status": "cancelled",
                    "error": error_text,
                }
            diagnosis = classify_failure(error_text, source="scheduler").to_payload()
            failed_task, task_applied = self.state.transition_task_claim(
                task.task_id,
                "failed",
                run_id=run.run_id,
                worker_owner=self._lease_owner,
                worker_claim_id=subagent_id,
                increment_attempt=True,
                failure_reason=error_text,
                diagnosis=diagnosis,
                retry_strategy={
                    "requires_changed_strategy": True,
                    "retry_allowed": False,
                    "reason": "scheduler task failed; inspect diagnosis before retry",
                },
                result={"error": error_text},
            )
            failed_subagent, subagent_applied = self.state.transition_subagent_run(
                subagent.subagent_id,
                "failed",
                expected_statuses=("queued", "running", "blocked"),
                error=error_text,
            )
            if task_applied:
                self.events.publish(run.run_id, "task.failed", _task_payload(failed_task))
                self.events.publish(
                    run.run_id,
                    "diagnosis.classified",
                    {"task_id": task.task_id, "source": "scheduler", **diagnosis},
                )
            if subagent_applied:
                self.events.publish(run.run_id, "subagent.failed", asdict(failed_subagent))
            return {
                "task_id": task.task_id,
                "subagent_id": subagent.subagent_id,
                "status": "failed" if task_applied or failed_task.status == "failed" else "skipped",
                "error": error_text,
            }
        finally:
            if agent is not None:
                agent.close()

    def _worker_config(
        self,
        config: AgentConfig,
        *,
        run_id: str,
        worker_id: str,
        task_id: str | None = None,
    ) -> tuple[AgentConfig, dict[str, str] | None]:
        run = self.state.get_run(run_id)
        run_workspace = Path(run.workspace)
        task = self.state.get_task_node(task_id) if task_id else None
        should_isolate = config.enable_worker_isolation or (
            task is not None and _task_requires_default_worker_isolation(task) and _workspace_supports_git_worktree(run_workspace)
        )
        if not should_isolate:
            return config, None
        worktree_root = config.worker_worktree_dir
        if not worktree_root.is_absolute():
            worktree_root = run_workspace / worktree_root
        isolation_worker_id = "repair" if task is not None and _task_requires_default_worker_isolation(task) else worker_id
        isolation = prepare_git_worktree(
            workspace=run_workspace,
            worktree_root=worktree_root,
            branch_prefix=config.worker_branch_prefix,
            run_id=run_id,
            worker_id=isolation_worker_id,
        )
        payload = isolation.to_payload()
        self.events.publish(run_id, "worker.isolated", payload)
        return replace(config, workspace=isolation.workspace), payload

    def _maybe_complete_root_task(self, run_id: str) -> None:
        tasks = self.state.list_task_nodes(run_id)
        roots = [task for task in tasks if _is_root_objective_task(task)]
        if not roots:
            return
        root = roots[0]
        children = [task for task in tasks if task.parent_id == root.task_id]
        if not children:
            return
        child_statuses = {task.status for task in children}
        if any(status == "failed" for status in child_statuses):
            updated = self.state.update_task_node(root.task_id, status="failed", result={"child_statuses": sorted(child_statuses)})
            self.events.publish(run_id, "task.failed", _task_payload(updated))
        elif any(status == "blocked" for status in child_statuses):
            updated = self.state.update_task_node(root.task_id, status="blocked", result={"child_statuses": sorted(child_statuses)})
            self.events.publish(run_id, "task.blocked", _task_payload(updated))
        elif all(status == "completed" for status in child_statuses):
            updated = self.state.update_task_node(root.task_id, status="completed", result={"child_statuses": sorted(child_statuses)})
            self.events.publish(run_id, "task.completed", _task_payload(updated))

    def _approval_handler(self, call: ToolCall, spec: ToolSpec, context: ToolContext) -> ToolExecution:
        run_id = context.run_id or f"manual_{uuid4().hex}"
        approval_id = f"approval_{uuid4().hex}"
        raw_arguments = deepcopy(call.arguments)
        stored_arguments = _approval_storage_arguments(raw_arguments)
        capability = self.capabilities.tool_decision(spec)
        resource_digest = self.tool_resource_digest(spec)
        with self._approval_lock:
            try:
                approval, created = self.state.create_approval_once(
                    approval_id=approval_id,
                    run_id=run_id,
                    tool_call_id=call.id,
                    tool_name=spec.name,
                    arguments=stored_arguments,
                    risk=spec.risk,
                    expires_at=(
                        datetime.now(UTC)
                        + timedelta(seconds=max(1.0, context.config.approval_ttl_seconds))
                    ).isoformat(),
                    principal="owner",
                    capability_revision=capability.revision,
                    resource_digest=resource_digest,
                )
            except ApprovalConflictError as exc:
                approval = exc.approval
                created = False
            durable_identity_matches = (
                approval["tool_call_id"] == call.id
                and approval["tool_name"] == spec.name
                and approval["arguments"] == stored_arguments
                and approval["risk"] == spec.risk
                and approval["capability_revision"] == capability.revision
                and approval["resource_digest"] == resource_digest
            )
            cached = self._approval_call_arguments.get(str(approval["approval_id"]))
            exact_call_matches = durable_identity_matches and (
                created
                or (cached is not None and cached == (run_id, raw_arguments))
                or (cached is None and not _contains_redaction_marker(stored_arguments))
            )
            if created:
                stale_ids = [
                    cached_approval_id
                    for cached_approval_id, (cached_run_id, _arguments) in self._approval_call_arguments.items()
                    if cached_run_id == run_id
                ]
                for cached_approval_id in stale_ids:
                    self._approval_call_arguments.pop(cached_approval_id, None)
                self._approval_call_arguments[str(approval["approval_id"])] = (
                    run_id,
                    raw_arguments,
                )
        if created:
            self.events.publish(run_id, "approval.requested", approval)
        safe_call = replace(call, name=spec.name, arguments=stored_arguments)
        return ToolExecution(
            call=safe_call,
            success=False,
            content=(
                f"Approval required for {call.name}."
                if exact_call_matches
                else "This run is already waiting for another exact-call approval."
            ),
            data={"approval_id": approval["approval_id"], "status": "pending"},
            error="approval_pending",
        )

    def _resolve_approval_arguments(
        self,
        approval: dict[str, Any],
        *,
        supplied_arguments: dict[str, Any] | None,
        approved: bool,
    ) -> dict[str, Any]:
        """Resolve exact arguments without using durable redaction placeholders."""

        stored_arguments = dict(approval["arguments"])
        if not approved:
            if supplied_arguments is not None:
                supplied_safe = _approval_storage_arguments(supplied_arguments)
                if supplied_arguments != stored_arguments and supplied_safe != stored_arguments:
                    raise ValueError("Approval decisions must match the exact requested arguments.")
            return stored_arguments
        with self._approval_lock:
            cached = self._approval_call_arguments.get(str(approval["approval_id"]))
            raw_arguments = deepcopy(cached[1]) if cached is not None else None
        if raw_arguments is not None:
            if supplied_arguments is not None and supplied_arguments not in (
                raw_arguments,
                stored_arguments,
            ):
                raise ValueError("Approval decisions must match the exact requested arguments.")
            return raw_arguments
        if _contains_redaction_marker(stored_arguments):
            raise ValueError(
                "Exact raw approval arguments are unavailable after restart; "
                "cancel this approval and request the tool call again."
            )
        if supplied_arguments is not None and supplied_arguments != stored_arguments:
            raise ValueError("Approval decisions must match the exact requested arguments.")
        return stored_arguments

    def _arguments_match_volatile_grant(
        self,
        approval_id: str,
        arguments: dict[str, Any],
        *,
        stored_arguments: dict[str, Any],
    ) -> bool:
        with self._approval_lock:
            cached = self._approval_call_arguments.get(approval_id)
            if cached is not None:
                return cached[1] == arguments
        return not _contains_redaction_marker(stored_arguments) and stored_arguments == arguments

    def _forget_approval_arguments(self, approval_id: str) -> None:
        with self._approval_lock:
            self._approval_call_arguments.pop(approval_id, None)

    def _forget_approval_arguments_for_run(self, run_id: str) -> None:
        with self._approval_lock:
            expired = [
                approval_id
                for approval_id, (cached_run_id, _arguments) in self._approval_call_arguments.items()
                if cached_run_id == run_id
            ]
            for approval_id in expired:
                self._approval_call_arguments.pop(approval_id, None)

    def _build_agent(self, config: AgentConfig) -> NestedMV2Agent:
        return build_agent(
            config,
            tools=self.build_registry(config),
            state=self.state,
            secret_resolver=self.secret_resolver,
        )

    def _config_for_run(self, run: RunRecord) -> AgentConfig:
        base = self.config
        if run.config_snapshot:
            effective = run.config_snapshot.get("effective_config")
            if isinstance(effective, dict):
                if run.config_snapshot.get("effective_config_schema_version") != 1:
                    raise ValueError("unsupported effective run configuration schema")
                base = AgentConfig.from_mapping(effective)
            else:
                base = apply_runtime_settings(
                    base,
                    RuntimeSettings.from_mapping(run.config_snapshot, base),
                )
        return replace(
            base,
            workspace=Path(run.workspace),
            provider=run.provider,
            model=run.model,
        )

    def _run_uses_autonomous_scheduler(self, run_id: str) -> bool:
        run = self.state.get_run(run_id)
        if self._config_for_run(run).enable_autonomous_scheduler:
            return True
        return any(
            _is_root_objective_task(task) and (task.plan or {}).get("autonomy_mode") == "autonomous"
            for task in self.state.list_task_nodes(run_id)
        )

    def _stream_handler(self, run_id: str) -> Callable[[LLMStreamEvent], None]:
        def handle(event: LLMStreamEvent) -> None:
            if event.type == "token":
                self.events.publish(run_id, "assistant.token", {"content": event.content})
            elif event.type == "tool_call" and event.tool_call is not None:
                self.events.publish(
                    run_id,
                    "assistant.tool_call",
                    {"tool": event.tool_call.name, "tool_call_id": event.tool_call.id, "arguments": event.tool_call.arguments},
                )
            elif event.type == "usage":
                self.events.publish(run_id, "assistant.usage", event.data)
            elif event.type == "provider_error":
                self.events.publish(run_id, "assistant.provider_error", {"content": event.content, **event.data})

        return handle

    def _progress_handler(
        self,
        run_id: str,
        *,
        cancellation_handler: Callable[[], bool] | None = None,
    ) -> Callable[[str, dict[str, Any]], None]:
        def handle(event_type: str, payload: dict[str, Any]) -> None:
            if event_type != "tool.request":
                return
            if self._is_cancelled(run_id) or (
                cancellation_handler is not None and cancellation_handler()
            ):
                raise RuntimeError("execution_fence_lost")
            self.events.publish(
                run_id,
                "tool.started",
                {
                    "tool": str(payload.get("tool") or payload.get("tool_name") or "tool"),
                    "tool_call_id": str(payload.get("tool_call_id") or ""),
                    "arguments": payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
                },
            )

        return handle

    def build_registry(self, config: AgentConfig | None = None) -> ToolRegistry:
        active_config = config or self.config
        registry = build_default_tools(active_config.enabled_tools)
        for adapter in [
            *self.mcp.tool_adapters(include_disabled=True),
            *self.skills.tool_adapters(include_disabled=True),
        ]:
            registry.register(adapter)
        registry.set_capability_gate(self._capability_gate)
        return registry

    def _capability_gate(self, spec: ToolSpec) -> tuple[bool, str]:
        decision = self.capabilities.tool_decision(spec)
        if decision.effective_enabled:
            return True, ""
        blockers = ", ".join(decision.blocked_by) or "capability policy"
        return False, f"Tool {spec.name} is disabled by {blockers}."

    def _complete_capsule(
        self,
        run_id: str,
        config: AgentConfig,
        agent: NestedMV2Agent,
        result: AgentTurnResult,
    ) -> None:
        if not config.enable_task_capsules:
            return
        runs_dir = config.memory_dir.parent / "runs"
        try:
            capsule_path = write_turn_capsule(
                runs_dir=runs_dir,
                run_id=run_id,
                result=result,
                backend=config.backend,
                selected_context=result.context_prompt,
            )
            summary = summarize_run_capsule(runs_dir=runs_dir, run_id=run_id, backend=config.backend)
            decisions = _capsule_decisions(
                summary,
                agent=agent,
                dry_run=config.auto_consolidation_dry_run or not config.enable_auto_consolidation,
            )
            self.events.publish(
                run_id,
                "capsule.completed",
                {
                    "capsule_path": str(capsule_path),
                    "summary": summary.to_payload(),
                    "auto_consolidation_enabled": config.enable_auto_consolidation,
                    "dry_run": config.auto_consolidation_dry_run or not config.enable_auto_consolidation,
                    "decisions": decisions,
                },
            )
            if config.enable_auto_compact:
                dry_run = not config.auto_compact_apply
                reports = [
                    RetentionCompactor(agent.memory).compact_layer(layer, dry_run=dry_run)
                    for layer in (MemoryLayer.WORKING, MemoryLayer.EPISODIC)
                ]
                if not dry_run:
                    agent.memory.seal_all()
                self.events.publish(
                    run_id,
                    "memory.compact",
                    {
                        "enabled": True,
                        "dry_run": dry_run,
                        "reports": reports,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            self.events.publish(run_id, "capsule.failed", {"error": f"{type(exc).__name__}: {exc}"})

    def _publish_turn_observability(self, run_id: str, result: AgentTurnResult) -> None:
        self.events.publish(
            run_id,
            "context.compile",
            {
                "session_id": result.session_id,
                "context_chars": result.context_chars,
                "stop_reason": result.stop_reason,
            },
        )
        for index, record_id in enumerate(result.memory_writes, start=1):
            self.events.publish(
                run_id,
                "memory.write",
                {
                    "session_id": result.session_id,
                    "record_id": record_id,
                    "index": index,
                    "total": len(result.memory_writes),
                },
            )
        if result.error:
            self.events.publish(run_id, "runtime.error", result.error)

    def _publish_tool_execution_events(self, run_id: str, executions: tuple[ToolExecution, ...]) -> None:
        spans = SpanRecorder(state=self.state, events=self.events)
        for execution in executions:
            with spans.start(
                run_id=run_id,
                span_type="tool.call",
                name=execution.call.name,
                metadata={"tool_call_id": execution.call.id},
            ):
                self.events.publish(run_id, "tool.executed", _execution_payload(execution))
                self.events.publish(
                    run_id,
                    "tool.completed" if execution.success else "tool.failed",
                    _execution_payload(execution),
                )

    def _abort_primary_admission(self, run_id: str, error: Exception) -> None:
        publication: Event | None = None
        with self._lock:
            self._reserved_primary_runs.discard(run_id)
            self._active_primary_runs.discard(run_id)
            self._threads.pop(run_id, None)
            publication = self._publication_events.get(run_id)
            self._queued_primary_runs = deque(
                queued for queued in self._queued_primary_runs if queued[0] != run_id
            )
        try:
            try:
                self.state.transition_run(
                    run_id,
                    "failed",
                    stop_reason="admission_setup_failed",
                    error=f"Admission setup failed: {type(error).__name__}",
                    recovery_reason="admission_setup_failed",
                )
            except KeyError:
                return
            try:
                self.events.publish(
                    run_id,
                    "run.admission_failed",
                    {"error_type": type(error).__name__},
                )
            except Exception:
                pass
        finally:
            if publication is not None:
                self._finish_publication(run_id, publication)

    def _reserve_primary_run(self, run_id: str) -> None:
        with self._lock:
            capacity = max(1, self.config.max_concurrent_runs) + max(0, self.config.max_queued_runs)
            admitted = (
                len(self._active_primary_runs)
                + len(self._queued_primary_runs)
                + len(self._reserved_primary_runs)
            )
            if admitted >= capacity:
                self._admission_rejections += 1
                raise RunCapacityError("run_capacity_exhausted")
            self._reserved_primary_runs.add(run_id)

    def _release_primary_reservation(self, run_id: str) -> None:
        with self._lock:
            self._reserved_primary_runs.discard(run_id)

    def capacity_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "active": len(self._active_primary_runs),
                "queued": len(self._queued_primary_runs),
                "reserved": len(self._reserved_primary_runs),
                "max_active": max(1, self.config.max_concurrent_runs),
                "max_queued": max(0, self.config.max_queued_runs),
            }

    def operational_counters(self) -> dict[str, int]:
        with self._lock:
            return {
                "admission_rejections": self._admission_rejections,
                "startup_recovered_failed": len(self.startup_recovery.get("failed", [])),
                "startup_recovered_preserved": len(self.startup_recovery.get("preserved", [])),
                "startup_workers_failed": len(self.startup_worker_recovery.get("failed", [])),
                "startup_workers_preserved": len(self.startup_worker_recovery.get("preserved", [])),
            }

    def _create_admitted_run(self, **fields: Any) -> RunRecord:
        try:
            return self.state.create_run(**fields)
        except Exception:
            run_id = str(fields.get("run_id", ""))
            with self._lock:
                self._reserved_primary_runs.discard(run_id)
            raise

    def _schedule_primary_run(self, run_id: str, target: Any, *args: Any) -> None:
        thread: Thread | None = None
        queued = False
        publication = Event()
        with self._lock:
            self._reserved_primary_runs.discard(run_id)
            self._publication_events[run_id] = publication
            if len(self._active_primary_runs) < max(1, self.config.max_concurrent_runs):
                self._active_primary_runs.add(run_id)
                thread = Thread(
                    target=self._run_primary_thread,
                    args=(run_id, target, args, publication),
                    daemon=True,
                )
                self._threads[run_id] = thread
            else:
                self._queued_primary_runs.append((run_id, target, args, publication))
                queued = True
        if queued:
            self.events.publish(run_id, "run.queued_for_capacity", {"run_id": run_id})
        elif thread is not None:
            thread.start()

    def _run_primary_thread(
        self,
        run_id: str,
        target: Any,
        args: tuple[Any, ...],
        publication: Event,
    ) -> None:
        try:
            target(run_id, *args)
        finally:
            try:
                self._primary_run_finished(run_id)
            finally:
                self._finish_publication(run_id, publication)

    def _primary_run_finished(self, run_id: str) -> None:
        next_thread: Thread | None = None
        skipped_publications: list[tuple[str, Event]] = []
        with self._lock:
            self._active_primary_runs.discard(run_id)
            self._threads.pop(run_id, None)
            while self._queued_primary_runs:
                next_run_id, target, args, publication = self._queued_primary_runs.popleft()
                if self.state.get_run(next_run_id).status in _TERMINAL_RUN_STATUSES:
                    skipped_publications.append((next_run_id, publication))
                    continue
                self._active_primary_runs.add(next_run_id)
                next_thread = Thread(
                    target=self._run_primary_thread,
                    args=(next_run_id, target, args, publication),
                    daemon=True,
                )
                self._threads[next_run_id] = next_thread
                break
        for skipped_run_id, skipped_publication in skipped_publications:
            self._finish_publication(skipped_run_id, skipped_publication)
        if next_thread is not None:
            next_thread.start()

    def _finish_publication(self, run_id: str, publication: Event) -> None:
        publication.set()
        with self._lock:
            if self._publication_events.get(run_id) is publication:
                self._publication_events.pop(run_id, None)

    def _start_thread(self, run_id: str, target: Any, *args: Any) -> None:
        thread = Thread(target=target, args=(run_id, *args), daemon=True)
        with self._lock:
            self._threads[run_id] = thread
        thread.start()

    def _is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled or run_id in self._lost_run_leases

    def _require_run_accepts_work(self, run_id: str, *, operation: str) -> RunRecord:
        run = self.state.get_run(run_id)
        if run.status in _TERMINAL_RUN_STATUSES:
            raise ValueError(f"{operation}_not_allowed_for_terminal_run:{run.status}")
        if self._is_cancelled(run_id):
            raise ValueError(f"{operation}_not_allowed_for_cancelled_run")
        return run


def _validate_task_completion(
    task: TaskNodeRecord | None,
    result: AgentTurnResult,
    *,
    allow_mock_provider: bool = False,
) -> dict[str, Any]:
    successful_tools = [execution.call.name for execution in result.tool_executions if execution.success]
    failed_tools = [execution.call.name for execution in result.tool_executions if not execution.success]
    required_tools = list(task.required_tools) if task is not None else []
    missing_tools = [] if allow_mock_provider else sorted(set(required_tools) - set(successful_tools))
    failure_codes: list[str] = []
    if result.stop_reason not in {"complete", "approval_required"}:
        failure_codes.append(f"stop_reason:{result.stop_reason}")
    if not result.assistant_message.strip():
        failure_codes.append("empty_assistant_message")
    if result.error:
        failure_codes.append("runtime_error")
    if failed_tools:
        failure_codes.append("failed_tools")
    if missing_tools:
        failure_codes.append("required_tools_missing")
    evidence: list[str] = []
    if result.proof_of_work:
        proof_evidence = result.proof_of_work.get("validation_evidence")
        if isinstance(proof_evidence, list):
            evidence.extend(str(item) for item in proof_evidence if str(item).strip())
    evidence.extend(f"tool:{tool_name}" for tool_name in successful_tools)
    if result.assistant_message.strip():
        evidence.append("assistant_response")
    criteria = list(task.acceptance_criteria) if task is not None else []
    passed = result.stop_reason == "approval_required" or not failure_codes
    return {
        "passed": passed,
        "failure_codes": failure_codes,
        "criteria": [
            {
                "criterion": criterion,
                "satisfied": passed,
                "evidence": list(evidence),
            }
            for criterion in criteria
        ],
        "successful_tools": successful_tools,
        "failed_tools": failed_tools,
        "missing_required_tools": missing_tools,
        "mock_validation_bypass": bool(allow_mock_provider and required_tools),
        "evidence": evidence,
    }


def _execution_payload(execution: ToolExecution) -> dict[str, Any]:
    return {
        "tool": execution.call.name,
        "tool_call_id": execution.call.id,
        "arguments": execution.call.arguments,
        "success": execution.success,
        "content": execution.content,
        "data": execution.data,
        "error": execution.error,
    }


def _approval_storage_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return an argument copy safe for approval records and audit events."""

    safe = redact_secrets(deepcopy(arguments))
    return safe if isinstance(safe, dict) else {}


def _contains_redaction_marker(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_redaction_marker(key) or _contains_redaction_marker(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_redaction_marker(item) for item in value)
    return isinstance(value, str) and "<redacted>" in value


def _turn_payload(result: AgentTurnResult) -> dict[str, Any]:
    return {
        "session_id": result.session_id,
        "user_message": result.user_message,
        "assistant_message": result.assistant_message,
        "tool_executions": [_execution_payload(execution) for execution in result.tool_executions],
        "context_chars": result.context_chars,
        "memory_writes": list(result.memory_writes),
        "stop_reason": result.stop_reason,
        "proof_of_work": result.proof_of_work,
    }


def _lease_owner_is_alive(owner: str | None) -> bool | None:
    match = re.fullmatch(r"manager_(\d+)_[A-Za-z0-9]+", owner or "")
    if match is None:
        return None
    return process_is_alive(int(match.group(1)))


def _worker_is_live(owner: str, heartbeat_at: str, *, ttl_seconds: float) -> bool:
    if _lease_owner_is_alive(owner) is not True or not heartbeat_at:
        return False
    try:
        heartbeat = datetime.fromisoformat(heartbeat_at.replace("Z", "+00:00"))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
    except ValueError:
        return False
    age_seconds = (datetime.now(UTC) - heartbeat).total_seconds()
    return -5.0 <= age_seconds <= max(0.1, ttl_seconds)


def _run_has_fresh_lease(run: RunRecord) -> bool:
    if not run.lease_owner or not run.lease_expires_at:
        return False
    try:
        expires_at = datetime.fromisoformat(run.lease_expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > datetime.now(UTC)


def _effective_config_snapshot(config: AgentConfig) -> dict[str, Any]:
    payload = asdict(runtime_settings_snapshot(config))
    payload["effective_config_schema_version"] = 1
    payload["effective_config"] = config.to_mapping()
    revision_payload = dict(payload)
    revision_payload.pop("revision", None)
    payload["revision"] = hashlib.sha256(
        json.dumps(
            revision_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    return payload


def _is_root_objective_task(task: TaskNodeRecord) -> bool:
    plan = task.plan or {}
    return task.parent_id is None and task.profile == "planner" and plan.get("decomposition") == "initial"


def _scheduler_run_outcome(scheduler: dict[str, Any]) -> tuple[str, str]:
    stop_reason = str(scheduler.get("stop_reason") or "idle")
    executed = scheduler.get("executed", [])
    statuses = {str(item.get("status")) for item in executed if isinstance(item, dict)}
    if "failed" in statuses or stop_reason == "task_failed":
        return "failed", stop_reason
    if stop_reason in {"tool_approval_required", "task_approval_required", "cycle_limit_reached"}:
        return "blocked", stop_reason
    return "completed", "scheduler_idle"


def _task_execution_prompt(task: TaskNodeRecord) -> str:
    dependencies = "\n".join(f"- {dependency}" for dependency in task.dependencies) or "- none"
    tools = "\n".join(f"- {tool}" for tool in task.required_tools) or "- none"
    criteria = "\n".join(f"- {criterion}" for criterion in task.acceptance_criteria) or "- Report concrete outcome and remaining risk."
    retry = task.retry_strategy or {}
    retry_note = ""
    if retry:
        retry_note = f"\nRetry strategy metadata:\n{retry}"
    return (
        f"Autonomous task profile: {task.profile}\n"
        f"Task title: {task.title}\n"
        f"Goal:\n{task.goal}\n\n"
        f"Dependencies:\n{dependencies}\n\n"
        f"Expected tools:\n{tools}\n\n"
        f"Acceptance criteria:\n{criteria}\n"
        f"{retry_note}\n\n"
        "Execute only the approved task scope. Use available tools when needed, respect high-risk approval gates, "
        "and finish with a concise result plus any blocker."
    )


def _task_payload(task: TaskNodeRecord) -> dict[str, Any]:
    payload = asdict(task)
    payload["dependencies"] = list(task.dependencies)
    payload["required_tools"] = list(task.required_tools)
    payload["acceptance_criteria"] = list(task.acceptance_criteria)
    return payload


def _task_scheduler_reason(task: TaskNodeRecord, by_id: dict[str, TaskNodeRecord]) -> str | None:
    if not task.approved:
        return None
    if task.status not in {"queued", "approved"}:
        return None
    if not _dependencies_completed(task, by_id):
        return None
    retry = task.retry_strategy or {}
    if retry.get("requires_changed_strategy"):
        if retry.get("retry_allowed") is not True or not str(retry.get("changed_strategy") or "").strip():
            return None
        return "retry_strategy_changed"
    if task.attempt_count > 0:
        return "retry_ready"
    return "dependencies_satisfied"


def _dependencies_completed(task: TaskNodeRecord, by_id: dict[str, TaskNodeRecord]) -> bool:
    return all(by_id.get(dependency) and by_id[dependency].status == "completed" for dependency in task.dependencies)


def _task_requires_default_worker_isolation(task: TaskNodeRecord) -> bool:
    repair_tool_prefixes = ("repair.",)
    code_mutation_tools = {"patch.apply", "git.commit", "git.create_local_branch", "git.export_patch"}
    if any(tool.startswith(repair_tool_prefixes) or tool in code_mutation_tools for tool in task.required_tools):
        return True
    text = f"{task.title} {task.goal}".lower()
    return "repair" in text and any(term in text for term in ("patch", "branch", "worktree", "commit", "validate"))


def _workspace_supports_git_worktree(workspace: Path) -> bool:
    git_marker = workspace / ".git"
    return git_marker.exists()


def _initial_task_plan(message: str, *, recent_messages: list[str] | tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Create a conservative persisted starter plan for new background runs.

    The live agent still does the real work. These deterministic nodes give the
    control plane a durable DAG skeleton for tracking, resume, and review instead
    of leaving every run as one opaque root task.
    """
    objective = message.strip() or "User objective"
    planning_context = "\n".join([*recent_messages, objective])
    inspect_id = f"task_{uuid4().hex}"
    validate_id = f"task_{uuid4().hex}"
    if _looks_like_repair_commit_request(objective):
        prepare_id = f"task_{uuid4().hex}"
        patch_id = f"task_{uuid4().hex}"
        review_id = f"task_{uuid4().hex}"
        commit_id = f"task_{uuid4().hex}"
        return [
            {
                "task_id": inspect_id,
                "title": "Inspect repair context",
                "goal": f"Gather repository context and failure evidence for: {objective}",
                "profile": "worker",
                "dependencies": [],
                "required_tools": ["repo.search", "repo.map", "memory.search", "context.pack"],
                "risk": "low",
                "acceptance_criteria": ["Relevant code, tests, and prior repair lessons are identified before mutation."],
            },
            {
                "task_id": prepare_id,
                "title": "Prepare repair isolation",
                "goal": f"Create or confirm an isolated repair branch/worktree before changing files for: {objective}",
                "profile": "worker",
                "dependencies": [inspect_id],
                "required_tools": ["repair.prepare", "repair.status"],
                "risk": "high",
                "acceptance_criteria": ["Mutation happens only on an approved repair branch/worktree."],
            },
            {
                "task_id": patch_id,
                "title": "Apply repair patch",
                "goal": f"Apply the smallest repair patch for: {objective}",
                "profile": "worker",
                "dependencies": [prepare_id],
                "required_tools": ["repair.apply_patch", "patch.apply"],
                "risk": "high",
                "acceptance_criteria": ["Patch is scoped to the diagnosed repair and path-safe."],
            },
            {
                "task_id": validate_id,
                "title": "Validate repair",
                "goal": f"Run targeted validation and classify failures for: {objective}",
                "profile": "worker",
                "dependencies": [patch_id],
                "required_tools": ["repair.orchestrate_validate", "repair.validate", "test.run", "lint.run"],
                "risk": "high",
                "acceptance_criteria": ["Targeted validation passes, or retry guidance records a changed strategy."],
            },
            {
                "task_id": review_id,
                "title": "Review repair before commit",
                "goal": f"Create the durable repair.review artifact after successful validation for: {objective}",
                "profile": "reviewer",
                "dependencies": [validate_id],
                "required_tools": ["repair.review", "git.diff", "repair.status"],
                "risk": "medium",
                "acceptance_criteria": ["repair.review records successful validation, current branch, changed files, and current diff hash."],
            },
            {
                "task_id": commit_id,
                "title": "Commit reviewed repair",
                "goal": f"Commit only after repair.review created a current reviewer gate for: {objective}",
                "profile": "worker",
                "dependencies": [review_id],
                "required_tools": ["git.commit"],
                "risk": "high",
                "acceptance_criteria": ["git.commit includes the current repair.review id and still requires exact-call approval."],
            },
        ]
    if _looks_like_artifact_build_request(planning_context):
        create_id = f"task_{uuid4().hex}"
        review_id = f"task_{uuid4().hex}"
        return [
            {
                "task_id": inspect_id,
                "title": "Inspect build context",
                "goal": f"Gather relevant files, constraints, and prior work before creating an artifact for: {objective}",
                "profile": "worker",
                "dependencies": [],
                "required_tools": ["repo.map", "file.list", "memory.search", "context.pack"],
                "risk": "low",
                "acceptance_criteria": ["Relevant workspace context is identified before changing files."],
            },
            {
                "task_id": create_id,
                "title": "Create artifact",
                "goal": f"Create the requested artifact for: {objective}",
                "profile": "worker",
                "dependencies": [inspect_id],
                "required_tools": ["file.write", "patch.apply", "tool.registry"],
                "risk": "high",
                "acceptance_criteria": [
                    "Artifact files are created under the workspace, or an explicit blocker is recorded.",
                    "High-risk file changes remain behind task/tool approval gates.",
                ],
            },
            {
                "task_id": validate_id,
                "title": "Validate artifact",
                "goal": f"Inspect or run the most relevant validation for the created artifact: {objective}",
                "profile": "worker",
                "dependencies": [create_id],
                "required_tools": ["file.read", "file.stat", "project.scripts", "test.run", "lint.run"],
                "risk": "low",
                "acceptance_criteria": ["Created files are inspected or validated, and any remaining risks are explicit."],
            },
            {
                "task_id": review_id,
                "title": "Review outcome",
                "goal": f"Review whether the artifact satisfies: {objective}",
                "profile": "reviewer",
                "dependencies": [validate_id],
                "required_tools": ["file.read", "git.diff"],
                "risk": "low",
                "acceptance_criteria": ["Artifact path, validation evidence, and next steps are explicit."],
            },
        ]
    return [
        {
            "task_id": inspect_id,
            "title": "Inspect context",
            "goal": f"Gather relevant context for: {objective}",
            "profile": "worker",
            "dependencies": [],
            "required_tools": ["memory.search", "context.pack"],
            "risk": "low",
            "acceptance_criteria": ["Relevant memory/context is considered before acting."],
        },
        {
            "task_id": validate_id,
            "title": "Execute and validate",
            "goal": f"Execute the approved low-risk path and validate progress for: {objective}",
            "profile": "worker",
            "dependencies": [inspect_id],
            "required_tools": ["tool.registry"],
            "risk": "low",
            "acceptance_criteria": ["Result is checked against the objective and failures are recorded."],
        },
        {
            "task_id": f"task_{uuid4().hex}",
            "title": "Review outcome",
            "goal": f"Review whether the result satisfies: {objective}",
            "profile": "reviewer",
            "dependencies": [validate_id],
            "required_tools": [],
            "risk": "low",
            "acceptance_criteria": ["Remaining risks or next steps are explicit."],
        },
    ]


def _looks_like_repair_commit_request(message: str) -> bool:
    normalized = message.lower()
    repair_terms = ("repair", "fix", "patch", "failing", "failure", "bug")
    commit_terms = ("commit", "merge", "pr", "pull request")
    validation_terms = ("validate", "test", "lint", "check")
    return (
        any(term in normalized for term in repair_terms)
        and any(term in normalized for term in commit_terms)
        and any(term in normalized for term in validation_terms)
    )


def _looks_like_artifact_build_request(message: str) -> bool:
    normalized = message.lower()
    action_terms = ("build", "create", "make", "generate", "implement", "ship", "scaffold")
    artifact_terms = (
        "app",
        "application",
        "artifact",
        "component",
        "game",
        "page",
        "program",
        "project",
        "random",
        "site",
        "something",
        "tool",
        "website",
        "whimsical",
    )
    continuation_terms = ("continue", "go", "go for it", "keep going", "resume", "do it")
    has_artifact_goal = _contains_term(normalized, action_terms) and _contains_term(normalized, artifact_terms)
    has_recent_artifact_context = _contains_term(normalized, artifact_terms) and _contains_term(
        normalized,
        continuation_terms,
    )
    return has_artifact_goal or has_recent_artifact_context


def _contains_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)


def _trace_category(event: dict[str, Any]) -> str:
    event_type = str(event.get("type", ""))
    payload = event.get("payload", {})
    if event_type.startswith("span."):
        return "span"
    if event_type.startswith("tool.") or event_type == "assistant.tool_call":
        return "tool"
    if event_type.startswith("memory.") or _payload_has_key(payload, "memory_writes"):
        return "memory"
    if event_type.startswith("context."):
        return "context"
    if event_type.startswith("assistant.") or event_type.startswith("llm.") or event_type.startswith("provider."):
        return "provider"
    if event_type.startswith("approval."):
        return "approval"
    if event_type.endswith(".failed") or event_type.endswith(".error") or _payload_has_key(payload, "error"):
        return "error"
    return "lifecycle"


def _payload_has_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_payload_has_key(item, key) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_payload_has_key(item, key) for item in value)
    return False


def _span_counts(spans: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for span in spans:
        span_type = str(span.get("span_type", "unknown"))
        counts[span_type] = counts.get(span_type, 0) + 1
    return counts


def _capsule_decisions(
    summary: Any,
    *,
    agent: NestedMV2Agent,
    dry_run: bool,
) -> list[dict[str, object]]:
    kernel = NestedLearningKernel()
    decisions: list[dict[str, object]] = []
    wrote = False
    for signal in summary.learning_signals:
        decision = kernel.decide(signal)
        payload = decision.to_payload()
        payload["dry_run"] = dry_run
        payload["signal_title"] = signal.title
        if decision.accepted and decision.target_layer is not None:
            if decision.target_layer == MemoryLayer.POLICY and not (
                agent.config.allow_policy_writes and signal.explicit_instruction
            ):
                payload["accepted"] = False
                payload["blocked"] = "policy_write_requires_explicit_config_and_instruction"
            elif not dry_run:
                record = kernel.to_memory_record(signal, decision)
                payload["record_id"] = agent.memory.put(record)
                wrote = True
        decisions.append(payload)
    if wrote:
        agent.memory.seal_all()
    return decisions


def _subagent_prompt(profile: str, goal: str) -> str:
    role = {
        "planner": "Break the goal into a concise execution plan with dependencies and checks.",
        "worker": "Execute the bounded subtask using available low-risk tools and report concrete results.",
        "reviewer": "Review the proposed or completed work for risks, missing tests, and next checks.",
    }.get(profile, "Execute the bounded subtask and report concrete results.")
    return f"Subagent profile: {profile}\nRole: {role}\nGoal:\n{goal}"
