"""Runtime policy selection and tool routing."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

from agent.runtime.base import ExecutionRuntime, RuntimeCapabilities, RuntimeMode
from agent.runtime.docker_runtime import DockerRuntime
from agent.runtime.native_runtime import NativeRuntime

logger = logging.getLogger("brain.agent.runtime.policy")


def _should_fallback_to_native(*, language: Any, docker_result: dict[str, Any]) -> bool:
    """Return True when hybrid mode should retry code execution on native runtime.

    This is intentionally narrow: only fallback for supported languages when the
    Docker execution path failed due to sandbox/container availability issues.
    """
    if language not in {"python", "javascript", "shell"}:
        return False

    if docker_result.get("success") is True:
        return False

    error_text = str(docker_result.get("error", "")).lower()
    if not error_text:
        return False

    fallback_signals = (
        "pull access denied",
        "image",
        "not found",
        "hands service is not connected",
        "no such container",
        "manifest unknown",
    )
    return any(signal in error_text for signal in fallback_signals)


@dataclass
class HybridRuntime:
    """Hybrid runtime that can route requests to docker or native backends."""

    docker: DockerRuntime
    native: NativeRuntime

    @property
    def capabilities(self) -> RuntimeCapabilities:
        dc = self.docker.capabilities
        nc = self.native.capabilities
        return RuntimeCapabilities(
            mode=RuntimeMode.HYBRID,
            supports_docker_execution=dc.supports_docker_execution,
            supports_native_execution=nc.supports_native_execution,
            supports_computer_use=nc.supports_computer_use,
            supports_host_shell=nc.supports_host_shell,
            supports_host_python=nc.supports_host_python,
            supports_code_languages=tuple(sorted(set(dc.supports_code_languages + nc.supports_code_languages))),
        )

    async def execute(self, *, tool_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if tool_name in {"host_shell", "host_python", "computer_use"}:
            return await self.native.execute(tool_name=tool_name, payload=payload)

        if tool_name == "code_execute":
            language = payload.get("language")
            if self.docker.capabilities.supports_docker_execution:
                docker_result = await self.docker.execute(tool_name=tool_name, payload=payload)
                if _should_fallback_to_native(language=language, docker_result=docker_result):
                    logger.warning(
                        "Docker code execution failed with sandbox/runtime error; "
                        "falling back to native execution for language=%s",
                        language,
                    )
                    native_payload = dict(payload)
                    native_payload["_fallback_from"] = docker_result.get(
                        "runtime_class",
                        "sandboxed_docker",
                    )
                    native_payload["_fallback_reason"] = docker_result.get("error", "")
                    native_result = await self.native.execute(
                        tool_name=tool_name,
                        payload=native_payload,
                    )
                    native_result.setdefault("warnings", [])
                    native_result["warnings"].append(
                        "Docker runtime unavailable; executed via native runtime fallback."
                    )
                    native_result.setdefault("docker_error", docker_result.get("error", ""))
                    native_result.setdefault("fallback_used", True)
                    native_result.setdefault(
                        "fallback_from",
                        docker_result.get("runtime_class", "sandboxed_docker"),
                    )
                    native_result.setdefault(
                        "fallback_to",
                        native_result.get("runtime_class", "hybrid_native_fallback"),
                    )
                    if docker_result.get("action_events"):
                        native_result.setdefault(
                            "attempted_action_events",
                            docker_result.get("action_events", []),
                        )
                    return native_result
                return docker_result
            if language in {"python", "javascript", "shell"}:
                native_payload = dict(payload)
                native_payload["_fallback_from"] = "sandboxed_docker"
                native_payload["_fallback_reason"] = (
                    "Hybrid runtime selected native execution because Docker sandbox execution "
                    "is not available."
                )
                native_result = await self.native.execute(
                    tool_name=tool_name,
                    payload=native_payload,
                )
                native_result.setdefault("warnings", [])
                native_result["warnings"].append(
                    "Docker sandbox unavailable in hybrid mode; executed via native runtime."
                )
                native_result.setdefault("fallback_used", True)
                native_result.setdefault("fallback_from", "sandboxed_docker")
                native_result.setdefault(
                    "fallback_to",
                    native_result.get("runtime_class", "hybrid_native_fallback"),
                )
                return native_result

        return await self.docker.execute(tool_name=tool_name, payload=payload)


def _parse_mode(raw_mode: str) -> RuntimeMode:
    normalized = (raw_mode or RuntimeMode.DOCKER.value).strip().lower()
    if normalized == RuntimeMode.NATIVE.value:
        return RuntimeMode.NATIVE
    if normalized == RuntimeMode.HYBRID.value:
        return RuntimeMode.HYBRID
    return RuntimeMode.DOCKER


def build_runtime_policy(*, hands_client: Any = None) -> ExecutionRuntime:
    """Build active runtime policy based on KESTREL_RUNTIME_MODE."""

    mode = _parse_mode(os.getenv("KESTREL_RUNTIME_MODE", RuntimeMode.DOCKER.value))
    runtime: ExecutionRuntime
    if mode == RuntimeMode.NATIVE:
        runtime = NativeRuntime()
    elif mode == RuntimeMode.HYBRID:
        runtime = HybridRuntime(docker=DockerRuntime(hands_client=hands_client), native=NativeRuntime())
    else:
        runtime = DockerRuntime(hands_client=hands_client)

    logger.info("Active runtime mode=%s capabilities=%s", mode.value, runtime.capabilities.as_dict())
    return runtime
