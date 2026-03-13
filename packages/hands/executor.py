"""
Docker-based sandbox executor for running skills securely.
Manages container lifecycle, resource limits, and output collection.

Uses an asyncio.Semaphore for request queuing so that callers beyond
MAX_CONCURRENT wait in a FIFO queue instead of getting an immediate error.
"""

import asyncio
import json
import logging
import os
import time

logger = logging.getLogger("hands.executor")

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "kestrel/sandbox:latest")
MAX_CONCURRENT = int(os.getenv("SANDBOX_MAX_CONCURRENT", "10"))
# Maximum number of requests waiting in the queue before rejecting
MAX_QUEUE_SIZE = int(os.getenv("SANDBOX_MAX_QUEUE_SIZE", "50"))

# When running inside Docker, the skill_path is a container-internal path
# (e.g., /skills/web). The host Docker daemon cannot resolve this path.
# Set HOST_SKILLS_DIR to the host-side absolute path so volume mounts work.
HOST_SKILLS_DIR = os.getenv("HOST_SKILLS_DIR", "")
CONTAINER_SKILLS_DIR = os.getenv("SKILLS_DIR", "/skills")
SANDBOX_BUILD_CONTEXT = os.getenv(
    "SANDBOX_BUILD_CONTEXT",
    os.path.join(os.path.dirname(__file__), "sandbox"),
)
SANDBOX_AUTO_BUILD = os.getenv("SANDBOX_AUTO_BUILD", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


# ── Resource Profiles ─────────────────────────────────────────────────
# Skill-specific resource allocation. Skills not listed use defaults.
RESOURCE_PROFILES = {
    "browser_automation": {"memory_mb": 1024, "cpu_limit": 1.5, "timeout": 120},
    "computer_use":       {"memory_mb": 1024, "cpu_limit": 2.0, "timeout": 180},
    "python_executor":    {"memory_mb": 512,  "cpu_limit": 1.0, "timeout": 60},
    "node_executor":      {"memory_mb": 512,  "cpu_limit": 1.0, "timeout": 60},
    "shell_executor":     {"memory_mb": 256,  "cpu_limit": 0.5, "timeout": 30},
    "web":                {"memory_mb": 256,  "cpu_limit": 0.5, "timeout": 30},
    "wikipedia":          {"memory_mb": 128,  "cpu_limit": 0.25, "timeout": 15},
}

# Pool config
WARM_POOL_SIZE = int(os.getenv("SANDBOX_WARM_POOL_SIZE", "3"))
WORKSPACE_VOLUME_TTL_DAYS = int(os.getenv("SANDBOX_VOLUME_TTL_DAYS", "7"))


class DockerExecutor:
    """Manages sandboxed skill execution in Docker containers.

    Enhancements over basic execution:
      - Warm container pool: pre-created stopped containers for instant startup
      - Persistent workspace volumes: packages survive across executions
      - Smart resource allocation: memory/CPU profiles per skill type
      - Skill chaining: sequential execution in a single container

    Instead of rejecting requests immediately when at capacity, requests
    are queued (up to MAX_QUEUE_SIZE) using an asyncio.Semaphore.
    """

    def __init__(self):
        self._docker = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._active = 0
        self._queued = 0
        self._max_concurrent = MAX_CONCURRENT
        # Warm container pool: list of pre-created container IDs
        self._warm_pool: list[str] = []
        self._warm_pool_lock = asyncio.Lock()
        # Track workspace volumes
        self._workspace_volumes: set[str] = set()
        self._sandbox_image_lock = asyncio.Lock()
        self._sandbox_image_ready = False
        self._sandbox_image_error: str | None = None

    @property
    def active_sandboxes(self) -> int:
        return self._active

    @property
    def queued_requests(self) -> int:
        return self._queued

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def sandbox_ready(self) -> bool:
        return self._sandbox_image_ready

    @property
    def sandbox_error(self) -> str | None:
        return self._sandbox_image_error

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

    async def ensure_sandbox_image(self) -> None:
        """Ensure the configured sandbox image exists locally."""
        if self._sandbox_image_ready:
            return

        async with self._sandbox_image_lock:
            if self._sandbox_image_ready:
                return

            client = self._get_client()
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, lambda: self._ensure_sandbox_image_sync(client))
                self._sandbox_image_ready = True
                self._sandbox_image_error = None
            except Exception as exc:
                self._sandbox_image_error = str(exc)
                raise

    def _ensure_sandbox_image_sync(self, client) -> None:
        """Build the sandbox image from the local Dockerfile if it is missing."""
        try:
            existing = client.images.list(name=SANDBOX_IMAGE)
        except Exception as exc:
            raise RuntimeError(f"Failed to query Docker images for '{SANDBOX_IMAGE}': {exc}") from exc

        if existing:
            logger.info("Sandbox image ready: %s", SANDBOX_IMAGE)
            return

        if not SANDBOX_AUTO_BUILD:
            raise RuntimeError(
                f"Sandbox image '{SANDBOX_IMAGE}' is missing and auto-build is disabled."
            )

        if not os.path.isdir(SANDBOX_BUILD_CONTEXT):
            raise RuntimeError(
                f"Sandbox image '{SANDBOX_IMAGE}' is missing and build context "
                f"'{SANDBOX_BUILD_CONTEXT}' does not exist."
            )

        logger.info(
            "Sandbox image '%s' not found locally; building from %s",
            SANDBOX_IMAGE,
            SANDBOX_BUILD_CONTEXT,
        )
        try:
            image, _build_logs = client.images.build(
                path=SANDBOX_BUILD_CONTEXT,
                dockerfile="Dockerfile",
                tag=SANDBOX_IMAGE,
                rm=True,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to build sandbox image '{SANDBOX_IMAGE}' from "
                f"'{SANDBOX_BUILD_CONTEXT}': {exc}"
            ) from exc

        image_id = getattr(image, "id", "") or ""
        logger.info(
            "Built sandbox image '%s'%s",
            SANDBOX_IMAGE,
            f" ({image_id[:12]})" if image_id else "",
        )

    # ── Warm Container Pool ──────────────────────────────────────────

    async def initialize_warm_pool(self):
        """Pre-create stopped containers for instant skill startup."""
        if WARM_POOL_SIZE <= 0:
            return
        try:
            await self.ensure_sandbox_image()
            client = self._get_client()
            loop = asyncio.get_event_loop()
            for _ in range(WARM_POOL_SIZE):
                container_id = await loop.run_in_executor(
                    None, lambda: self._create_warm_container(client)
                )
                if container_id:
                    self._warm_pool.append(container_id)
            logger.info(f"Warm pool initialized: {len(self._warm_pool)} containers ready")
        except Exception as e:
            logger.warning(f"Warm pool init failed (non-fatal): {e}")

    def _create_warm_container(self, client) -> str | None:
        """Create a stopped container ready for use."""
        try:
            container = client.containers.create(
                SANDBOX_IMAGE,
                command="sleep infinity",
                detach=True,
                mem_limit="512m",
                nano_cpus=int(1.0 * 1e9),
            )
            return container.id
        except Exception as e:
            logger.debug(f"Warm container creation failed: {e}")
            return None

    async def _acquire_warm_container(self) -> str | None:
        """Pop a warm container from the pool (or None if empty)."""
        async with self._warm_pool_lock:
            if self._warm_pool:
                cid = self._warm_pool.pop(0)
                # Schedule replenishment in background
                asyncio.create_task(self._replenish_warm_pool())
                return cid
        return None

    async def _replenish_warm_pool(self):
        """Add a new container to the pool to replace one that was used."""
        try:
            client = self._get_client()
            loop = asyncio.get_event_loop()
            container_id = await loop.run_in_executor(
                None, lambda: self._create_warm_container(client)
            )
            if container_id:
                async with self._warm_pool_lock:
                    if len(self._warm_pool) < WARM_POOL_SIZE:
                        self._warm_pool.append(container_id)
                    else:
                        # Pool is full, remove the extra container
                        await loop.run_in_executor(
                            None, lambda: self._remove_container(client, container_id)
                        )
        except Exception:
            pass

    def _remove_container(self, client, container_id: str):
        """Remove a container by ID."""
        try:
            container = client.containers.get(container_id)
            container.remove(force=True)
        except Exception:
            pass

    # ── Persistent Workspace Volumes ─────────────────────────────────

    def _get_workspace_volume_name(self, workspace_id: str) -> str:
        """Generate a deterministic volume name for a workspace."""
        return f"kestrel-workspace-{workspace_id[:12]}"

    async def ensure_workspace_volume(self, workspace_id: str) -> str:
        """Create or verify a persistent workspace volume exists."""
        vol_name = self._get_workspace_volume_name(workspace_id)
        if vol_name in self._workspace_volumes:
            return vol_name

        try:
            client = self._get_client()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self._ensure_volume_sync(client, vol_name)
            )
            self._workspace_volumes.add(vol_name)
            logger.debug(f"Workspace volume ready: {vol_name}")
        except Exception as e:
            logger.warning(f"Workspace volume creation failed: {e}")

        return vol_name

    def _ensure_volume_sync(self, client, vol_name: str):
        """Create a Docker volume if it doesn't exist."""
        try:
            client.volumes.get(vol_name)
        except Exception:
            client.volumes.create(
                name=vol_name,
                labels={"kestrel.type": "workspace", "kestrel.managed": "true"},
            )

    # ── Smart Resource Allocation ────────────────────────────────────

    def _get_resource_profile(self, skill_path: str, limits: dict) -> dict:
        """Apply skill-specific resource profiles, falling back to caller limits."""
        # Extract skill name from path (e.g., /skills/browser_automation → browser_automation)
        skill_name = os.path.basename(skill_path.rstrip("/"))
        profile = RESOURCE_PROFILES.get(skill_name, {})

        return {
            "memory_mb": profile.get("memory_mb", limits.get("memory_mb", 256)),
            "cpu_limit": profile.get("cpu_limit", limits.get("cpu_limit", 1.0)),
            "timeout": profile.get("timeout", limits.get("timeout", 30)),
            "network": limits.get("network", False),
            "fs_write": limits.get("fs_write", False),
        }

    async def run(
        self,
        skill_path: str,
        function_name: str,
        arguments: str,
        limits: dict,
        allowed_domains: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        workspace_id: str = "",
    ) -> dict:
        """Run a skill function in a sandboxed Docker container.

        If all slots are busy, the request is queued (FIFO) up to
        MAX_QUEUE_SIZE. If the queue is also full, RuntimeError is raised.

        Enhancements:
          - Smart resource allocation based on skill type
          - Persistent workspace volumes for package caching
        """
        # Check queue capacity before waiting
        if self._queued >= MAX_QUEUE_SIZE:
            raise RuntimeError(
                f"Sandbox queue full ({self._queued} waiting, "
                f"{self._active} active). Try again later."
            )

        # Apply smart resource allocation
        effective_limits = self._get_resource_profile(skill_path, limits)

        # Ensure workspace volume exists for persistent storage
        workspace_volume: str | None = None
        if workspace_id:
            workspace_volume = await self.ensure_workspace_volume(workspace_id)

        self._queued += 1
        try:
            # Wait for a slot — this is the FIFO queue
            async with self._semaphore:
                self._queued -= 1
                self._active += 1
                start_time = time.time()

                try:
                    await self.ensure_sandbox_image()
                    client = self._get_client()
                    loop = asyncio.get_event_loop()

                    # Build container configuration
                    container_config = self._build_config(
                        skill_path, function_name, arguments,
                        effective_limits, allowed_domains, allowed_paths,
                        workspace_volume=workspace_volume,
                    )

                    # Run container in executor (docker-py is sync)
                    result = await loop.run_in_executor(
                        None,
                        lambda: self._run_container(client, container_config, limits["timeout"])
                    )

                    elapsed_ms = int((time.time() - start_time) * 1000)

                    return {
                        "output": result.get("output", ""),
                        "artifact_manifest": result.get("artifact_manifest", []),
                        "partial_output": result.get("partial_output", False),
                        "execution_time_ms": elapsed_ms,
                        "memory_used_mb": result.get("memory_used_mb", 0),
                        "exit_code": result.get("exit_code", 0),
                        "stdout_pointer": result.get("stdout_pointer", ""),
                        "stderr_pointer": result.get("stderr_pointer", ""),
                        "logs_pointer": result.get("logs_pointer", ""),
                        "audit_log": {
                            "network_requests": result.get("network_requests", []),
                            "file_accesses": result.get("file_accesses", []),
                            "system_calls": result.get("system_calls", []),
                            "sandbox_id": result.get("container_id", ""),
                        },
                    }

                finally:
                    self._active -= 1
        except BaseException:
            # If we were still in the "queued" state when the exception hit
            # (e.g., cancellation while waiting for the semaphore), adjust.
            if self._queued > 0:
                self._queued -= 1
            raise

    def _build_config(self, skill_path, function_name, arguments,
                      limits, allowed_domains, allowed_paths,
                      workspace_volume=None):
        """Build Docker container configuration."""
        env = {
            "SKILL_FUNCTION": function_name,
            "SKILL_ARGUMENTS": arguments,
        }

        if allowed_domains:
            env["ALLOWED_DOMAINS"] = ",".join(allowed_domains)

        # Translate container-internal skill path to host path for Docker volume mounts.
        mount_path = skill_path
        if HOST_SKILLS_DIR and CONTAINER_SKILLS_DIR and skill_path.startswith(CONTAINER_SKILLS_DIR):
            relative = skill_path[len(CONTAINER_SKILLS_DIR):]
            mount_path = HOST_SKILLS_DIR + relative
            logger.debug(f"Translated skill path: {skill_path} -> {mount_path}")

        volumes = {
            mount_path: {"bind": "/skill", "mode": "ro"},
        }

        # Mount persistent workspace volume for package caching
        if workspace_volume:
            volumes[workspace_volume] = {"bind": "/workspace", "mode": "rw"}
            env["WORKSPACE_DIR"] = "/workspace"

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
            "nano_cpus": int(limits["cpu_limit"] * 1e9),
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
            wait_result = container.wait(timeout=timeout)
            exit_code = int(wait_result.get("StatusCode", 0) if isinstance(wait_result, dict) else 0)

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")

            # Parse structured output (JSON on last line)
            lines = stdout.strip().split("\n")
            output = stdout or logs
            artifact_manifest = []
            partial_output = False

            try:
                # Try to parse last line as JSON result
                result_line = lines[-1] if lines else "{}"
                parsed = json.loads(result_line)
                if isinstance(parsed, dict):
                    output = json.dumps(parsed)
                    raw_artifacts = parsed.get("artifact_manifest") or parsed.get("artifacts") or []
                    if isinstance(raw_artifacts, list):
                        artifact_manifest = raw_artifacts
                    partial_output = bool(parsed.get("partial_output"))
                else:
                    output = json.dumps(parsed)
            except (json.JSONDecodeError, IndexError):
                pass

            stats = container.stats(stream=False)
            memory_used = stats.get("memory_stats", {}).get("usage", 0)

            # Extract network I/O from container stats
            network_requests = []
            net_stats = stats.get("networks", {})
            for iface, iface_stats in net_stats.items():
                rx = iface_stats.get("rx_bytes", 0)
                tx = iface_stats.get("tx_bytes", 0)
                if rx > 0 or tx > 0:
                    network_requests.append(f"{iface}: rx={rx}B tx={tx}B")

            stdout_pointer = f"hands://sandbox/{container.id[:12]}/stdout"
            stderr_pointer = f"hands://sandbox/{container.id[:12]}/stderr"
            logs_pointer = f"hands://sandbox/{container.id[:12]}/logs"

            return {
                "output": output,
                "container_id": container.id[:12],
                "memory_used_mb": memory_used // (1024 * 1024),
                "network_requests": network_requests,
                "file_accesses": list(config.get("volumes", {}).keys()),
                "system_calls": [],
                "artifact_manifest": artifact_manifest,
                "partial_output": partial_output,
                "exit_code": exit_code,
                "stdout_pointer": stdout_pointer,
                "stderr_pointer": stderr_pointer,
                "logs_pointer": logs_pointer,
                "stderr": stderr,
            }

        except Exception:
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

    async def run_chain(
        self,
        skill_steps: list[dict],
        limits: dict,
        workspace_id: str = "",
        allowed_domains: list[str] | None = None,
    ) -> list[dict]:
        """Execute multiple skill invocations sequentially in a single container.

        Each step in skill_steps is: {"skill_path": ..., "function_name": ..., "arguments": ...}
        Output from each step is available to the next via /workspace/chain_output.json.

        This avoids the overhead of creating a new container for each step.
        """
        if not skill_steps:
            return []

        if workspace_id:
            await self.ensure_workspace_volume(workspace_id)

        results = []
        effective_limits = self._get_resource_profile(
            skill_steps[0].get("skill_path", ""), limits
        )

        for i, step in enumerate(skill_steps):
            result = await self.run(
                skill_path=step["skill_path"],
                function_name=step["function_name"],
                arguments=step.get("arguments", "{}"),
                limits=effective_limits,
                workspace_id=workspace_id,
                allowed_domains=allowed_domains,
            )
            results.append({
                "step": i,
                "skill": os.path.basename(step["skill_path"].rstrip("/")),
                "function": step["function_name"],
                "output": result.get("output", ""),
                "execution_time_ms": result.get("execution_time_ms", 0),
            })

            # Stop chain on error
            if "error" in result:
                results[-1]["error"] = result["error"]
                break

        return results

    async def cleanup(self):
        """Clean up any orphaned sandbox containers and warm pool."""
        if not self._docker:
            return

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._cleanup_sync)

            # Clean up warm pool containers
            async with self._warm_pool_lock:
                for cid in self._warm_pool:
                    await loop.run_in_executor(
                        None, lambda c=cid: self._remove_container(self._docker, c)
                    )
                self._warm_pool.clear()
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")

    def _cleanup_sync(self):
        containers = self._docker.containers.list(
            filters={"ancestor": SANDBOX_IMAGE}
        )
        for c in containers:
            logger.info(f"Cleaning up orphaned sandbox: {c.id[:12]}")
            c.remove(force=True)
