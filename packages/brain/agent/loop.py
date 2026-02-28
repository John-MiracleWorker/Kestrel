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

logger = logging.getLogger("brain.agent.loop")


def _council_debate_enabled() -> bool:
    """Whether to run the council cross-critique debate round."""
    return os.getenv("COUNCIL_INCLUDE_DEBATE", "false").lower() == "true"


class AgentLoop:
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
        )

        # Callback for approval resolution (set by the gRPC handler)
        self._approval_callback: Optional[Callable] = None

    async def run(self, task: AgentTask) -> AsyncIterator[TaskEvent]:
        """
        Execute an agent task, yielding events as they occur.

        This is the main entry point. It runs the full planning → execution
        → reflection loop until the task is complete, fails, or is paused.
        """
        start_time = time.monotonic()

        # ── Wire multi-agent coordinator for this task ────────
        try:
            from agent.coordinator import Coordinator
            coordinator = Coordinator(
                agent_loop=self,
                persistence=self._persistence,
                tool_registry=self._tools,
                event_callback=self._event_callback,
            )
            self._tools._coordinator = coordinator
            self._tools._current_task = task
        except Exception as e:
            logger.warning(f"Coordinator init skipped: {e}")
        try:
            # ── Phase 0: Enrich with Past Lessons + Memory Graph ─
            lesson_context = ""
            if self._learner:
                try:
                    lesson_context = await self._learner.enrich_context(
                        workspace_id=task.workspace_id,
                        goal=task.goal,
                    )
                    if lesson_context and self._event_callback:
                        lesson_count = lesson_context.count('\n') + 1
                        await self._event_callback("lessons_loaded", {
                            "count": lesson_count,
                            "preview": lesson_context[:150],
                        })
                except Exception as e:
                    logger.warning(f"Lesson enrichment failed: {e}")

            # Query the memory graph for relevant context
            memory_context = ""
            if self._memory_graph:
                try:
                    # Extract meaningful terms — skip stop words, keep technical nouns
                    _STOP_WORDS = frozenset({
                        "the", "this", "that", "then", "than", "with", "from", "have",
                        "will", "your", "what", "when", "where", "how", "should", "would",
                        "could", "into", "need", "make", "also", "some", "more", "just",
                        "about", "been", "they", "them", "their", "does", "done", "task",
                        "using", "which", "these", "those", "here", "there", "after",
                        "before", "please", "like", "want", "help", "create", "build",
                    })
                    goal_terms = [
                        w.lower().strip(".,!?:;")
                        for w in task.goal.split()
                        if len(w) > 3 and w.lower() not in _STOP_WORDS
                    ][:8]  # up from 5 — more terms = better graph recall
                    memory_context = await self._memory_graph.format_for_prompt(
                        workspace_id=task.workspace_id,
                        query_entities=goal_terms,
                    )
                    if memory_context and self._event_callback:
                        mem_lines = [l for l in memory_context.split('\n') if l.strip()]
                        await self._event_callback("memory_recalled", {
                            "count": len(mem_lines),
                            "entities": goal_terms,
                            "preview": memory_context[:200],
                        })
                except Exception as e:
                    logger.warning(f"Memory graph query failed: {e}")

            # ── Phase 1: Planning ────────────────────────────────
            if task.status == TaskStatus.PLANNING:
                task.status = TaskStatus.PLANNING
                await self._persistence.update_task(task)

                context = self._build_context(task)
                if lesson_context:
                    context += f"\n\n{lesson_context}"
                if memory_context:
                    # Deduplicate: skip memory lines whose first 60 chars already
                    # appear in the lesson context to avoid repeating known facts.
                    if lesson_context:
                        lesson_fingerprints = {
                            line.strip().lower()[:60]
                            for line in lesson_context.splitlines()
                            if line.strip()
                        }
                        deduped_mem = "\n".join(
                            line for line in memory_context.splitlines()
                            if line.strip().lower()[:60] not in lesson_fingerprints
                        )
                        if deduped_mem.strip():
                            context += f"\n\n{deduped_mem}"
                    else:
                        context += f"\n\n{memory_context}"

                try:
                    plan = await self._planner.create_plan(
                        goal=task.goal,
                        available_tools=self._tools.list_tools(),
                        context=context,
                    )
                    task.plan = plan
                except Exception as e:
                    logger.warning(f"Planning failed, using single-step fallback: {e}")
                    task.plan = TaskPlan(
                        goal=task.goal,
                        steps=[TaskStep(
                            index=0,
                            description=f"Execute the goal directly: {task.goal[:200]}",
                            status=StepStatus.PENDING,
                        )],
                        reasoning=f"Planning failed ({e}) — executing as single step",
                    )

                await self._persistence.update_task(task)

                # Emit plan for UI process bar
                if self._event_callback:
                    await self._event_callback("plan_created", {
                        "step_count": len(task.plan.steps),
                        "steps": [
                            {"index": s.index, "description": s.description[:100]}
                            for s in task.plan.steps[:6]
                        ],
                    })

                # Record plan decision in evidence chain
                if self._evidence_chain:
                    self._evidence_chain.record_plan_decision(
                        plan_summary=f"Created {len(task.plan.steps)}-step plan for: {task.goal[:100]}",
                        reasoning=f"Decomposed goal into {len(task.plan.steps)} steps based on available tools",
                        confidence=0.7,
                    )

                yield TaskEvent(
                    type=TaskEventType.PLAN_CREATED,
                    task_id=task.id,
                    content=json.dumps(task.plan.to_dict()),
                    progress=self._progress(task),
                )

                # ── Reflection: Red-team the plan before execution ──
                if self._reflection_engine and len(task.plan.steps) > 2:
                    try:
                        plan_text = json.dumps(task.plan.to_dict())
                        reflection = await self._reflection_engine.reflect(
                            plan=plan_text,
                            task_goal=task.goal,
                        )
                        logger.info(
                            f"Reflection: confidence={reflection.confidence_score:.2f} "
                            f"risk={reflection.estimated_risk_level} "
                            f"proceed={reflection.should_proceed} "
                            f"critiques={len(reflection.critique_points)}"
                        )
                        if self._evidence_chain:
                            self._evidence_chain.record_plan_decision(
                                plan_summary=f"Reflection: {reflection.estimated_risk_level} risk, confidence={reflection.confidence_score:.2f}",
                                reasoning=reflection.confidence_justification[:200],
                                confidence=reflection.confidence_score,
                            )
                        if not reflection.should_proceed:
                            yield TaskEvent(
                                type=TaskEventType.THINKING,
                                task_id=task.id,
                                content=f"⚠ Reflection flagged critical issues (confidence={reflection.confidence_score:.2f}). Proceeding with caution.\n" +
                                        "\n".join(f"- [{c.severity}] {c.description}" for c in reflection.critique_points[:3]),
                                progress=self._progress(task),
                            )
                    except Exception as e:
                        logger.warning(f"Reflection engine failed: {e}")

                # ── Council Deliberation: Multi-Agent Consensus ─────
                # Only invoke council for complex plans (complexity > 5.0)
                # to avoid wasting 5+ LLM calls on simple tasks
                plan_complexity = float(len(task.plan.steps))  # rough default
                try:
                    from agent.model_router import estimate_complexity, classify_step
                    _st = classify_step(task.goal)
                    plan_complexity = estimate_complexity(task.goal, _st)
                except Exception:
                    pass

                # Council thresholds:
                #   < 5.0  — skip entirely (cheap tasks, no council needed)
                #   5.0–7.0 — deliberate_lite(): 3 most relevant members, no debate (~40% cheaper)
                #   > 7.0  — full deliberate(): all 5 members + optional debate round
                if hasattr(self, "_council") and self._council and plan_complexity > 5.0:
                    try:
                        plan_text = json.dumps(task.plan.to_dict())
                        if plan_complexity <= 7.0:
                            # Moderate complexity — mini council (3 members, no debate)
                            verdict = await self._council.deliberate_lite(
                                proposal=plan_text,
                                context=task.goal,
                                top_n=3,
                            )
                        else:
                            # High complexity — full council
                            verdict = await self._council.deliberate(
                                proposal=plan_text,
                                context=task.goal,
                                include_debate=_council_debate_enabled(),
                            )
                        
                        if verdict.requires_user_review:
                            yield TaskEvent(
                                type=TaskEventType.THINKING,
                                task_id=task.id,
                                content=f"⚖️ Council Review Required: {verdict.review_reason}\n\n" +
                                        "\n".join(f"- {c}" for c in verdict.synthesized_concerns),
                                progress=self._progress(task),
                            )
                            # Pause the loop and ask the user
                            yield TaskEvent(
                                type=TaskEventType.APPROVAL_NEEDED,
                                task_id=task.id,
                                content=f"The Council is divided or flagged a critical issue. Proceed?",
                                progress=self._progress(task),
                            )
                            # The executor loop below handles the actual blocking/waiting for APPROVAL_NEEDED
                            task.status = TaskStatus.WAITING_APPROVAL
                            await self._persistence.update_task(task)
                            
                            # Wait for user approval
                            approved = await self._executor._wait_for_approval(task)
                            if not approved:
                                task.status = TaskStatus.FAILED
                                task.error = "User denied plan after Council review."
                                await self._persistence.update_task(task)
                                return
                                
                            task.status = TaskStatus.EXECUTING
                            await self._persistence.update_task(task)

                    except Exception as e:
                        logger.warning(f"Council deliberation failed: {e}")

            # ── Phase 2: Execution Loop ──────────────────────────
            # Guard: if plan is still None (shouldn't happen, but be safe), create fallback
            if task.plan is None:
                task.plan = TaskPlan(
                    goal=task.goal,
                    steps=[TaskStep(
                        index=0,
                        description=f"Respond to: {task.goal[:200]}",
                        status=StepStatus.PENDING,
                    )],
                )

            task.status = TaskStatus.EXECUTING
            await self._persistence.update_task(task)

            while not task.plan.is_complete:
                task.iterations += 1

                # Budget checks
                budget_error = self._guardrails.check_budget(task)
                if budget_error:
                    task.status = TaskStatus.FAILED
                    task.error = budget_error
                    await self._persistence.update_task(task)
                    yield TaskEvent(
                        type=TaskEventType.TASK_FAILED,
                        task_id=task.id,
                        content=budget_error,
                        progress=self._progress(task),
                    )
                    return

                # Wall-clock time check
                elapsed = time.monotonic() - start_time
                if elapsed > task.config.max_wall_time_seconds:
                    task.status = TaskStatus.FAILED
                    task.error = f"Wall-clock time limit exceeded ({int(elapsed)}s)"
                    await self._persistence.update_task(task)
                    yield TaskEvent(
                        type=TaskEventType.TASK_FAILED,
                        task_id=task.id,
                        content=task.error,
                        progress=self._progress(task),
                    )
                    return

                # Get next step
                step = task.plan.current_step
                if not step:
                    # No more executable steps (deps not met or all done)
                    break

                if step.status == StepStatus.PENDING:
                    step.status = StepStatus.IN_PROGRESS
                    step.started_at = datetime.now(timezone.utc)
                    await self._persistence.update_task(task)

                    yield TaskEvent(
                        type=TaskEventType.STEP_STARTED,
                        task_id=task.id,
                        step_id=step.id,
                        content=step.description,
                        progress=self._progress(task),
                    )

                # ── Reason: Ask LLM what to do ───────────────────
                async for event in self._executor.run_step(task, step):
                    yield event

                    # Check if we need to pause for approval
                    if event.type == TaskEventType.APPROVAL_NEEDED:
                        task.status = TaskStatus.WAITING_APPROVAL
                        await self._persistence.update_task(task)

                        # Wait for approval (this blocks until resolved)
                        approved = await self._executor._wait_for_approval(task)
                        if not approved:
                            # Denied — skip this step
                            step.status = StepStatus.SKIPPED
                            step.result = "Skipped — human denied approval"
                            task.status = TaskStatus.EXECUTING
                            await self._persistence.update_task(task)
                            break  # Move to next step

                        task.status = TaskStatus.EXECUTING
                        await self._persistence.update_task(task)

                    # Check if step completed
                    if step.status == StepStatus.COMPLETE:
                        yield TaskEvent(
                            type=TaskEventType.STEP_COMPLETE,
                            task_id=task.id,
                            step_id=step.id,
                            content=step.result or "",
                            progress=self._progress(task),
                        )
                        break

                    if step.status == StepStatus.FAILED:
                        # Try retry with exponential backoff (max 3 attempts per step)
                        if step.attempts < 3:
                            step.status = StepStatus.IN_PROGRESS
                            step.attempts += 1
                            backoff_delay = 2 ** (step.attempts - 1)  # 1s, 2s, 4s
                            logger.info(
                                f"Retrying step {step.id} (attempt {step.attempts}/3, "
                                f"backoff {backoff_delay}s): {step.error or 'unknown error'}"
                            )
                            await asyncio.sleep(backoff_delay)
                        else:
                            yield TaskEvent(
                                type=TaskEventType.TASK_FAILED,
                                task_id=task.id,
                                step_id=step.id,
                                content=step.error or "Step failed after 3 attempts",
                                progress=self._progress(task),
                            )
                            task.status = TaskStatus.FAILED
                            task.error = f"Step '{step.description[:80]}' failed after 3 retries: {step.error}"
                            await self._persistence.update_task(task)
                            return

                # ── Reflect: Should we replan? ───────────────────
                # Skip reflection if budget is already exhausted
                budget_ok = self._guardrails.check_budget(task) is None
                if (
                    budget_ok
                    and step
                    and step.status == StepStatus.COMPLETE
                    and task.iterations % 5 == 0
                    and task.plan.revision_count < 3
                ):
                    task.status = TaskStatus.REFLECTING
                    await self._persistence.update_task(task)

                    revised = await self._planner.revise_plan(
                        plan=task.plan,
                        observations=step.result or "",
                        available_tools=self._tools.list_tools(),
                    )
                    task.plan = revised
                    task.status = TaskStatus.EXECUTING
                    await self._persistence.update_task(task)

            # ── Phase 3: Completion ──────────────────────────────
            task.status = TaskStatus.COMPLETE
            task.completed_at = datetime.now(timezone.utc)

            # Build final summary from step results
            results = []
            for s in task.plan.steps:
                if s.result:
                    # In chat mode, use raw result without step description prefix
                    if task.messages:
                        results.append(s.result)
                    else:
                        results.append(f"**{s.description}**: {s.result}")
            task.result = "\n".join(results) if results else "Task completed successfully."

            await self._persistence.update_task(task)

            # Emit final process bar events
            if self._event_callback:
                # Token usage (enriched with metrics collector data)
                metrics_data = self._metrics.metrics.to_dict()
                await self._event_callback("token_usage", {
                    "total_tokens": task.token_usage,
                    "iterations": task.iterations,
                    "tool_calls": task.tool_calls_count,
                    "estimated_cost_usd": metrics_data.get("estimated_cost_usd", 0),
                    "llm_calls": metrics_data.get("llm_calls", 0),
                    "avg_tool_time_ms": metrics_data.get("avg_tool_time_ms", 0),
                    "total_elapsed_ms": metrics_data.get("total_elapsed_ms", 0),
                })
                # Evidence summary
                if self._evidence_chain and self._evidence_chain._decisions:
                    await self._event_callback("evidence_summary", {
                        "decision_count": len(self._evidence_chain._decisions),
                        "decisions": [
                            {"type": d.decision_type.value, "description": d.description[:80]}
                            for d in self._evidence_chain._decisions[:5]
                        ],
                    })

            yield TaskEvent(
                type=TaskEventType.TASK_COMPLETE,
                task_id=task.id,
                content=task.result,
                progress=self._progress(task),
            )

            # ── Phase 3b: Persist evidence chain ─────────────────
            if self._evidence_chain:
                try:
                    await self._evidence_chain.persist()
                except Exception as e:
                    logger.warning(f"Evidence chain persistence failed: {e}")

            # ── Phase 4: Update memory graph with task entities ───
            if self._memory_graph and task.result:
                try:
                    from agent.core.memory_graph import extract_entities_llm
                    _entities, _relations = await extract_entities_llm(
                        provider=self._provider,
                        model=self._model,
                        api_key=self._api_key,
                        user_message=task.goal,
                        assistant_response=task.result,
                    )
                    if _entities:
                        await self._memory_graph.extract_and_store(
                            conversation_id=task.id,
                            workspace_id=task.workspace_id,
                            entities=_entities,
                            relations=_relations,
                        )
                        logger.info(f"Memory graph: stored {len(_entities)} entities, {len(_relations)} relations from task {task.id}")
                except Exception as e:
                    logger.warning(f"Memory graph update failed: {e}")

            # ── Phase 5: Learn from this task ────────────────────
            if self._learner:
                # Skip extraction for trivial tasks — a 1-step, error-free,
                # low-tool-call run has nothing novel worth a 1024-token LLM call.
                _is_trivial = (
                    task.iterations <= 2
                    and task.tool_calls_count < 5
                    and task.status == TaskStatus.COMPLETE
                    and (not task.plan or len(task.plan.steps) <= 1)
                )
                if _is_trivial:
                    logger.debug(
                        f"Skipping lesson extraction for trivial task {task.id} "
                        f"(iterations={task.iterations}, tools={task.tool_calls_count})"
                    )
                else:
                    try:
                        await self._learner.extract_lessons(task)
                    except Exception as e:
                        logger.warning(f"Post-task learning failed: {e}")

        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            await self._persistence.update_task(task)
            yield TaskEvent(
                type=TaskEventType.TASK_PAUSED,
                task_id=task.id,
                content="Task cancelled",
                progress=self._progress(task),
            )

        except Exception as e:
            logger.error(f"Agent loop error: {e}", exc_info=True)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            await self._persistence.update_task(task)
            yield TaskEvent(
                type=TaskEventType.TASK_FAILED,
                task_id=task.id,
                content=str(e),
                progress=self._progress(task),
            )

    def _progress(self, task: AgentTask) -> dict:
        """Build progress snapshot."""
        done, total = task.plan.progress if task.plan else (0, 0)
        return {
            "current_step": done,
            "total_steps": total,
            "iterations": task.iterations,
            "tokens_used": task.token_usage,
            "tool_calls": task.tool_calls_count,
        }

    def _build_context(self, task: AgentTask) -> str:
        """Build context string for the planner."""
        parts = [f"Workspace: {task.workspace_id}"]
        if task.conversation_id:
            parts.append(f"Conversation: {task.conversation_id}")
        return "\n".join(parts)


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
