"""
Hands Service — gRPC server for sandboxed tool/skill execution.

Runs skills inside Docker containers with resource limits,
network isolation, and full audit logging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from concurrent import futures

from grpc import aio as grpc_aio
from dotenv import load_dotenv

from executor import DockerExecutor
from grpc_setup import hands_pb2, hands_pb2_grpc
from security.allowlist import PermissionChecker
from security.audit import AuditLogger
from shared_schemas import (
    FAILURE_CLASS_EXECUTION_ERROR,
    FAILURE_CLASS_NONE,
    FAILURE_CLASS_PARTIAL_OUTPUT,
    FAILURE_CLASS_SANDBOX_CRASH,
    FAILURE_CLASS_TIMEOUT,
    build_action_receipt,
    build_execution_action_event,
    classify_risk_class,
    classify_runtime_class,
    dumps_action_event,
    stable_hash,
)

load_dotenv()
logger = logging.getLogger("hands")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

GRPC_PORT = int(os.getenv("HANDS_GRPC_PORT", "50052"))
GRPC_HOST = os.getenv("HANDS_GRPC_HOST", "0.0.0.0")

# ── Skill Registry ───────────────────────────────────────────────────

_skills: dict[str, dict] = {}


def _classify_execution_context(skill_name: str, limits: dict) -> tuple[str, str]:
    runtime_class = classify_runtime_class("docker")
    risk_class = classify_risk_class(
        action_type=skill_name,
        network_enabled=bool(limits.get("network")),
        file_system_write=bool(limits.get("fs_write")),
    )
    return runtime_class, risk_class


def _request_grants(request) -> list[dict]:
    grants = []
    for grant in getattr(request, "grants", []):
        metadata = {}
        if getattr(grant, "metadata_json", ""):
            try:
                metadata = json.loads(grant.metadata_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
        grants.append(
            {
                "grant_id": grant.grant_id,
                "scope": grant.scope,
                "workspace_id": grant.workspace_id,
                "user_id": grant.user_id,
                "agent_profile_id": grant.agent_profile_id,
                "channel": grant.channel,
                "action_selector": grant.action_selector,
                "tool_selector": grant.tool_selector,
                "approval_state": grant.approval_state,
                "expires_at": grant.expires_at,
                "metadata": metadata,
            }
        )
    return grants


def _failure_class_to_proto(failure_class: str):
    return getattr(
        hands_pb2,
        f"FAILURE_CLASS_{str(failure_class or FAILURE_CLASS_EXECUTION_ERROR).upper()}",
        hands_pb2.FAILURE_CLASS_EXECUTION_ERROR,
    )


def _artifact_entry_to_proto(entry: dict):
    return hands_pb2.ArtifactManifestEntry(
        artifact_id=str(entry.get("artifact_id") or ""),
        name=str(entry.get("name") or ""),
        artifact_type=str(entry.get("artifact_type") or ""),
        uri=str(entry.get("uri") or ""),
        mime_type=str(entry.get("mime_type") or ""),
        size_bytes=int(entry.get("size_bytes") or 0),
        checksum=str(entry.get("checksum") or ""),
        description=str(entry.get("description") or ""),
        metadata_json=json.dumps(entry.get("metadata") or {}),
    )


def _grant_to_proto(grant: dict):
    return hands_pb2.CapabilityGrant(
        grant_id=str(grant.get("grant_id") or ""),
        scope=str(grant.get("scope") or ""),
        workspace_id=str(grant.get("workspace_id") or ""),
        user_id=str(grant.get("user_id") or ""),
        agent_profile_id=str(grant.get("agent_profile_id") or ""),
        channel=str(grant.get("channel") or ""),
        action_selector=str(grant.get("action_selector") or ""),
        tool_selector=str(grant.get("tool_selector") or ""),
        approval_state=str(grant.get("approval_state") or ""),
        expires_at=str(grant.get("expires_at") or ""),
        metadata_json=json.dumps(grant.get("metadata") or {}),
    )


def _receipt_to_proto(receipt: dict):
    return hands_pb2.ActionReceipt(
        receipt_id=str(receipt.get("receipt_id") or ""),
        request_id=str(receipt.get("request_id") or ""),
        runtime_class=str(receipt.get("runtime_class") or ""),
        risk_class=str(receipt.get("risk_class") or ""),
        failure_class=_failure_class_to_proto(receipt.get("failure_class") or FAILURE_CLASS_EXECUTION_ERROR),
        logs_pointer=str(receipt.get("logs_pointer") or ""),
        stdout_pointer=str(receipt.get("stdout_pointer") or ""),
        stderr_pointer=str(receipt.get("stderr_pointer") or ""),
        sandbox_id=str(receipt.get("sandbox_id") or ""),
        exit_code=int(receipt.get("exit_code") or 0),
        audit_summary=str(receipt.get("audit_summary") or ""),
        artifact_manifest=[
            _artifact_entry_to_proto(entry) for entry in receipt.get("artifact_manifest", [])
        ],
        file_touches=[str(item) for item in receipt.get("file_touches", [])],
        network_touches=[str(item) for item in receipt.get("network_touches", [])],
        system_touches=[str(item) for item in receipt.get("system_touches", [])],
        grants=[_grant_to_proto(grant) for grant in receipt.get("grants", [])],
        metadata_json=json.dumps(receipt.get("metadata") or {}),
        mutating=bool(receipt.get("mutating")),
        finalized_at=str(receipt.get("finalized_at") or ""),
    )


def load_skills(skills_dir: str | None = None):
    """Load skill metadata from the skills directory."""
    resolved_skills_dir = skills_dir or os.getenv("SKILLS_DIR", "./skills") or "./skills"
    if not os.path.isdir(resolved_skills_dir):
        logger.warning(f"Skills directory not found: {resolved_skills_dir}")
        return []

    for name in os.listdir(resolved_skills_dir):
        skill_path = os.path.join(resolved_skills_dir, name)
        skill_manifest_path = os.path.join(skill_path, "skill.json")
        legacy_manifest_path = os.path.join(skill_path, "manifest.json")
        manifest_path = (
            skill_manifest_path
            if os.path.isfile(skill_manifest_path)
            else legacy_manifest_path
        )
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

    def __init__(
        self,
        executor: DockerExecutor,
        permissions: PermissionChecker,
        audit: AuditLogger,
    ):
        self.executor = executor
        self.permissions = permissions
        self.audit = audit

    async def ExecuteAction(self, request, context):
        """Execute an action in a sandboxed Docker container."""
        skill_name = request.action_name
        function_name = request.function_name
        arguments = request.arguments_json
        user_id = request.user_id
        workspace_id = request.workspace_id
        request_id = request.request_id or str(uuid.uuid4())
        grants = _request_grants(request)

        logger.info(f"ExecuteAction: {skill_name}.{function_name} "
                     f"user={user_id} workspace={workspace_id}")

        limits = {
            "timeout": getattr(request.limits, "timeout_seconds", 30) or 30,
            "memory_mb": getattr(request.limits, "memory_mb", 512) or 512,
            "cpu_limit": getattr(request.limits, "cpu_limit", 1.0) or 1.0,
            "network": getattr(request.limits, "network_enabled", False),
            "fs_read": getattr(request.limits, "file_system_read", False),
            "fs_write": getattr(request.limits, "file_system_write", False),
        }
        runtime_class, risk_class = _classify_execution_context(skill_name, limits)

        permission_decision = self.permissions.evaluate_action(
            workspace_id=workspace_id,
            action_name=skill_name,
            function_name=function_name,
            grants=grants,
            mutating=bool(request.mutating),
        )
        if not permission_decision["allowed"]:
            receipt = build_action_receipt(
                request_id=request_id,
                runtime_class=runtime_class,
                risk_class=risk_class,
                failure_class=permission_decision["failure_class"],
                logs_pointer=f"hands://denied/{request_id}",
                audit_summary=permission_decision["reason"],
                grants=permission_decision["matched_grants"],
                metadata={
                    "conversation_id": request.conversation_id,
                    "session_id": request.session_id,
                    "routing_context_json": request.routing_context_json,
                },
                mutating=bool(request.mutating),
            )
            denial_event = build_execution_action_event(
                source="hands.service",
                action_type=f"{skill_name}.{function_name}",
                status="denied",
                runtime_class=runtime_class,
                risk_class=risk_class,
                before_state={"command_hash": stable_hash(arguments), "policy_decision": "admission"},
                after_state={"command_hash": stable_hash(arguments), "policy_decision": "denied"},
                metadata={"request_id": request_id, "reason": permission_decision["reason"]},
            )
            yield hands_pb2.ActionExecutionEvent(
                status=hands_pb2.ACTION_DENIED,
                error=permission_decision["reason"],
                action_event_json=dumps_action_event(denial_event),
                receipt=_receipt_to_proto(receipt),
                failure_class=_failure_class_to_proto(permission_decision["failure_class"]),
                final=True,
                logs_pointer=receipt["logs_pointer"],
            )
            return

        # Find skill
        skill = _skills.get(skill_name)
        if not skill:
            receipt = build_action_receipt(
                request_id=request_id,
                runtime_class=runtime_class,
                risk_class=risk_class,
                failure_class=FAILURE_CLASS_EXECUTION_ERROR,
                logs_pointer=f"hands://errors/{request_id}",
                audit_summary=f"Unknown action: {skill_name}",
                grants=grants,
                metadata={"conversation_id": request.conversation_id},
                mutating=bool(request.mutating),
            )
            yield hands_pb2.ActionExecutionEvent(
                status=hands_pb2.ACTION_FAILED,
                error=f"Unknown action: {skill_name}",
                receipt=_receipt_to_proto(receipt),
                failure_class=hands_pb2.FAILURE_CLASS_EXECUTION_ERROR,
                final=True,
                logs_pointer=receipt["logs_pointer"],
            )
            return

        # Start audit
        exec_id = str(uuid.uuid4())
        self.audit.log_start(
            exec_id,
            user_id,
            workspace_id,
            skill_name,
            function_name,
            arguments,
            runtime_class=runtime_class,
            risk_class=risk_class,
            metadata={
                "conversation_id": request.conversation_id,
                "session_id": request.session_id,
                "request_id": request_id,
            },
        )

        # Yield RUNNING status
        running_event = build_execution_action_event(
            source="hands.service",
                action_type=f"{skill_name}.{function_name}",
                status="running",
                runtime_class=runtime_class,
                risk_class=risk_class,
                before_state={"command_hash": stable_hash(arguments), "policy_decision": "admitted"},
                after_state={"command_hash": stable_hash(arguments), "policy_decision": "running"},
                metadata={
                    "exec_id": exec_id,
                    "conversation_id": request.conversation_id,
                    "session_id": request.session_id,
                    "request_id": request_id,
                },
            )
        yield hands_pb2.ActionExecutionEvent(
            status=hands_pb2.ACTION_RUNNING,
            output=f"Executing {skill_name}.{function_name}...",
            action_event_json=dumps_action_event(running_event),
            failure_class=hands_pb2.FAILURE_CLASS_NONE,
            final=False,
            logs_pointer=f"hands://sandbox/{exec_id}/logs",
        )

        try:
            # Execute in Docker sandbox
            result = await self.executor.run(
                skill_path=skill["path"],
                function_name=function_name,
                arguments=arguments,
                limits=limits,
                allowed_domains=list(request.allowed_domains),
                allowed_paths=list(request.allowed_paths),
                workspace_id=workspace_id,
            )

            self.audit.log_complete(
                exec_id,
                status="success",
                execution_time_ms=result.get("execution_time_ms", 0),
                memory_used_mb=result.get("memory_used_mb", 0),
                audit_log=result.get("audit_log", {}),
                runtime_class=runtime_class,
                risk_class=risk_class,
                metadata={
                    "conversation_id": request.conversation_id,
                    "session_id": request.session_id,
                    "request_id": request_id,
                },
            )

            audit_data = result.get("audit_log", {}) or {}
            failure_class = (
                FAILURE_CLASS_PARTIAL_OUTPUT if result.get("partial_output") else FAILURE_CLASS_NONE
            )
            receipt = build_action_receipt(
                request_id=request_id,
                runtime_class=runtime_class,
                risk_class=risk_class,
                failure_class=failure_class,
                logs_pointer=str(result.get("logs_pointer") or ""),
                stdout_pointer=str(result.get("stdout_pointer") or ""),
                stderr_pointer=str(result.get("stderr_pointer") or ""),
                sandbox_id=str(audit_data.get("sandbox_id") or ""),
                exit_code=int(result.get("exit_code") or 0),
                audit_summary=(
                    f"Executed {skill_name}.{function_name} in sandbox {audit_data.get('sandbox_id', '')}"
                ),
                artifact_manifest=result.get("artifact_manifest", []),
                file_touches=audit_data.get("file_accesses", []),
                network_touches=audit_data.get("network_requests", []),
                system_touches=audit_data.get("system_calls", []),
                grants=permission_decision["matched_grants"] or grants,
                metadata={
                    "conversation_id": request.conversation_id,
                    "session_id": request.session_id,
                    "execution_time_ms": result.get("execution_time_ms", 0),
                    "memory_used_mb": result.get("memory_used_mb", 0),
                },
                mutating=bool(request.mutating),
            )
            success_event = build_execution_action_event(
                source="hands.service",
                action_type=f"{skill_name}.{function_name}",
                status="partial" if result.get("partial_output") else "success",
                runtime_class=runtime_class,
                risk_class=risk_class,
                before_state={"command_hash": stable_hash(arguments), "policy_decision": "running"},
                after_state={
                    "command_hash": stable_hash(arguments),
                    "policy_decision": "partial" if result.get("partial_output") else "success",
                },
                metadata={
                    "exec_id": exec_id,
                    "execution_time_ms": result.get("execution_time_ms", 0),
                    "memory_used_mb": result.get("memory_used_mb", 0),
                    "conversation_id": request.conversation_id,
                    "request_id": request_id,
                    "receipt_id": receipt["receipt_id"],
                },
            )
            yield hands_pb2.ActionExecutionEvent(
                status=hands_pb2.ACTION_PARTIAL if result.get("partial_output") else hands_pb2.ACTION_COMPLETED,
                output=result.get("output", ""),
                execution_time_ms=result.get("execution_time_ms", 0),
                memory_used_mb=result.get("memory_used_mb", 0),
                action_event_json=dumps_action_event(success_event),
                receipt=_receipt_to_proto(receipt),
                failure_class=_failure_class_to_proto(failure_class),
                final=True,
                logs_pointer=receipt["logs_pointer"],
            )

        except asyncio.TimeoutError:
            self.audit.log_complete(
                exec_id,
                status="timeout",
                runtime_class=runtime_class,
                risk_class=risk_class,
                metadata={
                    "conversation_id": request.conversation_id,
                    "session_id": request.session_id,
                    "request_id": request_id,
                },
            )
            receipt = build_action_receipt(
                request_id=request_id,
                runtime_class=runtime_class,
                risk_class=risk_class,
                failure_class=FAILURE_CLASS_TIMEOUT,
                logs_pointer=f"hands://sandbox/{exec_id}/logs",
                sandbox_id=exec_id,
                exit_code=124,
                audit_summary=f"Execution timed out after {limits['timeout']}s",
                grants=permission_decision["matched_grants"] or grants,
                metadata={"conversation_id": request.conversation_id, "session_id": request.session_id},
                mutating=bool(request.mutating),
            )
            timeout_event = build_execution_action_event(
                source="hands.service",
                action_type=f"{skill_name}.{function_name}",
                status="timeout",
                runtime_class=runtime_class,
                risk_class=risk_class,
                before_state={"command_hash": stable_hash(arguments), "policy_decision": "running"},
                after_state={"command_hash": stable_hash(arguments), "policy_decision": "timeout"},
                metadata={
                    "exec_id": exec_id,
                    "conversation_id": request.conversation_id,
                    "request_id": request_id,
                    "receipt_id": receipt["receipt_id"],
                },
            )
            yield hands_pb2.ActionExecutionEvent(
                status=hands_pb2.ACTION_TIMEOUT,
                error=f"Action execution timed out after {limits['timeout']}s",
                action_event_json=dumps_action_event(timeout_event),
                receipt=_receipt_to_proto(receipt),
                failure_class=hands_pb2.FAILURE_CLASS_TIMEOUT,
                final=True,
                logs_pointer=receipt["logs_pointer"],
            )

        except Exception as e:
            failure_class = FAILURE_CLASS_SANDBOX_CRASH
            self.audit.log_complete(
                exec_id,
                status="error",
                error=str(e),
                runtime_class=runtime_class,
                risk_class=risk_class,
                metadata={
                    "conversation_id": request.conversation_id,
                    "session_id": request.session_id,
                    "request_id": request_id,
                },
            )
            receipt = build_action_receipt(
                request_id=request_id,
                runtime_class=runtime_class,
                risk_class=risk_class,
                failure_class=failure_class,
                logs_pointer=f"hands://sandbox/{exec_id}/logs",
                sandbox_id=exec_id,
                exit_code=1,
                audit_summary=str(e),
                grants=permission_decision["matched_grants"] or grants,
                metadata={"conversation_id": request.conversation_id, "session_id": request.session_id},
                mutating=bool(request.mutating),
            )
            error_event = build_execution_action_event(
                source="hands.service",
                action_type=f"{skill_name}.{function_name}",
                status="error",
                runtime_class=runtime_class,
                risk_class=risk_class,
                before_state={"command_hash": stable_hash(arguments), "policy_decision": "running"},
                after_state={"command_hash": stable_hash(arguments), "policy_decision": "error"},
                metadata={
                    "exec_id": exec_id,
                    "error": str(e),
                    "conversation_id": request.conversation_id,
                    "request_id": request_id,
                    "receipt_id": receipt["receipt_id"],
                },
            )
            yield hands_pb2.ActionExecutionEvent(
                status=hands_pb2.ACTION_FAILED,
                error=str(e),
                action_event_json=dumps_action_event(error_event),
                receipt=_receipt_to_proto(receipt),
                failure_class=_failure_class_to_proto(failure_class),
                final=True,
                logs_pointer=receipt["logs_pointer"],
            )

    async def ListSkills(self, request, context):
        """List available skills for a workspace."""
        workspace_id = request.workspace_id
        skills = []

        for name, skill in _skills.items():
            if not self.permissions.check(workspace_id, name):
                continue

            manifest = skill["manifest"]
            function_specs = manifest.get("functions")
            if function_specs is None and "tools" in manifest:
                function_specs = [{"name": tool} for tool in manifest.get("tools", [])]

            functions = []
            for fn in function_specs or []:
                functions.append(
                    hands_pb2.FunctionMetadata(
                        name=fn["name"],
                        description=fn.get("description", ""),
                        parameters_schema=json.dumps(fn.get("parameters", {})),
                    )
                )

            capabilities = manifest.get("capabilities", {})
            requires_network = manifest.get(
                "requires_network",
                capabilities.get("network", False),
            )
            requires_filesystem = manifest.get(
                "requires_filesystem",
                capabilities.get("filesystem", False),
            )

            skills.append(
                hands_pb2.SkillMetadata(
                    name=name,
                    description=manifest.get("description", ""),
                    version=manifest.get("version", "0.1.0"),
                    functions=functions,
                    requires_network=requires_network,
                    requires_filesystem=requires_filesystem,
                )
            )

        return hands_pb2.ListSkillsResponse(skills=skills)

    async def HealthCheck(self, request, context):
        """Return health status."""
        active = self.executor.active_sandboxes
        capacity = self.executor.max_concurrent - active
        return hands_pb2.HealthCheckResponse(
            healthy=self.executor.sandbox_ready,
            version="0.1.0",
            active_sandboxes=active,
            available_capacity=capacity,
        )


# ── Server Bootstrap ─────────────────────────────────────────────────

async def serve():
    # Initialize components
    executor = DockerExecutor()
    try:
        await executor.ensure_sandbox_image()
    except Exception as e:
        logger.warning(f"Sandbox image bootstrap failed (service will stay degraded): {e}")
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
        logger.info("Shutting down Hands service (keyboard interrupt)...")
    finally:
        logger.info("Cleaning up sandbox containers...")
        await executor.cleanup()
        await server.stop(5)


if __name__ == "__main__":
    asyncio.run(serve())
