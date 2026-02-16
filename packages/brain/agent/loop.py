"""
Agent Loop Engine — ReAct (Reason + Act) state machine for autonomous execution.

This is the heart of Kestrel's autonomous agent. It orchestrates:
  1. Planning — decompose the goal into steps
  2. Executing — run tools and observe results
  3. Reflecting — decide next action based on observations
  4. Completing — summarize results and report

The loop is fully resumable: all state is persisted to PostgreSQL,
so tasks survive service restarts and can be paused/resumed.
"""

import asyncio
import json
import logging
import time
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
    TaskStatus,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent.planner import TaskPlanner
from agent.learner import TaskLearner

logger = logging.getLogger("brain.agent.loop")


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
    ):
        self._provider = provider
        self._tools = tool_registry
        self._guardrails = guardrails
        self._persistence = persistence
        self._model = model
        self._planner = TaskPlanner(provider, model)
        self._learner = learner
        self._checkpoints = checkpoint_manager

        # Callback for approval resolution (set by the gRPC handler)
        self._approval_callback: Optional[Callable] = None

    async def run(self, task: AgentTask) -> AsyncIterator[TaskEvent]:
        """
        Execute an agent task, yielding events as they occur.

        This is the main entry point. It runs the full planning → execution
        → reflection loop until the task is complete, fails, or is paused.
        """
        start_time = time.monotonic()

        try:
            # ── Phase 0: Enrich with Past Lessons ────────────────
            lesson_context = ""
            if self._learner:
                try:
                    lesson_context = await self._learner.enrich_context(
                        workspace_id=task.workspace_id,
                        goal=task.goal,
                    )
                except Exception as e:
                    logger.warning(f"Lesson enrichment failed: {e}")

            # ── Phase 1: Planning ────────────────────────────────
            if task.status == TaskStatus.PLANNING:
                task.status = TaskStatus.PLANNING
                await self._persistence.update_task(task)

                context = self._build_context(task)
                if lesson_context:
                    context += f"\n\n{lesson_context}"

                plan = await self._planner.create_plan(
                    goal=task.goal,
                    available_tools=self._tools.list_tools(),
                    context=context,
                )
                task.plan = plan
                await self._persistence.update_task(task)

                yield TaskEvent(
                    type=TaskEventType.PLAN_CREATED,
                    task_id=task.id,
                    content=json.dumps(plan.to_dict()),
                    progress=self._progress(task),
                )

            # ── Phase 2: Execution Loop ──────────────────────────
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
                if (
                    step
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
                    results.append(f"**{s.description}**: {s.result}")
            task.result = "\n".join(results) if results else "Task completed successfully."

            await self._persistence.update_task(task)

            yield TaskEvent(
                type=TaskEventType.TASK_COMPLETE,
                task_id=task.id,
                content=task.result,
                progress=self._progress(task),
            )

            # ── Phase 4: Learn from this task ────────────────────
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

    async def _reason_and_act(
        self,
        task: AgentTask,
        step: Any,
    ) -> AsyncIterator[TaskEvent]:
        """
        One iteration of the ReAct loop:
        1. Build prompt with current observations
        2. Call LLM with available tools
        3. If LLM returns a tool call → execute it
        4. If LLM returns text → treat as thinking/reflection
        """
        # Build the agent prompt
        observations = "\n".join(
            f"[{tc.get('tool', '?')}] → {tc.get('result', '?')}"
            for tc in step.tool_calls
        ) or "(none yet)"

        done, total = task.plan.progress
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
        )

        # ── Handle LLM response ─────────────────────────────────
        if response.get("tool_calls"):
            for tc_data in response["tool_calls"]:
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
                    return  # Pause execution until approved

                # Emit tool_called event
                yield TaskEvent(
                    type=TaskEventType.TOOL_CALLED,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_args=json.dumps(tool_args),
                    progress=self._progress(task),
                )

                # Execute the tool
                result = await self._tools.execute(tool_call)
                task.tool_calls_count += 1

                # Record in step history
                step.tool_calls.append({
                    "id": tool_call.id,
                    "tool": tool_name,
                    "args": tool_args,
                    "result": result.output if result.success else result.error,
                    "success": result.success,
                    "time_ms": result.execution_time_ms,
                })

                yield TaskEvent(
                    type=TaskEventType.TOOL_RESULT,
                    task_id=task.id,
                    step_id=step.id,
                    tool_name=tool_name,
                    tool_result=result.output if result.success else result.error,
                    progress=self._progress(task),
                )

                # Check for special control tools
                if tool_name == "task_complete":
                    step.status = StepStatus.COMPLETE
                    step.result = tool_args.get("summary", result.output)
                    step.completed_at = datetime.now(timezone.utc)

                elif not result.success:
                    step.error = result.error
                    # Don't mark failed yet — the loop will handle retries

                await self._persistence.update_task(task)

        elif response.get("content"):
            # LLM returned text (thinking/reflection)
            thinking = response["content"]
            yield TaskEvent(
                type=TaskEventType.THINKING,
                task_id=task.id,
                step_id=step.id,
                content=thinking,
                progress=self._progress(task),
            )

            # If the LLM is done thinking without a tool call,
            # it might mean the step is simple enough to complete directly
            if any(phrase in thinking.lower() for phrase in [
                "step is complete",
                "this step is done",
                "completed this step",
                "no tools needed",
            ]):
                step.status = StepStatus.COMPLETE
                step.result = thinking
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
