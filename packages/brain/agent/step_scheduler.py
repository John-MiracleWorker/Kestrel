"""
Step Scheduler — parallel execution of independent plan steps.

Analyzes the TaskPlan DAG and executes groups of independent steps
concurrently using asyncio.gather, while respecting:
  - Step dependencies (DAG edges)
  - Risk-based parallelism limits (HIGH-risk tools run sequentially)
  - Budget constraints (iterations, tokens, wall-clock time)

This replaces the sequential while-loop in loop.py for plans that
have parallelizable steps, reducing wall-clock time significantly.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Optional

from agent.types import (
    AgentTask,
    RiskLevel,
    StepStatus,
    TaskEvent,
    TaskEventType,
    TaskPlan,
    TaskStep,
)

logger = logging.getLogger("brain.agent.step_scheduler")


class StepScheduler:
    """
    Executes plan steps respecting the dependency DAG, running independent
    steps in parallel where safe to do so.

    Usage:
        scheduler = StepScheduler(executor=executor, planner=planner, ...)
        async for event in scheduler.execute_plan(task, ...):
            yield event
    """

    def __init__(
        self,
        executor,
        planner,
        guardrails,
        persistence,
        tool_registry,
        learner=None,
        event_callback=None,
        max_parallel_steps: int = 3,
    ):
        self._executor = executor
        self._planner = planner
        self._guardrails = guardrails
        self._persistence = persistence
        self._tools = tool_registry
        self._learner = learner
        self._event_callback = event_callback
        self._max_parallel = max_parallel_steps

    def _get_ready_steps(self, plan: TaskPlan) -> list[TaskStep]:
        """Return all pending or in-progress steps whose dependencies are satisfied.

        IN_PROGRESS steps are included so the scheduler re-runs them if
        the executor returned without completing the step (e.g. text-only
        LLM response that didn't match done-criteria on the first pass).
        """
        completed_ids = {
            s.id for s in plan.steps
            if s.status in (StepStatus.COMPLETE, StepStatus.SKIPPED)
        }
        ready = []
        for step in plan.steps:
            if step.status not in (StepStatus.PENDING, StepStatus.IN_PROGRESS):
                continue
            deps_met = all(dep in completed_ids for dep in (step.depends_on or []))
            if deps_met:
                ready.append(step)
        return ready

    def _cap_parallelism(self, steps: list[TaskStep]) -> int:
        """Determine max parallel steps based on risk levels of tools involved."""
        has_high = False
        for step in steps:
            for tc in step.tool_calls:
                tool_name = tc.get("tool", tc.get("function", {}).get("name", ""))
                if tool_name:
                    risk = self._tools.get_risk_level(tool_name)
                    if risk == RiskLevel.HIGH:
                        has_high = True
                        break
            # Also check expected_tools for pre-planned risk assessment
            for tool_name in step.expected_tools:
                risk = self._tools.get_risk_level(tool_name)
                if risk == RiskLevel.HIGH:
                    has_high = True
                    break
            if has_high:
                break

        if has_high:
            return 1  # Sequential for HIGH-risk tools
        return self._max_parallel

    async def execute_plan(
        self,
        task: AgentTask,
        start_time: float,
        should_replan_fn: Callable,
        progress_fn: Callable,
    ) -> AsyncIterator[TaskEvent]:
        """
        Execute all steps in a task plan, yielding events as they occur.

        Uses parallel execution for independent steps and falls back to
        sequential execution when dependencies require it.
        """
        while not task.plan.is_complete:
            ready_steps = self._get_ready_steps(task.plan)
            if not ready_steps:
                break

            # Determine parallelism limit
            parallel_limit = self._cap_parallelism(ready_steps)
            batch = ready_steps[:parallel_limit]

            if len(batch) > 1:
                logger.info(
                    f"Parallel execution: {len(batch)} steps "
                    f"(limit={parallel_limit})"
                )
                if self._event_callback:
                    await self._event_callback("parallel_steps_started", {
                        "count": len(batch),
                        "steps": [s.description[:80] for s in batch],
                    })

            if len(batch) == 1:
                # Sequential path — single step
                async for event in self._execute_single_step(
                    task, batch[0], start_time, progress_fn
                ):
                    yield event
                    if event.type == TaskEventType.TASK_FAILED:
                        return
            else:
                # Parallel path — multiple independent steps
                async for event in self._execute_parallel_steps(
                    task, batch, start_time, progress_fn
                ):
                    yield event
                    if event.type == TaskEventType.TASK_FAILED:
                        return

            # Budget check after each batch
            task.iterations += 1
            budget_error = self._guardrails.check_budget(task)
            if budget_error:
                task.status = __import__("agent.types", fromlist=["TaskStatus"]).TaskStatus.FAILED
                task.error = budget_error
                await self._persistence.update_task(task)
                yield TaskEvent(
                    type=TaskEventType.TASK_FAILED,
                    task_id=task.id,
                    content=budget_error,
                    progress=progress_fn(task),
                )
                return

            # Wall-clock check
            elapsed = time.monotonic() - start_time
            if elapsed > task.config.max_wall_time_seconds:
                from agent.types import TaskStatus
                task.status = TaskStatus.FAILED
                task.error = f"Wall-clock time limit exceeded ({int(elapsed)}s)"
                await self._persistence.update_task(task)
                yield TaskEvent(
                    type=TaskEventType.TASK_FAILED,
                    task_id=task.id,
                    content=task.error,
                    progress=progress_fn(task),
                )
                return

            # Check if we should replan
            last_completed = None
            for s in reversed(task.plan.steps):
                if s.status == StepStatus.COMPLETE:
                    last_completed = s
                    break

            budget_ok = self._guardrails.check_budget(task) is None
            if (
                budget_ok
                and last_completed
                and last_completed.status == StepStatus.COMPLETE
                and should_replan_fn(task)
            ):
                from agent.types import TaskStatus
                task.status = TaskStatus.REFLECTING
                await self._persistence.update_task(task)

                revised = await self._planner.revise_plan(
                    plan=task.plan,
                    observations=last_completed.result or "",
                    available_tools=self._tools.list_tools(),
                )
                task.plan = revised
                task.status = TaskStatus.EXECUTING
                await self._persistence.update_task(task)

        # ── Deadlock detection ──────────────────────────────────────
        # If we exit the while loop but the plan isn't complete, some
        # steps are stuck (e.g. FAILED steps that exhausted retries,
        # or IN_PROGRESS steps with unsatisfiable dependencies).
        # Force-resolve them so the task doesn't hang silently.
        if not task.plan.is_complete:
            stuck_steps = [
                s for s in task.plan.steps
                if s.status in (StepStatus.IN_PROGRESS, StepStatus.PENDING)
            ]
            if stuck_steps:
                logger.warning(
                    f"Plan deadlock: {len(stuck_steps)} step(s) stuck after "
                    f"scheduler loop exited: "
                    f"{[f'{s.id}({s.status.value})' for s in stuck_steps]}"
                )
                for s in stuck_steps:
                    if s.result:
                        # Step has partial results — treat as complete
                        s.status = StepStatus.COMPLETE
                        s.completed_at = datetime.now(timezone.utc)
                    else:
                        s.status = StepStatus.FAILED
                        s.error = s.error or "Step could not be completed (scheduler deadlock)"
                await self._persistence.update_task(task)

                # If any were force-failed, propagate task failure
                force_failed = [s for s in stuck_steps if s.status == StepStatus.FAILED]
                if force_failed:
                    from agent.types import TaskStatus
                    task.status = TaskStatus.FAILED
                    task.error = (
                        f"{len(force_failed)} step(s) could not be completed: "
                        + "; ".join(s.description[:60] for s in force_failed[:3])
                    )
                    await self._persistence.update_task(task)
                    yield TaskEvent(
                        type=TaskEventType.TASK_FAILED,
                        task_id=task.id,
                        content=task.error,
                        progress=progress_fn(task),
                    )

    async def _execute_single_step(
        self,
        task: AgentTask,
        step: TaskStep,
        start_time: float,
        progress_fn: Callable,
    ) -> AsyncIterator[TaskEvent]:
        """Execute a single step (same logic as the original sequential loop)."""
        if step.status == StepStatus.PENDING:
            step.status = StepStatus.IN_PROGRESS
            step.started_at = datetime.now(timezone.utc)
            await self._persistence.update_task(task)

            yield TaskEvent(
                type=TaskEventType.STEP_STARTED,
                task_id=task.id,
                step_id=step.id,
                content=step.description,
                progress=progress_fn(task),
            )

        # Run the step via the executor
        async for event in self._executor.run_step(task, step):
            yield event

            # Handle approval
            if event.type == TaskEventType.APPROVAL_NEEDED:
                from agent.types import TaskStatus
                task.status = TaskStatus.WAITING_APPROVAL
                await self._persistence.update_task(task)

                approved = await self._executor._wait_for_approval(task)
                if not approved:
                    step.status = StepStatus.SKIPPED
                    step.result = "Skipped — human denied approval"
                    task.status = TaskStatus.EXECUTING
                    await self._persistence.update_task(task)
                    return

                task.status = TaskStatus.EXECUTING
                await self._persistence.update_task(task)

            # Step completed
            if step.status == StepStatus.COMPLETE:
                yield TaskEvent(
                    type=TaskEventType.STEP_COMPLETE,
                    task_id=task.id,
                    step_id=step.id,
                    content=step.result or "",
                    progress=progress_fn(task),
                )
                # Capture recovery pattern for online learning
                if step.attempts > 1 and self._learner:
                    try:
                        from agent.learner import Lesson
                        recovery_lesson = Lesson(
                            category="pattern",
                            summary=f"Recovery: {step.description[:80]}",
                            details=(
                                f"Step failed {step.attempts - 1} time(s) before succeeding. "
                                f"Error was: {(step.error or 'unknown')[:200]}"
                            ),
                            tools_used=[],
                            success=True,
                            confidence=0.7,
                            tags=["mid_execution", "recovery"],
                            source_task_id=task.id,
                        )
                        await self._learner._store_lessons(
                            task.workspace_id, [recovery_lesson]
                        )
                    except Exception as e:
                        logger.debug(f"Mid-execution lesson capture failed: {e}")
                return

            # Step failed — retry logic
            if step.status == StepStatus.FAILED:
                if step.attempts < 3:
                    step.status = StepStatus.IN_PROGRESS
                    step.attempts += 1
                    is_rate_limited = step.error and (
                        "429" in step.error or "rate limit" in step.error.lower()
                    )
                    backoff = 15 * step.attempts if is_rate_limited else 2 ** (step.attempts - 1)
                    logger.info(
                        f"Retrying step {step.id} (attempt {step.attempts}/3, "
                        f"backoff {backoff}s)"
                    )
                    await asyncio.sleep(backoff)
                else:
                    yield TaskEvent(
                        type=TaskEventType.TASK_FAILED,
                        task_id=task.id,
                        step_id=step.id,
                        content=step.error or "Step failed after 3 attempts",
                        progress=progress_fn(task),
                    )
                    from agent.types import TaskStatus
                    task.status = TaskStatus.FAILED
                    task.error = f"Step '{step.description[:80]}' failed after 3 retries: {step.error}"
                    await self._persistence.update_task(task)
                    return

        # ── Safety guard: step still IN_PROGRESS after executor finished ──
        # This happens when the LLM returns text-only content that doesn't
        # match done-criteria. The step will be re-queued by _get_ready_steps
        # on the next scheduler iteration. Log it so we have visibility.
        if step.status == StepStatus.IN_PROGRESS:
            logger.info(
                f"Step {step.id} still IN_PROGRESS after executor run "
                f"(attempts={step.attempts}, has_result={bool(step.result)})"
            )

    async def _execute_parallel_steps(
        self,
        task: AgentTask,
        steps: list[TaskStep],
        start_time: float,
        progress_fn: Callable,
    ) -> AsyncIterator[TaskEvent]:
        """Execute multiple independent steps concurrently."""
        # Mark all steps as in-progress
        for step in steps:
            step.status = StepStatus.IN_PROGRESS
            step.started_at = datetime.now(timezone.utc)

        await self._persistence.update_task(task)

        for step in steps:
            yield TaskEvent(
                type=TaskEventType.STEP_STARTED,
                task_id=task.id,
                step_id=step.id,
                content=step.description,
                progress=progress_fn(task),
            )

        # Collect events from all steps concurrently
        events_by_step: dict[str, list[TaskEvent]] = {s.id: [] for s in steps}

        async def _run_step(step: TaskStep) -> list[TaskEvent]:
            """Run a step and collect its events."""
            collected = []
            async for event in self._executor.run_step(task, step):
                collected.append(event)
            return collected

        # Run all steps concurrently
        results = await asyncio.gather(
            *[_run_step(s) for s in steps],
            return_exceptions=True,
        )

        # Process results and yield events
        any_failed = False
        for step, result in zip(steps, results):
            if isinstance(result, Exception):
                step.status = StepStatus.FAILED
                step.error = str(result)
                any_failed = True
                yield TaskEvent(
                    type=TaskEventType.TASK_FAILED,
                    task_id=task.id,
                    step_id=step.id,
                    content=f"Parallel step failed: {result}",
                    progress=progress_fn(task),
                )
                continue

            # Yield all collected events from this step
            for event in result:
                yield event

            # Check step status after execution
            if step.status == StepStatus.COMPLETE:
                yield TaskEvent(
                    type=TaskEventType.STEP_COMPLETE,
                    task_id=task.id,
                    step_id=step.id,
                    content=step.result or "",
                    progress=progress_fn(task),
                )
            elif step.status == StepStatus.FAILED:
                any_failed = True
            elif step.status == StepStatus.IN_PROGRESS:
                # Step wasn't resolved by the executor — will be
                # re-queued by _get_ready_steps on the next iteration.
                logger.info(
                    f"Parallel step {step.id} still IN_PROGRESS after "
                    f"executor run (has_result={bool(step.result)})"
                )

        await self._persistence.update_task(task)

        if any_failed:
            # Check if all steps failed (fatal) vs some (continue)
            all_failed = all(s.status == StepStatus.FAILED for s in steps)
            if all_failed:
                from agent.types import TaskStatus
                task.status = TaskStatus.FAILED
                task.error = "All parallel steps failed"
                await self._persistence.update_task(task)
                yield TaskEvent(
                    type=TaskEventType.TASK_FAILED,
                    task_id=task.id,
                    content=task.error,
                    progress=progress_fn(task),
                )
