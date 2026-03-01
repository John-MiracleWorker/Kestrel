"""
MCP Protocol Client — JSON-RPC 2.0 over stdio transport.

Implements the Model Context Protocol (MCP) client that can:
  1. Spawn an MCP server as a subprocess
  2. Perform the initialize handshake
  3. Discover available tools (tools/list)
  4. Call tools (tools/call)
  5. Gracefully disconnect

Protocol Reference: https://modelcontextprotocol.io/
"""

import asyncio
import json
import logging
import os
import shlex
import sys
import uuid
from typing import Any, Optional

logger = logging.getLogger("brain.agent.tools.mcp_client")


class MCPClient:
    """Client for a single MCP server connection over stdio."""

    def __init__(self, name: str, command: str, env: Optional[dict] = None):
        self.name = name
        self.command = command
        self.env = env or {}
        self._process: Optional[asyncio.subprocess.Process] = None
        self._connected = False
        self._server_info: dict = {}
        self._server_capabilities: dict = {}
        self._tools: list[dict] = []
        self._lock = asyncio.Lock()
        self._read_buffer = ""

    @property
    def connected(self) -> bool:
        return self._connected and self._process is not None

    @property
    def tools(self) -> list[dict]:
        return self._tools

    @property
    def server_info(self) -> dict:
        return self._server_info

    async def connect(self, timeout: float = 30) -> dict:
        """Spawn the MCP server process and perform the initialize handshake."""
        if self._connected:
            return {"already_connected": True, "server": self._server_info}

        # Build environment
        env = {**os.environ, **self.env}

        # Kestrel brain container maps the host's /Users -> /host_fs
        # We must rewrite host paths so subprocesses can find the files.
        mapped_command = self.command.replace("/Users/", "/host_fs/")

        try:
            parts = shlex.split(mapped_command)
        except ValueError:
            parts = mapped_command.split()

        # Try to fix unquoted paths with spaces (e.g., 'python /host_fs/.../little bird alt/server.py')
        if len(parts) >= 2 and parts[0] in ('python', 'python3', 'node', 'npx', 'ts-node'):
            potential_path = mapped_command[len(parts[0]):].strip()
            if os.path.exists(potential_path):
                parts = [parts[0], potential_path]

        # Auto-install dependencies for local Python/Node scripts if possible
        if parts and len(parts) >= 2:
            cmd = parts[0].lower()
            script_path = next((p for p in parts[1:] if p.endswith('.py') or p.endswith('.js') or p.endswith('.ts')), None)
            
            if script_path and os.path.exists(script_path):
                script_dir = os.path.dirname(os.path.abspath(script_path))
                
                # Auto-install Python requirements
                if cmd in ('python', 'python3') and os.path.exists(os.path.join(script_dir, 'requirements.txt')):
                    req_file = os.path.join(script_dir, 'requirements.txt')
                    logger.info(f"Auto-installing dependencies for {self.name} from {req_file}...")
                    try:
                        pip_proc = await asyncio.create_subprocess_exec(
                            sys.executable, "-m", "pip", "install", "-r", req_file,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            env=env
                        )
                        await pip_proc.wait()
                    except Exception as e:
                        logger.warning(f"Failed to auto-install requirements for {self.name}: {e}")
                        
                # Auto-install Node (package.json)
                elif cmd in ('node', 'npx', 'ts-node') and os.path.exists(os.path.join(script_dir, 'package.json')):
                    logger.info(f"Auto-installing npm dependencies for {self.name} in {script_dir}...")
                    try:
                        npm_proc = await asyncio.create_subprocess_exec(
                            "npm", "install",
                            cwd=script_dir,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            env=env
                        )
                        await npm_proc.wait()
                    except Exception as e:
                        logger.warning(f"Failed to auto-install npm packages for {self.name}: {e}")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *parts,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            return {"error": f"Command not found: {parts[0]}. Is it installed?"}
        except Exception as e:
            return {"error": f"Failed to spawn MCP server: {e}"}

        logger.info(f"MCP server '{self.name}' spawned (PID {self._process.pid})")

        # Perform initialize handshake
        try:
            result = await asyncio.wait_for(
                self._initialize(),
                timeout=timeout,
            )
            self._connected = True
            return result
        except asyncio.TimeoutError:
            await self.disconnect()
            return {"error": f"MCP server '{self.name}' timed out during initialization"}
        except Exception as e:
            await self.disconnect()
            return {"error": f"MCP initialization failed: {e}"}

    async def _initialize(self) -> dict:
        """Send initialize request and initialized notification."""
        # Step 1: Send initialize request
        init_result = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": False},
            },
            "clientInfo": {
                "name": "Kestrel",
                "version": "1.0.0",
            },
        })

        if "error" in init_result:
            raise Exception(f"Initialize failed: {init_result['error']}")

        result = init_result.get("result", {})
        self._server_info = result.get("serverInfo", {})
        self._server_capabilities = result.get("capabilities", {})

        # Step 2: Send initialized notification
        await self._send_notification("notifications/initialized", {})

        # Step 3: Discover tools — always attempt, many servers support tools
        # but don't advertise them in capabilities (e.g., GitHub MCP server)
        try:
            tools_result = await self._send_request("tools/list", {})
            self._tools = tools_result.get("result", {}).get("tools", [])
        except Exception as e:
            logger.debug(f"MCP '{self.name}': tools/list not supported: {e}")
            self._tools = []

        return {
            "server_info": self._server_info,
            "capabilities": self._server_capabilities,
            "tools_count": len(self._tools),
            "tools": [
                {"name": t["name"], "description": t.get("description", "")}
                for t in self._tools
            ],
        }

    async def list_tools(self) -> list[dict]:
        """List all tools available on this MCP server."""
        if not self._connected:
            return []

        result = await self._send_request("tools/list", {})
        self._tools = result.get("result", {}).get("tools", [])
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict = None) -> dict:
        """Call a tool on the connected MCP server.

        If the call fails due to a broken connection (process crash, pipe
        error), automatically attempts one reconnect before returning an error.
        """
        for attempt in range(2):  # 1 normal + 1 reconnect
            if not self._connected:
                if attempt == 0:
                    return {"error": "Not connected to MCP server"}
                # Second attempt after reconnect failed
                return {"error": "Reconnect failed — MCP server unreachable"}

            # Validate tool exists
            valid_tools = [t["name"] for t in self._tools]
            if tool_name not in valid_tools:
                return {
                    "error": f"Tool '{tool_name}' not found on '{self.name}'",
                    "available_tools": valid_tools,
                }

            try:
                result = await self._send_request("tools/call", {
                    "name": tool_name,
                    "arguments": arguments or {},
                })
            except (BrokenPipeError, ConnectionError, OSError) as e:
                logger.warning(
                    f"MCP '{self.name}' connection lost during call_tool: {e}. "
                    f"Attempting reconnect…"
                )
                await self._try_reconnect()
                continue

            if "error" in result:
                err = str(result["error"])
                # Detect transport-level failures that warrant a reconnect
                if any(kw in err.lower() for kw in ("broken pipe", "eof", "connection", "transport")):
                    if attempt == 0:
                        logger.warning(f"MCP '{self.name}' transport error: {err}. Reconnecting…")
                        await self._try_reconnect()
                        continue
                return {"error": result["error"]}

            # Parse MCP tool result
            tool_result = result.get("result", {})
            content = tool_result.get("content", [])

            # Extract text content
            output_parts = []
            for item in content:
                if item.get("type") == "text":
                    output_parts.append(item.get("text", ""))
                elif item.get("type") == "image":
                    output_parts.append(f"[Image: {item.get('mimeType', 'image')}]")
                elif item.get("type") == "resource":
                    output_parts.append(f"[Resource: {item.get('uri', '')}]")

            return {
                "success": not tool_result.get("isError", False),
                "output": "\n".join(output_parts) if output_parts else str(tool_result),
                "raw": tool_result,
            }

        return {"error": "MCP call_tool failed after reconnect attempt"}

    async def _try_reconnect(self) -> None:
        """Attempt to reconnect to the MCP server after a connection failure."""
        try:
            await self.disconnect()
        except Exception:
            pass
        try:
            await self.connect(timeout=15)
            logger.info(f"MCP '{self.name}' reconnected successfully")
        except Exception as e:
            logger.error(f"MCP '{self.name}' reconnect failed: {e}")

    async def disconnect(self):
        """Gracefully shut down the MCP server."""
        self._connected = False
        if self._process:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._process.kill()
            except ProcessLookupError:
                pass
            logger.info(f"MCP server '{self.name}' disconnected")
            self._process = None

    # ── JSON-RPC Protocol ────────────────────────────────────────────

    async def _send_request(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and wait for the response."""
        async with self._lock:
            request_id = str(uuid.uuid4())[:8]
            message = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }

            await self._write_message(message)

            # Read responses until we get one matching our ID
            for _ in range(50):  # Safety limit
                response = await self._read_message()
                if response is None:
                    stderr_output = await self._capture_stderr()
                    error_msg = "Server closed connection"
                    if stderr_output:
                        error_msg += f"\nServer stderr:\n{stderr_output}"
                    return {"error": error_msg}

                # Skip notifications (no id field)
                if "id" not in response:
                    continue

                if response.get("id") == request_id:
                    if "error" in response:
                        return {"error": response["error"]}
                    return response

            return {"error": "No response received for request"}

    async def _send_notification(self, method: str, params: dict):
        """Send a JSON-RPC notification (no response expected)."""
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._write_message(message)

    async def _write_message(self, message: dict):
        """Write a JSON-RPC message to the server's stdin (JSONL format)."""
        if not self._process or not self._process.stdin:
            raise ConnectionError("MCP server stdin not available")

        data = json.dumps(message) + "\n"
        self._process.stdin.write(data.encode("utf-8"))
        await self._process.stdin.drain()

    async def _read_message(self) -> Optional[dict]:
        """Read a JSON-RPC message from the server's stdout (JSONL format)."""
        if not self._process or not self._process.stdout:
            return None

        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(),
                timeout=30,
            )
            if not line:
                return None

            return json.loads(line.decode("utf-8").strip())

        except asyncio.TimeoutError:
            logger.error("MCP read timeout")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"MCP JSON decode error: {e}")
            return None

    async def _capture_stderr(self) -> str:
        """Read available stderr from a crashed server process for diagnostics."""
        if not self._process or not self._process.stderr:
            return ""
        try:
            if self._process.returncode is None:
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            stderr_bytes = await asyncio.wait_for(
                self._process.stderr.read(4096), timeout=1.0
            )
            return stderr_bytes.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""


class MCPConnectionPool:
    """Manages multiple MCP server connections with proactive health monitoring."""

    def __init__(self):
        self._connections: dict[str, MCPClient] = {}
        self._health_task: Optional[asyncio.Task] = None

    def start_health_monitor(self, interval_seconds: int = 60):
        """Start background health monitoring that prunes dead connections."""
        if self._health_task is not None and not self._health_task.done():
            return  # Already running
        self._health_task = asyncio.create_task(
            self._health_loop(interval_seconds)
        )

    async def _health_loop(self, interval: int):
        """Periodically check all MCP connections and remove dead ones."""
        while True:
            try:
                await asyncio.sleep(interval)
                results = await self.health_check()
                dead = [name for name, status in results.items() if status == "dead"]
                if dead:
                    logger.warning(
                        f"MCP health sweep: {len(dead)} dead connection(s) removed: {dead}"
                    )
                    for name in dead:
                        self._connections.pop(name, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"MCP health loop error: {e}")

    def stop_health_monitor(self):
        """Stop the background health monitor."""
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            self._health_task = None

    async def connect(
        self, name: str, command: str, env: Optional[dict] = None
    ) -> dict:
        """Connect to (or reuse) an MCP server."""
        if name in self._connections and self._connections[name].connected:
            client = self._connections[name]
            # Force reconnect if: no tools discovered (likely missing auth)
            # or env vars have changed (e.g., PAT was added)
            env_changed = env and env != client.env
            no_tools = len(client.tools) == 0
            if no_tools or env_changed:
                logger.info(
                    f"MCP '{name}': reconnecting (no_tools={no_tools}, "
                    f"env_changed={env_changed})"
                )
                await client.disconnect()
                del self._connections[name]
            else:
                return {
                    "already_connected": True,
                    "server": client.server_info,
                    "tools": [
                        {"name": t["name"], "description": t.get("description", "")}
                        for t in client.tools
                    ],
                }

        client = MCPClient(name, command, env)
        result = await client.connect()

        if "error" not in result:
            self._connections[name] = client

        return result

    async def get_client(self, name: str) -> Optional[MCPClient]:
        """Get a connected client by name, attempting auto-reconnect if disconnected."""
        client = self._connections.get(name)
        if not client:
            return None
        if client.connected:
            return client
        # Attempt auto-reconnect for known but disconnected clients
        try:
            logger.info(f"MCP '{name}': auto-reconnecting disconnected client")
            await client._try_reconnect()
            if client.connected:
                logger.info(f"MCP '{name}': auto-reconnect successful")
                return client
        except Exception as e:
            logger.warning(f"MCP '{name}': auto-reconnect failed: {e}")
        return None

    async def disconnect(self, name: str) -> dict:
        """Disconnect a specific MCP server."""
        client = self._connections.pop(name, None)
        if client:
            await client.disconnect()
            return {"success": True, "message": f"Disconnected '{name}'"}
        return {"error": f"Server '{name}' not connected"}

    async def disconnect_all(self):
        """Disconnect all servers."""
        for client in self._connections.values():
            await client.disconnect()
        self._connections.clear()

    def list_connected(self) -> list[dict]:
        """List all active connections."""
        return [
            {
                "name": name,
                "connected": client.connected,
                "server_info": client.server_info,
                "tools_count": len(client.tools),
            }
            for name, client in self._connections.items()
        ]

    def get_all_tools(self) -> list[dict]:
        """Get all tools from all connected servers."""
        all_tools = []
        for name, client in self._connections.items():
            if client.connected:
                for tool in client.tools:
                    all_tools.append({
                        "server": name,
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "input_schema": tool.get("inputSchema", {}),
                    })
        return all_tools

    async def health_check(self) -> dict[str, str]:
        """Check health of all MCP connections, reconnect dead ones.

        Returns a dict of {server_name: 'healthy' | 'reconnected' | 'dead'}.
        """
        results: dict[str, str] = {}
        for name, client in list(self._connections.items()):
            if client.connected:
                results[name] = "healthy"
            else:
                logger.warning(f"MCP '{name}' is disconnected, attempting reconnect…")
                try:
                    await client._try_reconnect()
                    if client.connected:
                        results[name] = "reconnected"
                    else:
                        results[name] = "dead"
                except Exception:
                    results[name] = "dead"
                    logger.error(f"MCP '{name}' health check: reconnect failed")
        return results


# Global connection pool
_pool = MCPConnectionPool()


def get_mcp_pool() -> MCPConnectionPool:
    """Get the global MCP connection pool."""
    return _pool

