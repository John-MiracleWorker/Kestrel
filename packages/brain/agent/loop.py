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
from agent.planner import TaskPlanner
from agent.learner import TaskLearner
from agent.memory_graph import MemoryGraph
from agent.evidence import EvidenceChain, DecisionType
from agent.observability import MetricsCollector

logger = logging.getLogger("brain.agent.loop")

# ── Constants ────────────────────────────────────────────────────────
MAX_PARALLEL_TOOLS = 5       # Max concurrent tool executions per turn
RETRY_MAX_ATTEMPTS = 3       # Max retries for transient tool failures
RETRY_BASE_DELAY_S = 1.0     # Base delay for exponential backoff
PARALLEL_STEP_MAX = 3         # Max steps to execute concurrently


# ── System Prompt for the Reasoning LLM ──────────────────────────────

AGENT_SYSTEM_PROMPT = """\
You are Kestrel, an autonomous AI agent. You are executing a multi-step task.

Current goal: {goal}
Current step: {step_description}

Instructions:
1. Analyze the current situation and decide which tool to call next.
2. Call exactly ONE tool per turn. Wait for its result before proceeding.
3. If the step is complete, call `task_complete` with a summary of what you accomplished.
4. If you need clarification from the user, call `ask_human` with your question.
5. If you encounter an error, try an alternative approach before giving up.
6. Think step-by-step. Explain your reasoning before acting.

Host Filesystem Strategy:
- Use project_recall(name) FIRST to check for cached project context.
- Use host_tree(path) ONCE for full directory tree — never call host_list repeatedly.
- Use host_find(pattern) or host_search(query, path) before broad reads to narrow scope quickly.
- Use host_batch_read(paths) for grouped file reads instead of repeated host_read calls.
- host_write requires human approval.

Progress: Step {step_index}/{total_steps} | Iteration {iteration}/{max_iterations}

Previous observations for this step:
{observations}
"""


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
    ):
        self._provider = provider
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
        self._metrics = MetricsCollector(model=model)

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
                    # Extract key terms from the goal for graph querying
                    goal_terms = [w for w in task.goal.split() if len(w) > 3][:5]
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
                async for event in self._reason_and_act(task, step):
                    yield event

                    # Check if we need to pause for approval
                    if event.type == TaskEventType.APPROVAL_NEEDED:
                        task.status = TaskStatus.WAITING_APPROVAL
                        await self._persistence.update_task(task)

                        # Wait for approval (this blocks until resolved)
                        approved = await self._wait_for_approval(task)
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
                        # Try retry (max 3 attempts per step)
                        if step.attempts < 3:
                            step.status = StepStatus.IN_PROGRESS
                            step.attempts += 1
                            logger.info(
                                f"Retrying step {step.id} (attempt {step.attempts})"
                            )
                        else:
                            yield TaskEvent(
                                type=TaskEventType.TASK_FAILED,
                                task_id=task.id,
                                step_id=step.id,
                                content=step.error or "Step failed after 3 attempts",
                                progress=self._progress(task),
                            )
                            task.status = TaskStatus.FAILED
                            task.error = f"Step '{step.description}' failed: {step.error}"
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
                    from agent.memory_graph import extract_entities_llm
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

    async def _execute_tool_with_retry(
        self,
        tool_call: ToolCall,
        tool_context: dict,
        max_attempts: int = RETRY_MAX_ATTEMPTS,
    ) -> "ToolResult":
        """
        Execute a tool with exponential backoff retry for transient failures.

        Retries on network errors, timeouts, and rate limits. Does NOT retry
        on validation errors or intentional failures.
        """
        last_result = None
        for attempt in range(max_attempts):
            result = await self._tools.execute(tool_call, context=tool_context)
            last_result = result

            if result.success:
                # Record successful execution in metrics
                self._metrics.record_tool_execution(
                    tool_name=tool_call.name,
                    execution_time_ms=result.execution_time_ms,
                    success=True,
                )
                return result

            # Determine if this is a retryable failure
            error_lower = (result.error or "").lower()
            is_transient = any(kw in error_lower for kw in [
                "timeout", "rate limit", "connection", "network",
                "503", "502", "429", "temporarily unavailable",
            ])

            if not is_transient or attempt == max_attempts - 1:
                self._metrics.record_tool_execution(
                    tool_name=tool_call.name,
                    execution_time_ms=result.execution_time_ms,
                    success=False,
                )
                return result

            # Exponential backoff: 1s, 2s, 4s
            delay = RETRY_BASE_DELAY_S * (2 ** attempt)
            logger.info(
                f"Retrying {tool_call.name} after transient failure "
                f"(attempt {attempt + 1}/{max_attempts}, delay {delay:.1f}s): "
                f"{result.error[:100]}"
            )
            await asyncio.sleep(delay)

        return last_result

    async def _execute_tools_parallel(
        self,
        parsed_calls: list[dict],
        task: AgentTask,
        step: Any,
    ) -> AsyncIterator[TaskEvent]:
        """
        Execute multiple independent tool calls concurrently.

        Tools that require approval or are control tools (task_complete,
        ask_human) are executed sequentially to maintain correct ordering.
        """
        # Separate into parallelizable and sequential calls
        parallel_batch = []
        sequential_queue = []

        for tc_data in parsed_calls:
            func = tc_data.get("function", {})
            tool_name = func.get("name", "")
            try:
                tool_args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                tool_args = {}

            # Control tools and tools needing approval must run sequentially
            is_control = tool_name in ("task_complete", "ask_human")
            needs_approval = self._guardrails.needs_approval(
                tool_name, tool_args, task.config,
                tool_registry=self._tools,
            )

            if is_control or needs_approval:
                sequential_queue.append(tc_data)
            else:
                parallel_batch.append(tc_data)

        # ── Execute parallel batch concurrently ──────────────────
        if len(parallel_batch) > 1:
            logger.info(
                f"Parallel tool dispatch: {len(parallel_batch)} tools "
                f"({', '.join(tc.get('function', {}).get('name', '?') for tc in parallel_batch)})"
            )

            semaphore = asyncio.Semaphore(MAX_PARALLEL_TOOLS)

            async def _run_one(tc_data: dict) -> tuple[dict, ToolCall, "ToolResult"]:
                async with semaphore:
                    func = tc_data.get("function", {})
                    tool_name = func.get("name", "")
                    try:
                        tool_args = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        tool_args = {}

                    tool_call = ToolCall(
                        id=tc_data.get("id", "call"),
                        name=tool_name,
                        arguments=tool_args,
                    )
                    tool_context = {"workspace_id": task.workspace_id} if task.workspace_id else {}
                    result = await self._execute_tool_with_retry(tool_call, tool_context)
                    return tc_data, tool_call, result

            results = await asyncio.gather(
                *(_run_one(tc) for tc in parallel_batch),
                return_exceptions=True,
            )

            for item in results:
                if isinstance(item, Exception):
                    logger.error(f"Parallel tool execution error: {item}")
                    continue

                tc_data, tool_call, result = item
                func = tc_data.get("function", {})
                tool_name = func.get("name", "")
                try:
                    tool_args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_args = {}

                task.tool_calls_count += 1

                # Record evidence
                if self._evidence_chain:
                    self._evidence_chain.record_tool_decision(
                        tool_name=tool_name,
                        args=tool_args,
                        reasoning=f"LLM selected {tool_name} (parallel batch) for step: {step.description[:80]}",
                    )

                # Record in step history
                step.tool_calls.append({
                    "id": tool_call.id,
                    "tool": tool_name,
                    "args": tool_args,
                    "result": result.output if result.success else result.error,
                    "success": result.success,
                    "time_ms": result.execution_time_ms,
                    **({"_gemini_raw_part": tc_data["_gemini_raw_part"]} if "_gemini_raw_part" in tc_data else {}),
                })

                yield TaskEvent(
                    type=TaskEventType.TOOL_CALLED,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_args=json.dumps(tool_args),
                    progress=self._progress(task),
                )
                yield TaskEvent(
                    type=TaskEventType.TOOL_RESULT,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_result=result.output if result.success else result.error,
                    progress=self._progress(task),
                )

                if not result.success:
                    step.error = result.error

                # Budget check after parallel batch
                budget_error = self._guardrails.check_budget(task)
                if budget_error:
                    logger.warning(f"Budget exceeded during parallel tools: {budget_error}")
                    step.status = StepStatus.COMPLETE
                    step.result = f"Stopped: {budget_error}"
                    step.completed_at = datetime.now(timezone.utc)
                    await self._persistence.update_task(task)
                    return

        elif len(parallel_batch) == 1:
            # Single tool — just add to sequential queue
            sequential_queue = parallel_batch + sequential_queue

        # ── Execute sequential calls one at a time ───────────────
        for tc_data in sequential_queue:
            async for event in self._execute_single_tool(tc_data, task, step):
                yield event
                # Check if step was completed by a control tool
                if step.status in (StepStatus.COMPLETE, StepStatus.FAILED, StepStatus.SKIPPED):
                    return

        await self._persistence.update_task(task)

    async def _execute_single_tool(
        self,
        tc_data: dict,
        task: AgentTask,
        step: Any,
    ) -> AsyncIterator[TaskEvent]:
        """Execute a single tool call with full guardrail and control-flow handling."""
        func = tc_data.get("function", {})
        tool_name = func.get("name", "")
        try:
            tool_args = json.loads(func.get("arguments", "{}"))
        except json.JSONDecodeError:
            tool_args = {}

        tool_call = ToolCall(
            id=tc_data.get("id", "call"),
            name=tool_name,
            arguments=tool_args,
        )

        # Check guardrails
        approval_needed = self._guardrails.needs_approval(
            tool_name, tool_args, task.config,
            tool_registry=self._tools,
        )

        if approval_needed:
            request = ApprovalRequest(
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                tool_args=tool_args,
                risk_level=self._tools.get_risk_level(tool_name),
                reason=approval_needed,
            )
            task.pending_approval = request
            await self._persistence.save_approval(request)

            yield TaskEvent(
                type=TaskEventType.APPROVAL_NEEDED,
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                tool_args=json.dumps(tool_args),
                approval_id=request.id,
                content=approval_needed,
                progress=self._progress(task),
            )
            return

        # Emit tool_called event
        yield TaskEvent(
            type=TaskEventType.TOOL_CALLED,
            task_id=task.id,
            step_id=step.id,
            tool_name=tool_name,
            tool_args=json.dumps(tool_args),
            progress=self._progress(task),
        )

        # Record tool decision in evidence chain
        if self._evidence_chain:
            self._evidence_chain.record_tool_decision(
                tool_name=tool_name,
                args=tool_args,
                reasoning=f"LLM selected {tool_name} for step: {step.description[:80]}",
            )

        # Execute with retry
        tool_context = {"workspace_id": task.workspace_id} if task.workspace_id else {}
        result = await self._execute_tool_with_retry(tool_call, tool_context)
        task.tool_calls_count += 1

        # Inline budget check
        budget_error = self._guardrails.check_budget(task)
        if budget_error:
            logger.warning(f"Budget exceeded mid-step: {budget_error}")
            step.status = StepStatus.COMPLETE
            step.result = f"Stopped: {budget_error}"
            step.completed_at = datetime.now(timezone.utc)
            await self._persistence.update_task(task)
            yield TaskEvent(
                type=TaskEventType.TOOL_RESULT,
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                tool_result=result.output if result.success else result.error,
                progress=self._progress(task),
            )
            return

        # Record in step history
        step.tool_calls.append({
            "id": tool_call.id,
            "tool": tool_name,
            "args": tool_args,
            "result": result.output if result.success else result.error,
            "success": result.success,
            "time_ms": result.execution_time_ms,
            **({"_gemini_raw_part": tc_data["_gemini_raw_part"]} if "_gemini_raw_part" in tc_data else {}),
        })

        yield TaskEvent(
            type=TaskEventType.TOOL_RESULT,
            task_id=task.id,
            step_id=step.id,
            tool_name=tool_name,
            tool_result=result.output if result.success else result.error,
            progress=self._progress(task),
        )

        # Handle control tools
        if tool_name == "task_complete":
            step.status = StepStatus.COMPLETE
            step.result = tool_args.get("summary", result.output)
            step.completed_at = datetime.now(timezone.utc)
            for remaining in task.plan.steps:
                if remaining.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS) and remaining.id != step.id:
                    remaining.status = StepStatus.SKIPPED
                    remaining.result = "Skipped — task completed early"
                    remaining.completed_at = datetime.now(timezone.utc)

        elif tool_name == "ask_human":
            question = tool_args.get("question", "The agent needs your input")
            approval_request = ApprovalRequest(
                id=str(uuid.uuid4()),
                task_id=task.id,
                step_id=step.id,
                tool_name="ask_human",
                tool_args=tool_args,
                reason=question,
            )
            task.pending_approval = approval_request
            task.status = TaskStatus.WAITING_APPROVAL
            await self._persistence.save_approval(approval_request)
            await self._persistence.update_task(task)

            yield TaskEvent(
                type=TaskEventType.APPROVAL_NEEDED,
                task_id=task.id,
                step_id=step.id,
                tool_name="ask_human",
                content=question,
                approval_id=approval_request.id,
                progress=self._progress(task),
            )

            approved = await self._wait_for_approval(task)
            task.status = TaskStatus.RUNNING

            if not approved:
                step.result = "User did not respond / declined"
                step.status = StepStatus.COMPLETE
                step.completed_at = datetime.now(timezone.utc)

        elif not result.success:
            step.error = result.error

        await self._persistence.update_task(task)

    async def _reason_and_act(
        self,
        task: AgentTask,
        step: Any,
    ) -> AsyncIterator[TaskEvent]:
        """
        One iteration of the ReAct loop:
        1. Build prompt with current observations
        2. Call LLM with available tools
        3. If LLM returns multiple tool calls → dispatch in parallel
        4. If LLM returns a single tool call → execute with retry
        5. If LLM returns text → treat as thinking/reflection
        """
        # Build the agent prompt
        observations = "\n".join(
            f"[{tc.get('tool', '?')}] → {tc.get('result', '?')}"
            for tc in step.tool_calls
        ) or "(none yet)"

        done, total = task.plan.progress

        # If the task has pre-built messages (from chat), use those
        # Otherwise, build agent-specific messages
        if task.messages:
            messages = list(task.messages)  # Copy to avoid mutation
            # Add tool call history from this step if any
            for tc in step.tool_calls[-6:]:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": tc.get("id", "call_1"),
                        "type": "function",
                        "function": {
                            "name": tc.get("tool", ""),
                            "arguments": json.dumps(tc.get("args", {})),
                        },
                        # Preserve Gemini raw part (includes thought_signature)
                        **({"_gemini_raw_part": tc["_gemini_raw_part"]} if "_gemini_raw_part" in tc else {}),
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", "call_1"),
                    "content": tc.get("result", ""),
                })
        else:
            system_prompt = AGENT_SYSTEM_PROMPT.format(
                goal=task.goal,
                step_description=step.description,
                step_index=step.index + 1,
                total_steps=total,
                iteration=task.iterations,
                max_iterations=task.config.max_iterations,
                observations=observations,
            )

            # Build messages: system + conversation history for this step
            messages = [{"role": "system", "content": system_prompt}]

            # Add step-specific messages
            if not step.tool_calls:
                messages.append({
                    "role": "user",
                    "content": f"Execute this step: {step.description}",
                })
            else:
                # Replay tool call history for context
                messages.append({
                    "role": "user",
                    "content": f"Continue executing: {step.description}",
                })
                for tc in step.tool_calls[-6:]:  # Last 6 tool calls for context
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": tc.get("id", "call_1"),
                            "type": "function",
                            "function": {
                                "name": tc.get("tool", ""),
                                "arguments": json.dumps(tc.get("args", {})),
                            },
                            # Preserve Gemini raw part (includes thought_signature)
                            **({"_gemini_raw_part": tc["_gemini_raw_part"]} if "_gemini_raw_part" in tc else {}),
                        }],
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", "call_1"),
                        "content": tc.get("result", ""),
                    })

        # Get available tools as OpenAI function schemas
        tool_schemas = [t.to_openai_schema() for t in self._tools.list_tools()]

        # Call LLM with function calling
        response = await self._provider.generate_with_tools(
            messages=messages,
            model=self._model,
            tools=tool_schemas,
            temperature=0.2,
            max_tokens=4096,
            api_key=self._api_key,
        )

        # Track LLM token usage in metrics
        if response.get("usage"):
            usage = response["usage"]
            self._metrics.record_llm_call(
                model=self._model,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                cached_tokens=usage.get("cached_tokens", 0),
            )
            if self._event_callback:
                await self._event_callback("metrics_update", self._metrics.metrics.to_compact_dict())

        # ── Handle LLM response ─────────────────────────────────
        if response.get("tool_calls"):
            tool_calls = response["tool_calls"]

            # Parallel dispatch when multiple independent tools are requested
            if len(tool_calls) > 1:
                logger.info(f"LLM returned {len(tool_calls)} tool calls — dispatching in parallel")
                async for event in self._execute_tools_parallel(tool_calls, task, step):
                    yield event
            else:
                # Single tool call — execute directly
                async for event in self._execute_single_tool(tool_calls[0], task, step):
                    yield event

        elif response.get("content"):
            text = response["content"]

            # In chat mode (task.messages is set), a plain text response
            # IS the final answer — not intermediate thinking.
            if task.messages:
                step.status = StepStatus.COMPLETE
                step.result = text
                step.completed_at = datetime.now(timezone.utc)
                await self._persistence.update_task(task)
            else:
                # Autonomous task mode — text without a tool call is thinking
                yield TaskEvent(
                    type=TaskEventType.THINKING,
                    task_id=task.id,
                    step_id=step.id,
                    content=text,
                    progress=self._progress(task),
                )

                # If the LLM is done thinking without a tool call,
                # it might mean the step is simple enough to complete directly
                if any(phrase in text.lower() for phrase in [
                    "step is complete",
                    "this step is done",
                    "completed this step",
                    "no tools needed",
                ]):
                    step.status = StepStatus.COMPLETE
                    step.result = text
                    step.completed_at = datetime.now(timezone.utc)
                    await self._persistence.update_task(task)

    async def _wait_for_approval(self, task: AgentTask) -> bool:
        """
        Block until the pending approval is resolved.
        Returns True if approved, False if denied/expired.
        """
        if not task.pending_approval:
            return True

        approval = task.pending_approval
        max_wait = 1800  # 30 minutes
        poll_interval = 2  # seconds

        elapsed = 0
        while elapsed < max_wait:
            # Check approval status from persistence
            updated = await self._persistence.get_approval(approval.id)
            if updated and updated.status != ApprovalStatus.PENDING:
                task.pending_approval = None
                return updated.status == ApprovalStatus.APPROVED

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        # Expired
        task.pending_approval = None
        return False

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
    ) -> None:
        """Resolve an approval request."""
        raise NotImplementedError
