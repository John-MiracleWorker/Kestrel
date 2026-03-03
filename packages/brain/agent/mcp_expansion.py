"""
MCP Auto-Expansion Engine — discovers and installs new MCP servers
when capability gaps are detected.

When the agent encounters a task that requires tools not currently available,
this engine:
  1. Detects the capability gap (via LLM analysis)
  2. Searches for MCP servers that could fill the gap
  3. Requests approval before installation
  4. Installs and connects the new server
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("brain.agent.mcp_expansion")


@dataclass
class MCPCandidate:
    """A candidate MCP server that could fill a capability gap."""
    name: str
    description: str
    capability: str
    install_command: str = ""
    confidence: float = 0.0
    source: str = ""  # "registry", "github", "builtin"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "capability": self.capability,
            "install_command": self.install_command,
            "confidence": round(self.confidence, 2),
            "source": self.source,
        }


# ── Built-in MCP Server Catalog ──────────────────────────────────────

BUILTIN_MCP_CATALOG: dict[str, MCPCandidate] = {
    "email": MCPCandidate(
        name="gmail-mcp",
        description="Gmail MCP server for reading and sending emails",
        capability="email",
        install_command="npx @anthropic/mcp-server-gmail",
        confidence=0.9,
        source="builtin",
    ),
    "github": MCPCandidate(
        name="github-mcp",
        description="GitHub MCP server for repo, PR, and issue management",
        capability="github",
        install_command="npx @anthropic/mcp-server-github",
        confidence=0.9,
        source="builtin",
    ),
    "slack": MCPCandidate(
        name="slack-mcp",
        description="Slack MCP server for channel messaging",
        capability="slack",
        install_command="npx @anthropic/mcp-server-slack",
        confidence=0.9,
        source="builtin",
    ),
    "filesystem": MCPCandidate(
        name="filesystem-mcp",
        description="Filesystem MCP server for file operations",
        capability="filesystem",
        install_command="npx @anthropic/mcp-server-filesystem",
        confidence=0.9,
        source="builtin",
    ),
    "database": MCPCandidate(
        name="postgres-mcp",
        description="PostgreSQL MCP server for database queries",
        capability="database",
        install_command="npx @anthropic/mcp-server-postgres",
        confidence=0.85,
        source="builtin",
    ),
    "web_browse": MCPCandidate(
        name="puppeteer-mcp",
        description="Puppeteer MCP server for web browsing and scraping",
        capability="web_browse",
        install_command="npx @anthropic/mcp-server-puppeteer",
        confidence=0.85,
        source="builtin",
    ),
}


class MCPExpansionEngine:
    """
    Detects capability gaps and suggests MCP server installations.

    Workflow:
      1. Agent fails to find a tool → detect_capability_gap()
      2. Engine analyzes the goal and available tools via LLM
      3. Searches catalog/registry for matching MCP servers
      4. Returns candidate(s) for approval
      5. After approval, installs via the MCP connection manager
    """

    def __init__(self, llm_provider=None, model: str = "", mcp_manager=None):
        self._provider = llm_provider
        self._model = model
        self._mcp_manager = mcp_manager  # The existing MCP connection manager

    async def detect_capability_gap(
        self,
        goal: str,
        available_tools: list[str],
        error_message: str = "",
    ) -> Optional[str]:
        """
        Use LLM to determine if a capability gap exists and what's needed.

        Returns a capability keyword (e.g., "email", "github") or None.
        """
        if not self._provider:
            return self._rule_based_gap_detection(goal, error_message)

        try:
            prompt = (
                f"A task failed or has a capability gap.\n"
                f"Goal: {goal}\n"
                f"Available tools: {', '.join(available_tools[:30])}\n"
                f"Error: {error_message[:200]}\n\n"
                f"What external capability is missing? Respond with just ONE word "
                f"from: email, github, slack, filesystem, database, web_browse, calendar, "
                f"spreadsheet, none\n"
                f"If no external capability is needed, respond with 'none'."
            )

            response = await self._provider.generate(
                messages=[{"role": "user", "content": prompt}],
                model=self._model,
                temperature=0.1,
                max_tokens=50,
            )

            raw = response if isinstance(response, str) else response.get("content", "")
            capability = raw.strip().lower().split()[0] if raw.strip() else "none"
            return None if capability == "none" else capability

        except Exception as e:
            logger.warning(f"LLM gap detection failed: {e}")
            return self._rule_based_gap_detection(goal, error_message)

    def _rule_based_gap_detection(self, goal: str, error: str) -> Optional[str]:
        """Fast rule-based fallback for capability gap detection."""
        goal_lower = goal.lower()
        error_lower = error.lower()
        combined = goal_lower + " " + error_lower

        keyword_map = {
            "email": ["email", "gmail", "inbox", "send mail", "mail"],
            "github": ["github", "pull request", "pr ", "issue", "repository"],
            "slack": ["slack", "channel", "message team"],
            "database": ["database", "sql", "postgres", "query data"],
        }

        for capability, keywords in keyword_map.items():
            if any(kw in combined for kw in keywords):
                return capability

        return None

    async def search_and_evaluate(self, capability: str) -> list[MCPCandidate]:
        """Search for MCP servers matching the capability."""
        candidates = []

        # Check built-in catalog first
        if capability in BUILTIN_MCP_CATALOG:
            candidates.append(BUILTIN_MCP_CATALOG[capability])

        # Check partial matches
        for cap_key, candidate in BUILTIN_MCP_CATALOG.items():
            if capability in candidate.description.lower() and candidate not in candidates:
                lower_conf = MCPCandidate(
                    name=candidate.name,
                    description=candidate.description,
                    capability=candidate.capability,
                    install_command=candidate.install_command,
                    confidence=candidate.confidence * 0.7,
                    source=candidate.source,
                )
                candidates.append(lower_conf)

        return candidates

    async def install_with_approval(
        self,
        candidate: MCPCandidate,
        workspace_id: str,
        approval_fn=None,
    ) -> bool:
        """
        Install an MCP server after getting approval.

        Returns True if installation succeeded.
        """
        if approval_fn:
            approved = await approval_fn(
                f"Install MCP server '{candidate.name}'?\n"
                f"Description: {candidate.description}\n"
                f"Command: {candidate.install_command}"
            )
            if not approved:
                logger.info(f"MCP installation denied: {candidate.name}")
                return False

        if self._mcp_manager:
            try:
                await self._mcp_manager.connect(
                    name=candidate.name,
                    command=candidate.install_command,
                )
                logger.info(f"MCP server installed: {candidate.name}")
                return True
            except Exception as e:
                logger.error(f"MCP installation failed: {e}")
                return False

        logger.warning(f"No MCP manager available for installation of {candidate.name}")
        return False
