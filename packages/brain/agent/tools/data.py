"""
Database tools — read-only queries and write mutations.

Write mutations (INSERT, UPDATE, DELETE, DDL) are classified as HIGH risk
and always require human approval.
"""

import logging
import os
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.data")

# Connection pool (set during registration)
_pool = None


def register_data_tools(registry, pool=None) -> None:
    """Register database tools."""
    global _pool
    _pool = pool

    registry.register(
        definition=ToolDefinition(
            name="database_query",
            description=(
                "Execute a read-only SQL query against the workspace database. "
                "Returns results as a list of rows. Use for data analysis, "
                "lookups, and reporting."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SQL SELECT query to execute",
                    },
                    "max_rows": {
                        "type": "integer",
                        "description": "Maximum rows to return (default 50)",
                        "default": 50,
                    },
                },
                "required": ["query"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=30,
            category="data",
        ),
        handler=database_query,
    )

    registry.register(
        definition=ToolDefinition(
            name="database_mutate",
            description=(
                "Execute a write SQL statement (INSERT, UPDATE, DELETE, CREATE, ALTER, DROP). "
                "This modifies the database and requires human approval. "
                "Use only when you need to change data or schema."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SQL statement to execute",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this mutation is needed (shown to human for approval)",
                    },
                },
                "required": ["query", "reason"],
            },
            risk_level=RiskLevel.HIGH,
            requires_approval=True,
            timeout_seconds=30,
            category="data",
        ),
        handler=database_mutate,
    )


async def database_query(
    query: str,
    max_rows: int = 50,
) -> dict:
    """Execute a read-only SQL query."""
    if not _pool:
        return {"error": "Database not available"}

    max_rows = min(max_rows, 200)

    # Safety: only allow SELECT and WITH (CTE) statements
    stripped = query.strip().upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
        return {
            "error": "Only SELECT and WITH (CTE) queries are allowed. "
                     "Use database_mutate for write operations.",
        }

    try:
        async with _pool.acquire() as conn:
            # Force statement timeout
            await conn.execute("SET statement_timeout = '25s'")

            rows = await conn.fetch(query)
            rows = rows[:max_rows]

            # Convert to list of dicts
            results = []
            for row in rows:
                results.append({k: _serialize_value(v) for k, v in dict(row).items()})

            return {
                "query": query,
                "rows": results,
                "count": len(results),
                "truncated": len(rows) >= max_rows,
            }

    except Exception as e:
        return {"query": query, "error": str(e)}


async def database_mutate(
    query: str,
    reason: str = "",
) -> dict:
    """Execute a write SQL statement (requires approval)."""
    if not _pool:
        return {"error": "Database not available"}

    try:
        async with _pool.acquire() as conn:
            await conn.execute("SET statement_timeout = '25s'")
            result = await conn.execute(query)

            return {
                "query": query,
                "result": result,
                "success": True,
                "reason": reason,
            }

    except Exception as e:
        return {"query": query, "error": str(e)}


def _serialize_value(value) -> str | int | float | bool | None:
    """Serialize a database value to a JSON-safe type."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    # datetime, UUID, Decimal, etc.
    return str(value)
