"""
Database tools — DISABLED for security.

Direct SQL execution against the platform database has been removed
to prevent SQL injection, data exfiltration, and CTE-based bypass attacks
(e.g., WITH deleted AS (DELETE FROM users RETURNING *) SELECT * FROM deleted).

If users need data analysis tools, they should:
  1. Use the code_execute tool with pandas/sqlite for data processing
  2. Connect external databases via workspace-scoped credentials
  3. Use purpose-built read-only APIs with parameterized queries
"""

import logging

logger = logging.getLogger("brain.agent.tools.data")


def register_data_tools(registry, pool=None) -> None:
    """
    Data tools registration — intentionally empty.

    Direct SQL execution against the platform database has been removed
    for security. The database_query and database_mutate tools allowed
    an LLM (or prompt injection attack) to bypass read-only checks via
    CTE expressions and access sensitive data including password hashes
    and API keys.
    """
    logger.info("Data tools: direct SQL tools disabled for security")
