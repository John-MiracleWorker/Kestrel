"""
Eval Metrics — aggregation and querying of evaluation results.

Provides dashboard-ready metrics including:
  - Success rate over time
  - Average iterations per task
  - Tool usage distribution
  - Performance trends
  - Pass/fail rates by category
"""

import logging
from typing import Any, Optional

logger = logging.getLogger("brain.evals.metrics")


class EvalMetrics:
    """
    Aggregates and queries evaluation results from the database.

    Used by the metrics dashboard and auto-improvement loop.
    """

    def __init__(self, pool=None):
        self._pool = pool

    async def get_summary(self, limit: int = 100) -> dict:
        """Get a summary of recent eval runs."""
        if not self._pool:
            return {"error": "No database connection"}

        try:
            async with self._pool.acquire() as conn:
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM eval_runs"
                )
                successes = await conn.fetchval(
                    "SELECT COUNT(*) FROM eval_runs WHERE success = true"
                )
                avg_iters = await conn.fetchval(
                    "SELECT AVG(iterations) FROM eval_runs"
                )
                avg_wall = await conn.fetchval(
                    "SELECT AVG(wall_time_ms) FROM eval_runs"
                )
                avg_tokens = await conn.fetchval(
                    "SELECT AVG(token_usage) FROM eval_runs WHERE token_usage > 0"
                )

            return {
                "total_runs": total or 0,
                "successes": successes or 0,
                "success_rate": round((successes or 0) / max(total or 1, 1), 3),
                "avg_iterations": round(float(avg_iters or 0), 1),
                "avg_wall_time_ms": round(float(avg_wall or 0), 0),
                "avg_token_usage": round(float(avg_tokens or 0), 0),
            }
        except Exception as e:
            logger.error(f"Eval metrics query failed: {e}")
            return {"error": str(e)}

    async def get_by_scenario(self) -> list[dict]:
        """Get per-scenario breakdown of eval results."""
        if not self._pool:
            return []

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        scenario_id,
                        scenario_name,
                        COUNT(*) as runs,
                        SUM(CASE WHEN success THEN 1 ELSE 0 END) as successes,
                        AVG(iterations) as avg_iterations,
                        AVG(wall_time_ms) as avg_wall_time,
                        AVG(token_usage) as avg_tokens
                    FROM eval_runs
                    GROUP BY scenario_id, scenario_name
                    ORDER BY scenario_id
                    """
                )

            return [
                {
                    "scenario_id": row["scenario_id"],
                    "scenario_name": row["scenario_name"],
                    "runs": row["runs"],
                    "successes": row["successes"],
                    "success_rate": round(row["successes"] / max(row["runs"], 1), 3),
                    "avg_iterations": round(float(row["avg_iterations"] or 0), 1),
                    "avg_wall_time_ms": round(float(row["avg_wall_time"] or 0), 0),
                    "avg_token_usage": round(float(row["avg_tokens"] or 0), 0),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Eval metrics by scenario failed: {e}")
            return []

    async def get_trend(self, days: int = 30) -> list[dict]:
        """Get daily performance trend over the last N days."""
        if not self._pool:
            return []

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        DATE(created_at) as day,
                        COUNT(*) as runs,
                        SUM(CASE WHEN success THEN 1 ELSE 0 END) as successes,
                        AVG(iterations) as avg_iterations,
                        AVG(wall_time_ms) as avg_wall_time
                    FROM eval_runs
                    WHERE created_at > now() - $1 * interval '1 day'
                    GROUP BY DATE(created_at)
                    ORDER BY day
                    """,
                    days,
                )

            return [
                {
                    "day": str(row["day"]),
                    "runs": row["runs"],
                    "successes": row["successes"],
                    "success_rate": round(row["successes"] / max(row["runs"], 1), 3),
                    "avg_iterations": round(float(row["avg_iterations"] or 0), 1),
                    "avg_wall_time_ms": round(float(row["avg_wall_time"] or 0), 0),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Eval trend query failed: {e}")
            return []

    async def get_tool_distribution(self) -> list[dict]:
        """Get tool usage distribution across eval runs."""
        if not self._pool:
            return []

        try:
            async with self._pool.acquire() as conn:
                # Query from agent_tasks that were created by eval runs
                rows = await conn.fetch(
                    """
                    SELECT
                        scenario_id,
                        COUNT(*) as runs,
                        AVG(tool_calls) as avg_tool_calls
                    FROM eval_runs
                    GROUP BY scenario_id
                    ORDER BY avg_tool_calls DESC
                    """
                )

            return [
                {
                    "scenario_id": row["scenario_id"],
                    "runs": row["runs"],
                    "avg_tool_calls": round(float(row["avg_tool_calls"] or 0), 1),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Tool distribution query failed: {e}")
            return []
