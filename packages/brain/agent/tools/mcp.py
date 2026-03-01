"""
MCP tools — Search, connect, and manage MCP (Model Context Protocol) servers.

The agent can:
  1. Search for MCP servers from public registries
  2. Connect to an MCP server and list available tools
  3. Install an MCP server into the workspace's tool registry
  4. Call tools on connected MCP servers
"""

import asyncio
import json
import logging
import os
from typing import Optional

import httpx

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.mcp")

# Module-level workspace context — set by chat_service before each task
_current_workspace_id: Optional[str] = None

# ── Known MCP Server Registries ───────────────────────────────────────
# These are public sources of MCP server metadata.
MCP_REGISTRIES = [
    {
        "name": "Smithery",
        "url": "https://registry.smithery.ai/api/v1/servers",
        "type": "api",
    },
    {
        "name": "MCP Hub",
        "url": "https://raw.githubusercontent.com/modelcontextprotocol/servers/main/README.md",
        "type": "github_readme",
    },
]

# ── Built-in MCP Server Catalog (offline fallback) ───────────────────
BUILTIN_CATALOG = [
    {
        "name": "filesystem",
        "description": "Read, write, and manage files and directories on the local filesystem.",
        "install": "npx -y @modelcontextprotocol/server-filesystem",
        "transport": "stdio",
        "category": "files",
    },
    {
        "name": "brave-search",
        "description": "Search the web using Brave's search API.",
        "install": "npx -y @modelcontextprotocol/server-brave-search",
        "transport": "stdio",
        "category": "web",
        "requires_env": ["BRAVE_API_KEY"],
    },
    {
        "name": "github",
        "description": "Interact with GitHub repositories, issues, PRs, and more.",
        "install": "npx -y @modelcontextprotocol/server-github",
        "transport": "stdio",
        "category": "development",
        "requires_env": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    },
    {
        "name": "slack",
        "description": "Read and send Slack messages, manage channels and threads.",
        "install": "npx -y @modelcontextprotocol/server-slack",
        "transport": "stdio",
        "category": "communication",
        "requires_env": ["SLACK_BOT_TOKEN"],
    },
    {
        "name": "postgres",
        "description": "Query PostgreSQL databases with read-only access.",
        "install": "npx -y @modelcontextprotocol/server-postgres",
        "transport": "stdio",
        "category": "database",
        "requires_env": ["POSTGRES_URL"],
    },
    {
        "name": "sqlite",
        "description": "Read and query SQLite databases.",
        "install": "npx -y @modelcontextprotocol/server-sqlite",
        "transport": "stdio",
        "category": "database",
    },
    {
        "name": "puppeteer",
        "description": "Control a headless browser — navigate, screenshot, click, fill forms.",
        "install": "npx -y @modelcontextprotocol/server-puppeteer",
        "transport": "stdio",
        "category": "web",
    },
    {
        "name": "memory",
        "description": "Persistent memory using a knowledge graph with entities and relations.",
        "install": "npx -y @modelcontextprotocol/server-memory",
        "transport": "stdio",
        "category": "memory",
    },
    {
        "name": "sequential-thinking",
        "description": "Chain-of-thought reasoning for complex multi-step problems.",
        "install": "npx -y @modelcontextprotocol/server-sequential-thinking",
        "transport": "stdio",
        "category": "reasoning",
    },
    {
        "name": "google-maps",
        "description": "Search locations, get directions, and geocode addresses.",
        "install": "npx -y @modelcontextprotocol/server-google-maps",
        "transport": "stdio",
        "category": "location",
        "requires_env": ["GOOGLE_MAPS_API_KEY"],
    },
    {
        "name": "fetch",
        "description": "Fetch URLs and convert HTML to markdown for LLM consumption.",
        "install": "npx -y @modelcontextprotocol/server-fetch",
        "transport": "stdio",
        "category": "web",
    },
    {
        "name": "everything",
        "description": "Demo MCP server that exercises all MCP features (tools, resources, prompts).",
        "install": "npx -y @modelcontextprotocol/server-everything",
        "transport": "stdio",
        "category": "demo",
    },
    {
        "name": "ollama",
        "description": "Generate text and chat with local Ollama models. Supports model listing and multi-turn conversations.",
        "install": "python3 mcp-servers/ollama/server.py",
        "transport": "stdio",
        "category": "ai",
    },
]


def register_mcp_tools(registry, pool=None) -> None:
    """Register MCP discovery and management tools."""

    async def mcp_search(query: str, category: str = "") -> dict:
        """Search for MCP servers matching a query."""
        query_lower = query.lower()

        # 0. Merge workspace-installed servers into the search catalog
        workspace_servers: list[dict] = []
        ws_id = _current_workspace_id or ""
        if pool and ws_id:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """SELECT name, description, server_url, transport
                           FROM installed_tools
                           WHERE workspace_id = $1 AND enabled = true""",
                        ws_id,
                    )
                for r in rows:
                    workspace_servers.append({
                        "name": r["name"],
                        "description": r["description"] or "",
                        "install": r["server_url"],
                        "transport": r["transport"] or "stdio",
                        "category": "workspace",
                        "source": "installed",
                    })
            except Exception as e:
                logger.debug(f"Failed to load workspace MCP servers: {e}")

        # 1. Search built-in catalog + workspace-installed servers
        combined_catalog = BUILTIN_CATALOG + workspace_servers
        results = []
        for server in combined_catalog:
            score = 0
            if query_lower in server["name"].lower():
                score += 3
            if query_lower in server.get("description", "").lower():
                score += 2
            if category and category.lower() == server.get("category", ""):
                score += 2
            # Workspace-installed servers get a relevance boost
            if server.get("source") == "installed":
                score += 1
            # Check word-level matches
            for word in query_lower.split():
                if word in server.get("description", "").lower():
                    score += 1
                if word in server["name"].lower():
                    score += 1
            if score > 0:
                results.append({**server, "_score": score})

        # 2. Try searching the Smithery registry API
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://registry.smithery.ai/api/v1/servers",
                    params={"q": query, "limit": 10},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    servers = data.get("servers", data) if isinstance(data, dict) else data
                    if isinstance(servers, list):
                        for s in servers[:10]:
                            results.append({
                                "name": s.get("name", s.get("qualifiedName", "")),
                                "description": s.get("description", ""),
                                "install": s.get("installCommand", s.get("qualifiedName", "")),
                                "transport": s.get("transport", "stdio"),
                                "category": s.get("category", "external"),
                                "source": "smithery",
                                "_score": 1,
                            })
        except Exception as e:
            logger.debug(f"Smithery search failed (offline fallback): {e}")

        # Sort by relevance
        results.sort(key=lambda x: x.get("_score", 0), reverse=True)

        # Remove score from output
        for r in results:
            r.pop("_score", None)

        return {
            "query": query,
            "results": results[:15],
            "total": len(results),
            "sources": ["builtin_catalog", "smithery_registry"],
        }

    async def mcp_install(
        name: str,
        server_command: str,
        workspace_id: str = "",
        transport: str = "stdio",
        description: str = "",
        env_vars: str = "",
    ) -> dict:
        """Install/register an MCP server for this workspace."""
        workspace_id = workspace_id or _current_workspace_id or ""
        if not pool:
            return {"success": False, "error": "No database connection available"}
        if not workspace_id:
            return {"success": False, "error": "No workspace_id available. Cannot install MCP server."}

        config = {}
        if env_vars:
            try:
                config["env"] = json.loads(env_vars)
            except json.JSONDecodeError:
                # Parse KEY=VALUE format
                env_dict = {}
                for line in env_vars.strip().split("\n"):
                    if "=" in line:
                        k, v = line.split("=", 1)
                        env_dict[k.strip()] = v.strip()
                config["env"] = env_dict

        try:
            import uuid
            tool_id = str(uuid.uuid4())

            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO installed_tools
                        (id, workspace_id, name, description, server_url, transport, config)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    ON CONFLICT (workspace_id, name)
                    DO UPDATE SET server_url = $5, transport = $6,
                                  config = $7::jsonb, updated_at = NOW(), enabled = true
                    """,
                    tool_id, workspace_id, name, description,
                    server_command, transport, json.dumps(config),
                )

            return {
                "success": True,
                "message": f"MCP server '{name}' installed successfully.",
                "name": name,
                "command": server_command,
                "transport": transport,
                "note": "The server will be available in your next conversation. "
                        "Restart may be needed for stdio servers.",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mcp_list(workspace_id: str = "") -> dict:
        """List all installed MCP servers for a workspace."""
        workspace_id = workspace_id or _current_workspace_id or ""
        if not pool:
            return {"installed": [], "error": "No database connection"}
        if not workspace_id:
            return {"installed": [], "error": "No workspace_id available."}

        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT name, description, server_url, transport, enabled, installed_at
                    FROM installed_tools
                    WHERE workspace_id = $1
                    ORDER BY name
                    """,
                    workspace_id,
                )
            return {
                "installed": [
                    {
                        "name": r["name"],
                        "description": r["description"],
                        "command": r["server_url"],
                        "transport": r["transport"],
                        "enabled": r["enabled"],
                        "installed_at": str(r["installed_at"]),
                    }
                    for r in rows
                ],
                "total": len(rows),
            }
        except Exception as e:
            return {"installed": [], "error": str(e)}

    async def mcp_uninstall(name: str, workspace_id: str = "") -> dict:
        """Uninstall/remove an MCP server from the workspace."""
        workspace_id = workspace_id or _current_workspace_id or ""
        if not pool:
            return {"success": False, "error": "No database connection"}
        if not workspace_id:
            return {"success": False, "error": "No workspace_id available."}

        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM installed_tools WHERE workspace_id = $1 AND name = $2",
                    workspace_id, name,
                )
            deleted = int(result.split(" ")[-1]) if result else 0
            return {
                "success": deleted > 0,
                "message": f"MCP server '{name}' removed." if deleted else f"'{name}' not found.",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Register Tools ────────────────────────────────────────────────
    registry.register(
        definition=ToolDefinition(
            name="mcp_search",
            description=(
                "Search for MCP (Model Context Protocol) servers that can add new "
                "capabilities. Search by keyword, category, or description. Returns "
                "available servers with install commands. Categories include: "
                "web, files, database, development, communication, memory, reasoning."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g., 'github', 'database', 'browser automation')",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional category filter",
                        "default": "",
                    },
                },
                "required": ["query"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=12,
            category="mcp",
        ),
        handler=mcp_search,
    )

    registry.register(
        definition=ToolDefinition(
            name="mcp_install",
            description=(
                "Install an MCP server into this workspace's tool registry. "
                "After installation, the server's tools become available in future "
                "conversations. Use mcp_search first to find servers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name for this MCP server (e.g., 'github', 'postgres')",
                    },
                    "server_command": {
                        "type": "string",
                        "description": "Install/run command (e.g., 'npx -y @modelcontextprotocol/server-github')",
                    },
                    "workspace_id": {
                        "type": "string",
                        "description": "Workspace ID (auto-filled by agent context)",
                        "default": "",
                    },
                    "transport": {
                        "type": "string",
                        "description": "Connection type: stdio, http, or sse",
                        "default": "stdio",
                    },
                    "description": {
                        "type": "string",
                        "description": "Brief description of what this server provides",
                        "default": "",
                    },
                    "env_vars": {
                        "type": "string",
                        "description": "Environment variables needed, as JSON or KEY=VALUE lines",
                        "default": "",
                    },
                },
                "required": ["name", "server_command"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=15,
            category="mcp",
        ),
        handler=mcp_install,
    )

    registry.register(
        definition=ToolDefinition(
            name="mcp_list_installed",
            description="List all MCP servers installed in this workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "workspace_id": {
                        "type": "string",
                        "description": "Workspace ID",
                        "default": "",
                    },
                },
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=5,
            category="mcp",
        ),
        handler=mcp_list,
    )

    registry.register(
        definition=ToolDefinition(
            name="mcp_uninstall",
            description="Remove an installed MCP server from this workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the MCP server to remove",
                    },
                    "workspace_id": {
                        "type": "string",
                        "description": "Workspace ID",
                        "default": "",
                    },
                },
                "required": ["name"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=5,
            category="mcp",
        ),
        handler=mcp_uninstall,
    )

    logger.info("MCP tools registered: mcp_search, mcp_install, mcp_list_installed, mcp_uninstall")

    # ── MCP Protocol Tools (require the client) ──────────────────────

    from agent.tools.mcp_client import get_mcp_pool

    mcp_pool = get_mcp_pool()

    async def mcp_connect(
        name: str,
        command: str = "",
        env_vars: str = "",
        workspace_id: str = "",
    ) -> dict:
        """Connect to an MCP server and discover its tools."""
        # If no command given, try to find from catalog or installed DB
        if not command:
            for server in BUILTIN_CATALOG:
                if server["name"] == name:
                    command = server["install"]
                    break

        # Also check the installed_tools DB for this server.
        # Always query the DB: use stored command only if none was found above,
        # but always load stored env vars (e.g. API keys saved via mcp_install).
        stored_env = {}
        if pool:
            ws_id = workspace_id or _current_workspace_id or ""
            if ws_id:
                try:
                    async with pool.acquire() as conn:
                        row = await conn.fetchrow(
                            """SELECT server_url, config FROM installed_tools
                               WHERE (name = $1 OR name ILIKE '%' || $1 || '%')
                                 AND enabled = true
                               ORDER BY CASE WHEN name = $1 THEN 0 ELSE 1 END
                               LIMIT 1""",
                            name,
                        )
                        if row:
                            if not command:
                                command = row["server_url"]
                            if row["config"]:
                                cfg = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
                                stored_env = cfg.get("env", {})
                except Exception as e:
                    logger.warning(f"Failed to look up installed MCP server '{name}': {e}")

        if not command:
            return {"error": f"No command specified for '{name}'. Use mcp_search to find it."}

        # Parse env vars (explicitly provided override stored ones)
        env = dict(stored_env)  # Start with stored env
        if env_vars:
            try:
                env.update(json.loads(env_vars))
            except json.JSONDecodeError:
                for line in env_vars.strip().split("\n"):
                    if "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()

        # Auto-resolve required env vars from os.environ and common aliases.
        # This bridges naming mismatches, e.g. user has GITHUB_PAT in .env
        # but the GitHub MCP server expects GITHUB_PERSONAL_ACCESS_TOKEN.
        _ENV_ALIASES = {
            "GITHUB_PERSONAL_ACCESS_TOKEN": ["GITHUB_PAT", "GITHUB_TOKEN"],
        }
        for catalog_server in BUILTIN_CATALOG:
            if catalog_server["name"] == name:
                for required_var in catalog_server.get("requires_env", []):
                    if required_var not in env:
                        # Try os.environ directly first
                        val = os.environ.get(required_var)
                        if not val:
                            # Try common aliases
                            for alias in _ENV_ALIASES.get(required_var, []):
                                val = os.environ.get(alias)
                                if val:
                                    logger.info(
                                        f"MCP '{name}': resolved {required_var} "
                                        f"from alias {alias}"
                                    )
                                    break
                        if val:
                            env[required_var] = val
                break

        # Pre-call health check: verify existing connection is healthy
        existing = await mcp_pool.get_client(name)
        if existing and existing.connected:
            return {
                "already_connected": True,
                "server": existing.server_info,
                "tools": [
                    {"name": t["name"], "description": t.get("description", "")}
                    for t in existing.tools
                ],
            }

        result = await mcp_pool.connect(name, command, env)
        return result

    async def mcp_call(
        server_name: str,
        tool_name: str,
        arguments: str = "{}",
    ) -> dict:
        """Call a tool on a connected MCP server."""
        client = await mcp_pool.get_client(server_name)
        if not client:
            connected = mcp_pool.list_connected()
            names = [c["name"] for c in connected]
            return {
                "error": f"Server '{server_name}' not connected.",
                "connected_servers": names,
                "hint": "Use mcp_connect first.",
            }

        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON arguments: {arguments}"}

        return await client.call_tool(tool_name, args)

    async def mcp_disconnect(name: str = "") -> dict:
        """Disconnect from an MCP server (or all if name is empty)."""
        if not name:
            await mcp_pool.disconnect_all()
            return {"success": True, "message": "All MCP servers disconnected."}
        return await mcp_pool.disconnect(name)

    async def mcp_status() -> dict:
        """Show status of all connected MCP servers and their tools."""
        connected = mcp_pool.list_connected()
        all_tools = mcp_pool.get_all_tools()
        return {
            "connected_servers": connected,
            "total_tools": len(all_tools),
            "tools": all_tools,
        }

    # Register protocol tools
    registry.register(
        definition=ToolDefinition(
            name="mcp_connect",
            description=(
                "Connect to an MCP server by name. If it's a built-in server "
                "(e.g., 'filesystem', 'github', 'brave-search'), just provide the name. "
                "For custom servers, provide the command to start it. "
                "Returns the list of tools the server provides."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the MCP server (e.g., 'filesystem', 'github')",
                    },
                    "command": {
                        "type": "string",
                        "description": "Command to start the server (optional if built-in)",
                        "default": "",
                    },
                    "env_vars": {
                        "type": "string",
                        "description": "Environment variables as JSON or KEY=VALUE lines",
                        "default": "",
                    },
                },
                "required": ["name"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=35,
            category="mcp",
        ),
        handler=mcp_connect,
    )

    registry.register(
        definition=ToolDefinition(
            name="mcp_call",
            description=(
                "Call a tool on a connected MCP server. The server must be "
                "connected first using mcp_connect. Arguments are passed as JSON."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Name of the connected MCP server",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Name of the tool to call",
                    },
                    "arguments": {
                        "type": "string",
                        "description": "Tool arguments as JSON string",
                        "default": "{}",
                    },
                },
                "required": ["server_name", "tool_name"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=60,
            category="mcp",
        ),
        handler=mcp_call,
    )

    registry.register(
        definition=ToolDefinition(
            name="mcp_disconnect",
            description="Disconnect from an MCP server (or all if no name given).",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Server name to disconnect, or empty for all",
                        "default": "",
                    },
                },
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=10,
            category="mcp",
        ),
        handler=mcp_disconnect,
    )

    registry.register(
        definition=ToolDefinition(
            name="mcp_status",
            description="Show all connected MCP servers and their available tools.",
            parameters={"type": "object", "properties": {}},
            risk_level=RiskLevel.LOW,
            timeout_seconds=5,
            category="mcp",
        ),
        handler=mcp_status,
    )

    logger.info("MCP protocol tools registered: mcp_connect, mcp_call, mcp_disconnect, mcp_status")
