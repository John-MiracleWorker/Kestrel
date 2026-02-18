"""
Hands Service — gRPC server for sandboxed tool/skill execution.

Runs skills inside Docker containers with resource limits,
network isolation, and full audit logging.
"""

import asyncio
import logging
import os
import json
import uuid
from concurrent import futures

import grpc
from grpc import aio as grpc_aio
from dotenv import load_dotenv

from executor import DockerExecutor
from security.allowlist import PermissionChecker
from security.audit import AuditLogger

load_dotenv()
logger = logging.getLogger("hands")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

GRPC_PORT = int(os.getenv("HANDS_GRPC_PORT", "50052"))
GRPC_HOST = os.getenv("HANDS_GRPC_HOST", "0.0.0.0")

# ── Skill Registry ───────────────────────────────────────────────────

_skills: dict[str, dict] = {}


def load_skills(skills_dir: str = None):
    """Load skill metadata from the skills directory."""
    skills_dir = skills_dir or os.getenv("SKILLS_DIR", "./skills")
    if not os.path.isdir(skills_dir):
        logger.warning(f"Skills directory not found: {skills_dir}")
        return []

    for name in os.listdir(skills_dir):
        skill_path = os.path.join(skills_dir, name)
        manifest_path = os.path.join(skill_path, "manifest.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                _skills[name] = {
                    "name": name,
                    "path": skill_path,
                    "manifest": manifest,
                }
                logger.info(f"Loaded skill: {name}")
            except Exception as e:
                logger.error(f"Failed to load skill {name}: {e}")

    return list(_skills.values())


# ── gRPC Servicer ────────────────────────────────────────────────────

class HandsServicer:
    """Implements kestrel.hands.HandsService."""

    def __init__(self, executor: DockerExecutor, permissions: PermissionChecker,
                 audit: AuditLogger):
        self.executor = executor
        self.permissions = permissions
        self.audit = audit

    async def ExecuteSkill(self, request, context):
        """Execute a skill in a sandboxed Docker container."""
        skill_name = request.skill_name
        function_name = request.function_name
        arguments = request.arguments
        user_id = request.user_id
        workspace_id = request.workspace_id

        logger.info(f"ExecuteSkill: {skill_name}.{function_name} "
                     f"user={user_id} workspace={workspace_id}")

        # Check permissions
        if not self.permissions.check(workspace_id, skill_name, function_name):
            yield {
                "status": 4,  # PERMISSION_DENIED
                "error": f"Skill '{skill_name}' not allowed in workspace",
            }
            return

        # Find skill
        skill = _skills.get(skill_name)
        if not skill:
            yield {
                "status": 2,  # ERROR
                "error": f"Unknown skill: {skill_name}",
            }
            return

        # Build resource limits from request or defaults
        limits = {
            "timeout": getattr(request.limits, "timeout_seconds", 30) or 30,
            "memory_mb": getattr(request.limits, "memory_mb", 512) or 512,
            "cpu_limit": getattr(request.limits, "cpu_limit", 1.0) or 1.0,
            "network": getattr(request.limits, "network_enabled", False),
            "fs_read": getattr(request.limits, "file_system_read", False),
            "fs_write": getattr(request.limits, "file_system_write", False),
        }

        # Start audit
        exec_id = str(uuid.uuid4())
        self.audit.log_start(exec_id, user_id, workspace_id, skill_name,
                             function_name, arguments)

        # Yield RUNNING status
        yield {
            "status": 0,  # RUNNING
            "output": f"Executing {skill_name}.{function_name}...",
        }

        try:
            # Execute in Docker sandbox
            result = await self.executor.run(
                skill_path=skill["path"],
                function_name=function_name,
                arguments=arguments,
                limits=limits,
                allowed_domains=list(request.allowed_domains),
                allowed_paths=list(request.allowed_paths),
            )

            self.audit.log_complete(
                exec_id,
                status="success",
                execution_time_ms=result.get("execution_time_ms", 0),
                memory_used_mb=result.get("memory_used_mb", 0),
                audit_log=result.get("audit_log", {}),
            )

            yield {
                "status": 1,  # SUCCESS
                "output": result.get("output", ""),
                "execution_time_ms": result.get("execution_time_ms", 0),
                "memory_used_mb": result.get("memory_used_mb", 0),
                "audit_log": result.get("audit_log", {}),
            }

        except asyncio.TimeoutError:
            self.audit.log_complete(exec_id, status="timeout")
            yield {
                "status": 3,  # TIMEOUT
                "error": f"Skill execution timed out after {limits['timeout']}s",
            }

        except Exception as e:
            self.audit.log_complete(exec_id, status="error", error=str(e))
            yield {
                "status": 2,  # ERROR
                "error": str(e),
            }

    async def ListSkills(self, request, context):
        """List available skills for a workspace."""
        workspace_id = request.workspace_id
        skills = []

        for name, skill in _skills.items():
            if not self.permissions.check(workspace_id, name):
                continue

            manifest = skill["manifest"]
            functions = []
            for fn in manifest.get("functions", []):
                functions.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "parameters_schema": json.dumps(fn.get("parameters", {})),
                })

            skills.append({
                "name": name,
                "description": manifest.get("description", ""),
                "version": manifest.get("version", "0.1.0"),
                "functions": functions,
                "requires_network": manifest.get("requires_network", False),
                "requires_filesystem": manifest.get("requires_filesystem", False),
            })

        return {"skills": skills}

    async def HealthCheck(self, request, context):
        """Return health status."""
        active = self.executor.active_sandboxes
        capacity = self.executor.max_concurrent - active
        return {
            "healthy": True,
            "version": "0.1.0",
            "active_sandboxes": active,
            "available_capacity": capacity,
        }


# ── Server Bootstrap ─────────────────────────────────────────────────

async def serve():
    # Initialize components
    executor = DockerExecutor()
    permissions = PermissionChecker()
    audit = AuditLogger()

    # Load skills
    load_skills()

    server = grpc_aio.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=[
            ("grpc.max_send_message_length", 50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
        ],
    )

    servicer = HandsServicer(executor, permissions, audit)

    # Generate and load proto stubs
    from grpc_tools import protoc
    import sys

    proto_path = os.path.join(os.path.dirname(__file__), "../shared/proto")
    out_dir = os.path.join(os.path.dirname(__file__), "_generated")
    os.makedirs(out_dir, exist_ok=True)

    protoc.main([
        "grpc_tools.protoc",
        f"-I{proto_path}",
        f"--python_out={out_dir}",
        f"--grpc_python_out={out_dir}",
        "hands.proto",
    ])

    sys.path.insert(0, out_dir)
    import hands_pb2
    import hands_pb2_grpc

    hands_pb2_grpc.add_HandsServiceServicer_to_server(servicer, server)

    # Enable reflection
    from grpc_reflection.v1alpha import reflection as grpc_reflection
    service_names = (
        hands_pb2.DESCRIPTOR.services_by_name["HandsService"].full_name,
        grpc_reflection.SERVICE_NAME,
    )
    grpc_reflection.enable_server_reflection(service_names, server)

    bind_address = f"{GRPC_HOST}:{GRPC_PORT}"
    server.add_insecure_port(bind_address)

    logger.info(f"Hands gRPC server starting on {bind_address}")
    await server.start()
    logger.info("Hands service ready")

    try:
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down Hands service...")
        await server.stop(5)
        await executor.cleanup()


if __name__ == "__main__":
    asyncio.run(serve())
