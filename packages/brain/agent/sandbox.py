from __future__ import annotations
"""
Docker Sandbox Mode — per-session container isolation for untrusted inputs.

Inspired by OpenClaw's sandbox system:
  - agents.defaults.sandbox.mode: "non-main"
  - Non-main sessions (groups/channels) run in per-session Docker containers
  - Tool allowlists and denylists for sandboxed sessions
  - Resource limits (memory, CPU, timeout) per container

This provides security isolation without affecting the main agent session,
which runs directly on the host for full-access operations.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("brain.agent.sandbox")


class SandboxMode(str, Enum):
    """Sandbox execution modes."""
    NONE = "none"          # No sandbox — direct host execution
    NON_MAIN = "non-main"  # Only sandbox non-main sessions
    ALL = "all"            # Sandbox everything (strictest)


@dataclass
class SandboxConfig:
    """Configuration for a sandboxed session."""
    mode: SandboxMode = SandboxMode.NON_MAIN
    image: str = "python:3.12-slim"
    memory_limit: str = "256m"
    cpu_limit: float = 0.5
    timeout_seconds: int = 300
    network_enabled: bool = False
    volume_mounts: list[str] = field(default_factory=list)

    # Tool access control
    allowed_tools: list[str] = field(default_factory=lambda: [
        "read_file", "write_file", "edit_file",
        "execute_code", "search_files",
        "sessions_list", "sessions_history", "sessions_send",
    ])
    denied_tools: list[str] = field(default_factory=lambda: [
        "browser", "execute_command", "cron_create",
        "webhook_create", "delegate_task",
    ])


@dataclass
class SandboxInstance:
    """A running sandbox container."""
    id: str
    session_id: str
    container_id: Optional[str] = None
    status: str = "creating"  # creating, running, stopped, failed
    config: SandboxConfig = field(default_factory=SandboxConfig)
    created_at: str = ""


class SandboxManager:
    """
    Manages Docker sandbox containers for agent sessions.

    Provides secure, isolated execution environments for non-main
    sessions, preventing untrusted channel inputs from accessing
    host resources.
    """

    def __init__(self, default_config: SandboxConfig = None):
        self._config = default_config or SandboxConfig()
        self._instances: dict[str, SandboxInstance] = {}
        self._docker_available = None

    async def check_docker(self) -> bool:
        """Check if Docker is available on the host."""
        if self._docker_available is not None:
            return self._docker_available

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "version", "--format", "{{.Server.Version}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            self._docker_available = proc.returncode == 0
            if self._docker_available:
                logger.info(f"Docker available: v{stdout.decode().strip()}")
            else:
                logger.warning("Docker not available")
        except (FileNotFoundError, asyncio.TimeoutError):
            self._docker_available = False
            logger.warning("Docker not found on system")

        return self._docker_available

    def should_sandbox(self, session_type: str) -> bool:
        """Determine if a session should be sandboxed."""
        if self._config.mode == SandboxMode.NONE:
            return False
        if self._config.mode == SandboxMode.ALL:
            return True
        # NON_MAIN mode — only sandbox non-main sessions
        return session_type != "main"

    def is_tool_allowed(self, tool_name: str, session_id: str) -> bool:
        """Check if a tool is allowed in a sandboxed session."""
        instance = self._instances.get(session_id)
        if not instance:
            return True  # Not sandboxed

        config = instance.config

        # Explicit deny takes precedence
        if tool_name in config.denied_tools:
            return False

        # If allowlist is defined, tool must be in it
        if config.allowed_tools:
            return tool_name in config.allowed_tools

        return True

    async def create_sandbox(
        self,
        session_id: str,
        config: SandboxConfig = None,
    ) -> SandboxInstance:
        """Create a new sandbox container for a session."""
        cfg = config or self._config
        sandbox_id = str(uuid.uuid4())[:12]

        instance = SandboxInstance(
            id=sandbox_id,
            session_id=session_id,
            config=cfg,
        )
        self._instances[session_id] = instance

        if not await self.check_docker():
            instance.status = "failed"
            logger.error("Cannot create sandbox: Docker not available")
            return instance

        container_name = f"kestrel-sandbox-{sandbox_id}"

        # Build docker run command
        cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "--memory", cfg.memory_limit,
            "--cpus", str(cfg.cpu_limit),
            "--rm",
            "--label", f"kestrel.session={session_id}",
            "--label", f"kestrel.sandbox={sandbox_id}",
        ]

        # Network isolation
        if not cfg.network_enabled:
            cmd.extend(["--network", "none"])

        # Volume mounts (read-only workspace)
        for mount in cfg.volume_mounts:
            cmd.extend(["-v", f"{mount}:ro"])

        # Image and keep-alive command
        cmd.extend([cfg.image, "tail", "-f", "/dev/null"])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode == 0:
                instance.container_id = stdout.decode().strip()[:12]
                instance.status = "running"
                logger.info(f"Sandbox created: {container_name} ({instance.container_id})")
            else:
                instance.status = "failed"
                logger.error(f"Sandbox creation failed: {stderr.decode()}")

        except asyncio.TimeoutError:
            instance.status = "failed"
            logger.error("Sandbox creation timed out")
        except Exception as e:
            instance.status = "failed"
            logger.error(f"Sandbox creation error: {e}")

        return instance

    async def execute_in_sandbox(
        self,
        session_id: str,
        code: str,
        language: str = "python",
        timeout: int = 60,
    ) -> dict[str, Any]:
        """Execute code inside a sandbox container."""
        instance = self._instances.get(session_id)

        if not instance or instance.status != "running":
            return {
                "success": False,
                "error": "No active sandbox for this session",
            }

        # Write code to container via stdin
        exec_cmd = ["docker", "exec", "-i", instance.container_id]

        if language == "python":
            exec_cmd.extend(["python3", "-c", code])
        elif language == "bash":
            exec_cmd.extend(["bash", "-c", code])
        else:
            return {"success": False, "error": f"Unsupported language: {language}"}

        try:
            proc = await asyncio.create_subprocess_exec(
                *exec_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=min(timeout, instance.config.timeout_seconds),
            )

            return {
                "success": proc.returncode == 0,
                "stdout": stdout.decode(errors="replace")[:10_000],
                "stderr": stderr.decode(errors="replace")[:5_000],
                "exit_code": proc.returncode,
            }

        except asyncio.TimeoutError:
            # Kill the process inside container
            try:
                await asyncio.create_subprocess_exec(
                    "docker", "exec", instance.container_id,
                    "pkill", "-f", "python3" if language == "python" else "bash",
                )
            except Exception:
                pass

            return {
                "success": False,
                "error": f"Execution timed out after {timeout}s",
                "exit_code": -1,
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Sandbox execution error: {e}",
                "exit_code": -1,
            }

    async def destroy_sandbox(self, session_id: str) -> bool:
        """Stop and remove a sandbox container."""
        instance = self._instances.pop(session_id, None)
        if not instance or not instance.container_id:
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", instance.container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            instance.status = "stopped"
            logger.info(f"Sandbox destroyed: {instance.container_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to destroy sandbox: {e}")
            return False

    async def cleanup_all(self) -> int:
        """Destroy all sandbox containers (e.g., on shutdown)."""
        count = 0
        for session_id in list(self._instances.keys()):
            if await self.destroy_sandbox(session_id):
                count += 1
        return count

    def get_status(self) -> dict:
        """Get sandbox system status."""
        return {
            "docker_available": self._docker_available,
            "mode": self._config.mode.value,
            "active_sandboxes": len([
                i for i in self._instances.values()
                if i.status == "running"
            ]),
            "total_created": len(self._instances),
        }
