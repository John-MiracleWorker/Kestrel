"""
Agent Loop Engine — ReAct (Reason + Act) state machine for autonomous execution.

This is the heart of Kestrel's autonomous agent. It orchestrates:
  1. Planning — decompose the goal into steps
  2. Executing — run tools and observe results (with parallel tool dispatch)
  3. Reflecting — decide next action based on observations
  4. Completing — summarize results and report

The loop is fully resumable: all state is persisted to PostgreSQL,
so tasks survive service restarts and can be paused/resumed.

Enhancements:
  - Parallel tool execution: when the LLM returns multiple independent tool
    calls, they are dispatched concurrently for faster task completion.
  - Smart retry with exponential backoff for transient tool failures.
  - Streaming metrics integration for real-time cost/token tracking.
  - Parallel step execution for independent plan steps.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

from core.feature_mode import get_feature_mode, mode_supports_labs, mode_supports_ops
from core import runtime as runtime_module
from agent.types import (
    AgentTask,
    ApprovalRequest,
    ApprovalStatus,
    GuardrailConfig,
    RiskLevel,
    StepStatus,
    TaskEvent,
    TaskEventType,
    TaskPlan,
    TaskStep,
    TaskStatus,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent.core.planner import TaskPlanner
from agent.learner import TaskLearner
from agent.core.memory_graph import MemoryGraph
from agent.evidence import EvidenceChain, DecisionType
from agent.observability import MetricsCollector
from agent.model_router import ModelRouter, classify_step
from agent.council import CouncilSession, CouncilRole
from agent.core.executor import TaskExecutor
from agent.loop_runtime import AgentLoopRuntimeMixin
from agent.step_scheduler import StepScheduler
from agent.simulation import OutcomeSimulator
from agent.state_machine import TaskStateMachine

logger = logging.getLogger("brain.agent.loop")

# Emergency rollback flag: keep the legacy loop available only as a fallback.
_ENABLE_LEGACY_LOOP = os.getenv("KESTREL_ENABLE_LEGACY_LOOP", "false").lower() == "true"


def _council_debate_enabled() -> bool:
    """Whether to run the council cross-critique debate round."""
    return os.getenv("COUNCIL_INCLUDE_DEBATE", "false").lower() == "true"


def _feature_mode_allows_reflection() -> bool:
    return mode_supports_ops(get_feature_mode())


def _feature_mode_allows_simulation() -> bool:
    return mode_supports_labs(get_feature_mode())


def _feature_mode_allows_council() -> bool:
    return mode_supports_labs(get_feature_mode())


class AgentLoop(AgentLoopRuntimeMixin):
    """
    ReAct agent loop — plans, executes tools, observes results, and reflects.

    Usage:
        loop = AgentLoop(provider, tool_registry, guardrails, persistence)
        async for event in loop.run(task):
            # Stream events to the client in real-time
            print(event)
    """

    def __init__(
        self,
        provider,
        tool_registry: "ToolRegistry",
        guardrails: "Guardrails",
        persistence: "TaskPersistence",
        model: str = "",
        learner: Optional[TaskLearner] = None,
        checkpoint_manager=None,
        memory_graph: Optional[MemoryGraph] = None,
        evidence_chain: Optional[EvidenceChain] = None,
        api_key: str = "",
        event_callback=None,
        reflection_engine=None,
        model_router: Optional[ModelRouter] = None,
        provider_resolver=None,
        approval_memory=None,
        simulator: Optional[OutcomeSimulator] = None,
        verifier=None,
        persona_learner=None,
    ):
        self._provider = provider
        self._provider_resolver = provider_resolver  # callable: (name) -> provider
        self._tools = tool_registry
        self._guardrails = guardrails
        self._persistence = persistence
        self._model = model
        self._api_key = api_key
        self._planner = TaskPlanner(provider, model)
        self._learner = learner
        self._checkpoints = checkpoint_manager
        self._memory_graph = memory_graph
        self._evidence_chain = evidence_chain
        self._event_callback = event_callback
        self._reflection_engine = reflection_engine
        self._model_router = model_router or ModelRouter()
        self._approval_memory = approval_memory
        self._simulator = simulator
        self._persona_learner = persona_learner
        self._metrics = MetricsCollector(model=model)
        self._council = CouncilSession(
            llm_provider=provider,
            model=model,
            event_callback=event_callback,
        )

        self._executor = TaskExecutor(
            provider=provider,
            tool_registry=tool_registry,
            guardrails=guardrails,
            persistence=persistence,
            metrics=self._metrics,
            model=model,
            api_key=api_key,
            model_router=self._model_router,
            provider_resolver=provider_resolver,
            event_callback=event_callback,
            evidence_chain=evidence_chain,
            progress_callback=self._progress,
            approval_memory=approval_memory,
            verifier=verifier,
        )

        self._step_scheduler = StepScheduler(
            executor=self._executor,
            planner=self._planner,
            guardrails=guardrails,
            persistence=persistence,
            tool_registry=tool_registry,
            learner=learner,
            event_callback=event_callback,
        )

        # State machine enforces legal task status transitions
        self._state_machine = TaskStateMachine(strict=False)

        # Callback for approval resolution (set by the gRPC handler)
        self._approval_callback: Optional[Callable] = None

    def _should_replan(self, task: AgentTask) -> bool:
        """Drift-based replanning: replan when the plan is going off-track,
        not on a fixed iteration interval.

        Triggers replanning when:
        - 2+ consecutive recent step failures (plan is stale)
        - On track to exceed 80% of the iteration budget
        - Fallback: every 8 iterations (relaxed from 5)

        Caps total replans at 3 per task.
        """
        if task.plan.revision_count >= 3:
            return False

        # Count consecutive recent failures (last 3 steps)
        recent_steps = [s for s in task.plan.steps if s.status in (StepStatus.COMPLETE, StepStatus.FAILED)]
        recent_failures = 0
        for s in reversed(recent_steps[-3:]):
            if s.status == StepStatus.FAILED:
                recent_failures += 1
            else:
                break
        if recent_failures >= 2:
            return True

        # Check if on track to exceed iteration budget
        done_count = sum(1 for s in task.plan.steps if s.status == StepStatus.COMPLETE)
        total_count = len(task.plan.steps)
        if done_count > 0 and total_count > 0:
            projected_iterations = task.iterations * (total_count / done_count)
            if projected_iterations > task.config.max_iterations * 0.8:
                return True

        # Relaxed fallback: every 8 iterations
        return task.iterations % 8 == 0

    def _should_skip_council(self, task: AgentTask, plan_complexity: float) -> bool:
        """Skip council deliberation for routine plans.

        A plan is routine when ALL of these hold:
        - Complexity is below 8.5
        - No plan steps involve HIGH risk tools
        - No steps mention security-sensitive operations

        MEDIUM-risk tools (file_write, code_execute, mcp_call, etc.) are
        routine operations and no longer trigger council review.  Only
        HIGH-risk tools (host_write, database_mutate, container rebuild)
        warrant the full council.

        Saves 3-5 LLM calls for the majority of real tasks.
        """
        if plan_complexity >= 8.5:
            return False

        if not task.plan or not task.plan.steps:
            return False

        for step in task.plan.steps:
            # Check tool calls — only HIGH risk triggers council
            for tc in step.tool_calls:
                tool_name = tc.get("tool", tc.get("function", {}).get("name", ""))
                if tool_name:
                    risk = self._tools.get_risk_level(tool_name)
                    if risk == RiskLevel.HIGH:
                        return False
            # Check description for security-sensitive keywords
            desc_lower = step.description.lower()
            if any(kw in desc_lower for kw in (
                "delete", "deploy", "credential", "secret", "admin",
                "sudo", "production", "database migration",
            )):
                return False

        logger.debug(
            f"Council skip: no HIGH-risk tools in {len(task.plan.steps)} steps "
            f"(complexity={plan_complexity:.1f})"
        )
        return True

    async def run(self, task: AgentTask) -> AsyncIterator[TaskEvent]:
        """
        Execute an agent task, yielding events as they occur.

        This is the main entry point. LangGraph is the default execution
        engine. The legacy loop remains available only as an emergency
        rollback path via KESTREL_ENABLE_LEGACY_LOOP=true.
        """
        if _ENABLE_LEGACY_LOOP:
            async for event in self._run_legacy(task):
                yield event
            return

        async for event in self._run_langgraph(task):
            yield event

# ── Task Persistence Interface ───────────────────────────────────────


class TaskPersistence:
    """
    Abstract interface for persisting agent task state.
    Implemented by the database layer in server.py.
    """

    async def save_task(self, task: AgentTask) -> None:
        """Save a new task to the database."""
        raise NotImplementedError

    async def update_task(self, task: AgentTask) -> None:
        """Update an existing task."""
        raise NotImplementedError

    async def get_task(self, task_id: str) -> Optional[AgentTask]:
        """Load a task by ID."""
        raise NotImplementedError

    async def save_approval(self, approval: ApprovalRequest) -> None:
        """Save an approval request."""
        raise NotImplementedError

    async def get_approval(self, approval_id: str) -> Optional[ApprovalRequest]:
        """Get an approval request by ID."""
        raise NotImplementedError

    async def resolve_approval(
        self,
        approval_id: str,
        status: ApprovalStatus,
        decided_by: str,
    ) -> bool:
        """Resolve an approval request. Returns True when a pending approval was updated."""
        raise NotImplementedError
