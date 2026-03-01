"""
Approval Memory — learn from user approval patterns to reduce future interruptions.

Tracks (tool_name, args_pattern) approval history per workspace.  After a user
approves the same generalized pattern N times (default 3), future matching
tool calls are auto-approved at the INFORM tier instead of CONFIRM.

Patterns are generalized: file paths become directory wildcards, UUIDs are
replaced with placeholders, long content strings are collapsed.  This ensures
that approving "write to /project/src/foo.py" also covers "write to
/project/src/bar.py" without creating an infinite number of patterns.

Safety invariant: a single denial permanently blocks auto-approval for that
pattern (denial_count > 0 → never auto-approve).
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("brain.agent.approval_memory")

# Number of approvals before auto-approving a pattern
AUTO_APPROVE_THRESHOLD = 3

# Maximum stored patterns per workspace (prevent unbounded growth)
MAX_PATTERNS_PER_WORKSPACE = 500


@dataclass
class ApprovalPattern:
    """A generalized tool+args pattern with approval history."""
    tool_name: str
    args_pattern: str       # Generalized JSON pattern
    pattern_hash: str       # For fast lookup
    approval_count: int
    denial_count: int
    workspace_id: str
    user_id: str


def generalize_args(tool_name: str, tool_args: dict) -> str:
    """Convert specific tool arguments to a generalized pattern.

    Rules:
      - File paths: keep the directory prefix, wildcard the filename
        ``/project/src/utils.py`` → ``/project/src/*``
      - UUIDs: replace with ``<UUID>``
      - Numbers > 100: replace with ``<N>``
      - Strings > 50 chars: replace with ``<CONTENT>``
      - Keep: action names, tool names, boolean flags, short enums
    """
    generalized: dict = {}
    for key, value in tool_args.items():
        if isinstance(value, str):
            # UUID pattern
            if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", value):
                generalized[key] = "<UUID>"
            # File path: keep directory, wildcard file
            elif "/" in value and len(value) > 5:
                parts = value.rsplit("/", 1)
                generalized[key] = parts[0] + "/*"
            # Long content strings
            elif len(value) > 50:
                generalized[key] = "<CONTENT>"
            else:
                generalized[key] = value  # Keep short strings (actions, enums)
        elif isinstance(value, bool):
            generalized[key] = value
        elif isinstance(value, (int, float)):
            generalized[key] = value if abs(value) <= 100 else "<N>"
        elif isinstance(value, dict):
            generalized[key] = "<OBJECT>"
        elif isinstance(value, list):
            generalized[key] = f"<LIST:{len(value)}>"
        else:
            generalized[key] = "<OTHER>"

    return json.dumps(generalized, sort_keys=True)


def pattern_hash(tool_name: str, args_pattern: str) -> str:
    """Deterministic hash for a tool+pattern combination."""
    key = f"{tool_name}:{args_pattern}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class ApprovalMemory:
    """Persistent approval pattern memory backed by PostgreSQL.

    Provides both async methods (for recording after approvals) and a
    sync-safe cache for fast lookups during the guardrails check.
    """

    def __init__(self, pool):
        self._pool = pool
        # In-memory cache: pattern_hash → (approval_count, denial_count)
        # Populated on first query per workspace, refreshed periodically.
        self._cache: dict[str, dict[str, tuple[int, int]]] = {}

    async def record_approval(
        self,
        tool_name: str,
        tool_args: dict,
        approved: bool,
        user_id: str,
        workspace_id: str,
    ) -> None:
        """Record an approval/denial decision for pattern learning."""
        args_pat = generalize_args(tool_name, tool_args)
        ph = pattern_hash(tool_name, args_pat)

        if approved:
            await self._pool.execute(
                """
                INSERT INTO approval_patterns
                    (pattern_hash, tool_name, args_pattern, workspace_id, user_id,
                     approval_count, denial_count, last_approved_at)
                VALUES ($1, $2, $3::jsonb, $4, $5, 1, 0, NOW())
                ON CONFLICT (pattern_hash, workspace_id) DO UPDATE SET
                    approval_count = approval_patterns.approval_count + 1,
                    last_approved_at = NOW()
                """,
                ph, tool_name, args_pat, workspace_id, user_id,
            )
        else:
            await self._pool.execute(
                """
                INSERT INTO approval_patterns
                    (pattern_hash, tool_name, args_pattern, workspace_id, user_id,
                     approval_count, denial_count, last_denied_at)
                VALUES ($1, $2, $3::jsonb, $4, $5, 0, 1, NOW())
                ON CONFLICT (pattern_hash, workspace_id) DO UPDATE SET
                    denial_count = approval_patterns.denial_count + 1,
                    last_denied_at = NOW()
                """,
                ph, tool_name, args_pat, workspace_id, user_id,
            )

        # Update local cache
        ws_cache = self._cache.setdefault(workspace_id, {})
        prev = ws_cache.get(ph, (0, 0))
        if approved:
            ws_cache[ph] = (prev[0] + 1, prev[1])
        else:
            ws_cache[ph] = (prev[0], prev[1] + 1)

    async def load_workspace_cache(self, workspace_id: str) -> None:
        """Pre-load all patterns for a workspace into the in-memory cache."""
        rows = await self._pool.fetch(
            """
            SELECT pattern_hash, approval_count, denial_count
            FROM approval_patterns
            WHERE workspace_id = $1
            ORDER BY last_approved_at DESC NULLS LAST
            LIMIT $2
            """,
            workspace_id,
            MAX_PATTERNS_PER_WORKSPACE,
        )
        ws_cache: dict[str, tuple[int, int]] = {}
        for row in rows:
            ws_cache[row["pattern_hash"]] = (
                row["approval_count"],
                row["denial_count"],
            )
        self._cache[workspace_id] = ws_cache

    def should_auto_approve_sync(
        self,
        tool_name: str,
        tool_args: dict,
        workspace_id: str,
    ) -> tuple[bool, Optional[str]]:
        """Sync-safe check using the in-memory cache.

        Returns ``(should_auto, reason_or_none)``.
        Called from ``Guardrails.check_tool()`` which is synchronous.
        """
        if not workspace_id:
            return False, None

        ws_cache = self._cache.get(workspace_id)
        if ws_cache is None:
            return False, None

        args_pat = generalize_args(tool_name, tool_args)
        ph = pattern_hash(tool_name, args_pat)

        counts = ws_cache.get(ph)
        if counts is None:
            return False, None

        approvals, denials = counts

        # Never auto-approve if there have been denials
        if denials > 0:
            return False, None

        if approvals >= AUTO_APPROVE_THRESHOLD:
            return True, (
                f"Auto-approved: '{tool_name}' pattern approved "
                f"{approvals} times previously"
            )

        return False, None

    async def should_auto_approve(
        self,
        tool_name: str,
        tool_args: dict,
        workspace_id: str,
    ) -> tuple[bool, Optional[str]]:
        """Async version — queries DB if cache miss."""
        # Try cache first
        ok, reason = self.should_auto_approve_sync(tool_name, tool_args, workspace_id)
        if ok:
            return True, reason

        if not workspace_id:
            return False, None

        # Cache miss — query DB
        args_pat = generalize_args(tool_name, tool_args)
        ph = pattern_hash(tool_name, args_pat)

        row = await self._pool.fetchrow(
            """
            SELECT approval_count, denial_count
            FROM approval_patterns
            WHERE pattern_hash = $1 AND workspace_id = $2
            """,
            ph, workspace_id,
        )

        if not row:
            return False, None

        approvals = row["approval_count"]
        denials = row["denial_count"]

        # Update cache
        ws_cache = self._cache.setdefault(workspace_id, {})
        ws_cache[ph] = (approvals, denials)

        if denials > 0:
            return False, None

        if approvals >= AUTO_APPROVE_THRESHOLD:
            return True, (
                f"Auto-approved: '{tool_name}' pattern approved "
                f"{approvals} times previously"
            )

        return False, None
