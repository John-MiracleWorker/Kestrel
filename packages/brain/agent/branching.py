from __future__ import annotations
"""
Time-Travel Plan Branching — fork from any past checkpoint, explore
alternative strategies, compare outcomes.

Extends the existing CheckpointManager with branching semantics:
  - main branch: the original execution path
  - forks: alternative paths from any checkpoint
  - comparisons: side-by-side outcome diffs
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("brain.agent.branching")


# ── Data Models ──────────────────────────────────────────────────────


@dataclass
class Branch:
    """A divergent execution path from a checkpoint."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    name: str = ""                    # Human-readable label
    parent_branch: str = "main"       # Branch this forked from
    fork_checkpoint_id: str = ""      # Which checkpoint we forked at
    fork_step_index: int = 0
    status: str = "active"            # active, complete, abandoned
    strategy_hint: str = ""           # Why this branch exists / what to try
    outcome_summary: str = ""         # Result after running
    tools_used: list[str] = field(default_factory=list)
    total_tool_calls: int = 0
    total_tokens: int = 0
    created_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "name": self.name,
            "parent_branch": self.parent_branch,
            "fork_step_index": self.fork_step_index,
            "status": self.status,
            "strategy_hint": self.strategy_hint,
            "outcome_summary": self.outcome_summary,
            "total_tool_calls": self.total_tool_calls,
            "total_tokens": self.total_tokens,
            "created_at": self.created_at,
        }


@dataclass
class BranchComparison:
    """Side-by-side comparison of two branches."""
    branch_a: str
    branch_b: str
    winner: str = ""                  # Which branch produced better results
    reason: str = ""
    a_summary: str = ""
    b_summary: str = ""
    a_tool_calls: int = 0
    b_tool_calls: int = 0
    a_tokens: int = 0
    b_tokens: int = 0
    differences: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "branch_a": self.branch_a,
            "branch_b": self.branch_b,
            "winner": self.winner,
            "reason": self.reason,
            "a_summary": self.a_summary,
            "b_summary": self.b_summary,
            "differences": self.differences,
            "cost_comparison": {
                "a": {"tool_calls": self.a_tool_calls, "tokens": self.a_tokens},
                "b": {"tool_calls": self.b_tool_calls, "tokens": self.b_tokens},
            },
        }


# ── Branch Manager ───────────────────────────────────────────────────


class BranchManager:
    """
    Manages execution branches for a task. Works alongside CheckpointManager.

    Usage:
        mgr = BranchManager(pool)
        branch = await mgr.create_branch(task, checkpoint_id, "Try npm instead of yarn")
        comparison = await mgr.compare(branch_a_id, branch_b_id)
    """

    def __init__(self, pool=None):
        self._pool = pool
        self._branches: dict[str, Branch] = {}

    async def create_branch(
        self,
        task_id: str,
        checkpoint_id: str,
        strategy_hint: str = "",
        name: str = "",
    ) -> Branch:
        """
        Fork a new branch from a checkpoint.
        The caller is responsible for restoring the checkpoint and re-running.
        """
        branch = Branch(
            task_id=task_id,
            name=name or f"branch-{len(self._branches) + 1}",
            fork_checkpoint_id=checkpoint_id,
            strategy_hint=strategy_hint,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        self._branches[branch.id] = branch

        # Persist
        await self._persist_branch(branch)

        logger.info(
            f"Created branch '{branch.name}' from checkpoint {checkpoint_id} "
            f"for task {task_id}"
        )
        return branch

    async def complete_branch(
        self,
        branch_id: str,
        outcome_summary: str,
        tools_used: list[str] = None,
        total_tool_calls: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """Mark a branch as complete with its outcome."""
        branch = self._branches.get(branch_id)
        if not branch:
            logger.warning(f"Branch {branch_id} not found")
            return

        branch.status = "complete"
        branch.outcome_summary = outcome_summary
        branch.tools_used = tools_used or []
        branch.total_tool_calls = total_tool_calls
        branch.total_tokens = total_tokens
        branch.completed_at = datetime.now(timezone.utc).isoformat()

        await self._persist_branch(branch)
        logger.info(f"Branch '{branch.name}' completed: {outcome_summary[:80]}")

    async def list_branches(self, task_id: str) -> list[Branch]:
        """List all branches for a task."""
        return [
            b for b in self._branches.values()
            if b.task_id == task_id
        ]

    async def compare(self, branch_a_id: str, branch_b_id: str) -> BranchComparison:
        """Compare two branches side-by-side."""
        a = self._branches.get(branch_a_id)
        b = self._branches.get(branch_b_id)

        if not a or not b:
            raise ValueError("Both branches must exist for comparison")

        differences = []
        if a.tools_used != b.tools_used:
            a_only = set(a.tools_used) - set(b.tools_used)
            b_only = set(b.tools_used) - set(a.tools_used)
            if a_only:
                differences.append(f"Branch A used tools not in B: {', '.join(a_only)}")
            if b_only:
                differences.append(f"Branch B used tools not in A: {', '.join(b_only)}")

        if a.total_tool_calls != b.total_tool_calls:
            differences.append(
                f"Tool calls: A={a.total_tool_calls}, B={b.total_tool_calls}"
            )

        if a.total_tokens != b.total_tokens:
            differences.append(
                f"Token usage: A={a.total_tokens}, B={b.total_tokens}"
            )

        # Determine winner (heuristic: fewer resources + successful outcome)
        winner = ""
        reason = ""
        if a.status == "complete" and b.status != "complete":
            winner = branch_a_id
            reason = "Branch A completed successfully; Branch B did not."
        elif b.status == "complete" and a.status != "complete":
            winner = branch_b_id
            reason = "Branch B completed successfully; Branch A did not."
        elif a.total_tool_calls < b.total_tool_calls:
            winner = branch_a_id
            reason = "Branch A used fewer tool calls"
        elif b.total_tool_calls < a.total_tool_calls:
            winner = branch_b_id
            reason = "Branch B used fewer tool calls"

        return BranchComparison(
            branch_a=branch_a_id,
            branch_b=branch_b_id,
            winner=winner,
            reason=reason,
            a_summary=a.outcome_summary,
            b_summary=b.outcome_summary,
            a_tool_calls=a.total_tool_calls,
            b_tool_calls=b.total_tool_calls,
            a_tokens=a.total_tokens,
            b_tokens=b.total_tokens,
            differences=differences,
        )

    async def _persist_branch(self, branch: Branch) -> None:
        """Save branch to the database."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO task_branches
                        (id, task_id, name, parent_branch, fork_checkpoint_id,
                         fork_step_index, status, strategy_hint, outcome_summary,
                         total_tool_calls, total_tokens, created_at, completed_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status,
                        outcome_summary = EXCLUDED.outcome_summary,
                        total_tool_calls = EXCLUDED.total_tool_calls,
                        total_tokens = EXCLUDED.total_tokens,
                        completed_at = EXCLUDED.completed_at
                    """,
                    branch.id, branch.task_id, branch.name,
                    branch.parent_branch, branch.fork_checkpoint_id,
                    branch.fork_step_index, branch.status,
                    branch.strategy_hint, branch.outcome_summary,
                    branch.total_tool_calls, branch.total_tokens,
                    branch.created_at, branch.completed_at,
                )
        except Exception as e:
            logger.debug(f"Branch persistence failed: {e}")
