"""
Eval Runner — executes evaluation scenarios and collects results.

The runner creates isolated agent tasks for each scenario, runs them
through the full agent loop, and captures metrics including:
  - Success/failure status
  - Iteration count
  - Tool call count
  - Token usage
  - Wall-clock time
  - Verifier pass/fail
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from evals.scenarios import EvalScenario

logger = logging.getLogger("brain.evals.runner")


@dataclass
class EvalResult:
    """Result of a single evaluation scenario run."""
    scenario_id: str
    scenario_name: str
    success: bool
    iterations: int = 0
    tool_calls: int = 0
    token_usage: int = 0
    wall_time_ms: int = 0
    verifier_passed: Optional[bool] = None
    error: str = ""
    task_id: str = ""
    metrics: dict = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "success": self.success,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "token_usage": self.token_usage,
            "wall_time_ms": self.wall_time_ms,
            "verifier_passed": self.verifier_passed,
            "error": self.error,
            "task_id": self.task_id,
        }


class EvalRunner:
    """
    Executes evaluation scenarios through the agent loop and collects results.

    Usage:
        runner = EvalRunner(loop_factory=create_agent_loop, persistence=persistence)
        results = await runner.run_suite(scenarios)
    """

    def __init__(self, loop_factory=None, persistence=None, pool=None):
        self._loop_factory = loop_factory  # callable that returns an AgentLoop
        self._persistence = persistence
        self._pool = pool

    async def run_scenario(self, scenario: EvalScenario) -> EvalResult:
        """Run a single evaluation scenario."""
        if not self._loop_factory:
            return EvalResult(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                success=False,
                error="No loop factory configured",
            )

        from agent.types import AgentTask, GuardrailConfig, TaskStatus, TaskEventType

        task = AgentTask(
            id=str(uuid.uuid4()),
            user_id="eval-system",
            workspace_id="eval-workspace",
            goal=scenario.goal,
            status=TaskStatus.PLANNING,
            config=GuardrailConfig(
                max_iterations=scenario.max_iterations,
                max_tool_calls=scenario.max_tool_calls,
                max_wall_time_seconds=scenario.max_wall_time_seconds,
                auto_approve_risk="medium",
            ),
        )

        loop = self._loop_factory()
        start = time.monotonic()

        try:
            if self._persistence:
                await self._persistence.save_task(task)

            async for event in loop.run(task):
                pass

            wall_time_ms = int((time.monotonic() - start) * 1000)

            result = EvalResult(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                success=task.status == TaskStatus.COMPLETE,
                iterations=task.iterations,
                tool_calls=task.tool_calls_count,
                token_usage=task.token_usage,
                wall_time_ms=wall_time_ms,
                error=task.error or "",
                task_id=task.id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        except Exception as e:
            wall_time_ms = int((time.monotonic() - start) * 1000)
            result = EvalResult(
                scenario_id=scenario.id,
                scenario_name=scenario.name,
                success=False,
                wall_time_ms=wall_time_ms,
                error=str(e),
                task_id=task.id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        # Persist result
        await self._persist_result(result)

        return result

    async def run_suite(
        self,
        scenarios: list[EvalScenario],
        parallel: bool = False,
    ) -> list[EvalResult]:
        """Run a suite of evaluation scenarios."""
        results = []

        if parallel:
            import asyncio
            tasks = [self.run_scenario(s) for s in scenarios]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # Convert exceptions to failed results
            final = []
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    final.append(EvalResult(
                        scenario_id=scenarios[i].id,
                        scenario_name=scenarios[i].name,
                        success=False,
                        error=str(r),
                    ))
                else:
                    final.append(r)
            return final
        else:
            for scenario in scenarios:
                result = await self.run_scenario(scenario)
                results.append(result)
                logger.info(
                    f"Eval [{scenario.id}] {'PASS' if result.success else 'FAIL'} "
                    f"({result.wall_time_ms}ms, {result.iterations} iters)"
                )

        return results

    async def _persist_result(self, result: EvalResult) -> None:
        """Save eval result to the database."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO eval_runs (
                        scenario_id, scenario_name, success,
                        iterations, tool_calls, token_usage,
                        wall_time_ms, verifier_passed, metrics_json
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    result.scenario_id,
                    result.scenario_name,
                    result.success,
                    result.iterations,
                    result.tool_calls,
                    result.token_usage,
                    result.wall_time_ms,
                    result.verifier_passed,
                    json.dumps(result.metrics),
                )
        except Exception as e:
            logger.debug(f"Eval result persistence failed: {e}")
