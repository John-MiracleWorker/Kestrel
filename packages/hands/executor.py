"""
Docker-based sandbox executor for running skills securely.
Manages container lifecycle, resource limits, and output collection.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("hands.executor")

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "kestrel/sandbox:latest")
MAX_CONCURRENT = int(os.getenv("SANDBOX_MAX_CONCURRENT", "10"))

# When running inside Docker, the skill_path is a container-internal path
# (e.g., /skills/web). The host Docker daemon cannot resolve this path.
# Set HOST_SKILLS_DIR to the host-side absolute path so volume mounts work.
HOST_SKILLS_DIR = os.getenv("HOST_SKILLS_DIR", "")
CONTAINER_SKILLS_DIR = os.getenv("SKILLS_DIR", "/skills")


class DockerExecutor:
    """Manages sandboxed skill execution in Docker containers."""

    def __init__(self):
        self._docker = None
        self._active = 0
        self._max_concurrent = MAX_CONCURRENT

    @property
    def active_sandboxes(self) -> int:
        return self._active

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def _get_client(self):
        """Lazy-load Docker client."""
        if self._docker is None:
            try:
                import docker
                self._docker = docker.from_env()
                logger.info("Docker client connected")
            except Exception as e:
                logger.error(f"Docker not available: {e}")
                raise RuntimeError("Docker is required for sandboxed execution")
        return self._docker

    async def run(
        self,
        skill_path: str,
        function_name: str,
        arguments: str,
        limits: dict,
        allowed_domains: list[str] = None,
        allowed_paths: list[str] = None,
    ) -> dict:
        """Run a skill function in a sandboxed Docker container."""
        if self._active >= self._max_concurrent:
            raise RuntimeError("Maximum concurrent sandboxes reached")

        self._active += 1
        start_time = time.time()
        audit_log = {
            "network_requests": [],
            "file_accesses": [],
            "system_calls": [],
        }

        try:
            client = self._get_client()
            loop = asyncio.get_event_loop()

            # Build container configuration
            container_config = self._build_config(
                skill_path, function_name, arguments,
                limits, allowed_domains, allowed_paths,
            )

            # Run container in executor (docker-py is sync)
            result = await loop.run_in_executor(
                None,
                lambda: self._run_container(client, container_config, limits["timeout"])
            )

            elapsed_ms = int((time.time() - start_time) * 1000)

            return {
                "output": result.get("output", ""),
                "execution_time_ms": elapsed_ms,
                "memory_used_mb": result.get("memory_used_mb", 0),
                "audit_log": {
                    **audit_log,
                    "sandbox_id": result.get("container_id", ""),
                },
            }

        finally:
            self._active -= 1

    def _build_config(self, skill_path, function_name, arguments,
                      limits, allowed_domains, allowed_paths):
        """Build Docker container configuration."""
        env = {
            "SKILL_FUNCTION": function_name,
            "SKILL_ARGUMENTS": arguments,
        }

        if allowed_domains:
            env["ALLOWED_DOMAINS"] = ",".join(allowed_domains)

        # Translate container-internal skill path to host path for Docker volume mounts.
        # When running in Docker-in-Docker (DinD), the host Docker daemon needs
        # the host-side absolute path, not the path inside the Hands container.
        mount_path = skill_path
        if HOST_SKILLS_DIR and CONTAINER_SKILLS_DIR and skill_path.startswith(CONTAINER_SKILLS_DIR):
            relative = skill_path[len(CONTAINER_SKILLS_DIR):]
            mount_path = HOST_SKILLS_DIR + relative
            logger.debug(f"Translated skill path: {skill_path} -> {mount_path}")

        volumes = {
            mount_path: {"bind": "/skill", "mode": "ro"},
        }

        # Add allowed filesystem paths as read-only mounts
        if allowed_paths:
            for i, p in enumerate(allowed_paths):
                mode = "rw" if limits.get("fs_write") else "ro"
                volumes[p] = {"bind": f"/mnt/host/{i}", "mode": mode}

        return {
            "image": SANDBOX_IMAGE,
            "environment": env,
            "volumes": volumes,
            "mem_limit": f"{limits['memory_mb']}m",
            "cpus": limits["cpu_limit"],
            "network_disabled": not limits.get("network", False),
            "read_only": not limits.get("fs_write", False),
            "detach": True,
            "auto_remove": False,
        }

    def _run_container(self, client, config, timeout):
        """Synchronous container execution."""
        container = None
        try:
            container = client.containers.run(**config)
            container.wait(timeout=timeout)

            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")

            # Parse structured output (JSON on last line)
            lines = logs.strip().split("\n")
            output = logs

            try:
                # Try to parse last line as JSON result
                result_line = lines[-1] if lines else "{}"
                parsed = json.loads(result_line)
                output = json.dumps(parsed)
            except (json.JSONDecodeError, IndexError):
                pass

            stats = container.stats(stream=False)
            memory_used = stats.get("memory_stats", {}).get("usage", 0)

            return {
                "output": output,
                "container_id": container.id[:12],
                "memory_used_mb": memory_used // (1024 * 1024),
            }

        except Exception as e:
            if container:
                try:
                    container.kill()
                except Exception:
                    pass
            raise

        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    async def cleanup(self):
        """Clean up any orphaned sandbox containers."""
        if not self._docker:
            return

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._cleanup_sync)
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")

    def _cleanup_sync(self):
        containers = self._docker.containers.list(
            filters={"ancestor": SANDBOX_IMAGE}
        )
        for c in containers:
            logger.info(f"Cleaning up orphaned sandbox: {c.id[:12]}")
            c.remove(force=True)
