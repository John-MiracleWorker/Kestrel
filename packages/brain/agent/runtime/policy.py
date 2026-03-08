"""Runtime policy selection and tool routing."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from agent.runtime.base import ExecutionRuntime, RuntimeCapabilities, RuntimeMode
from agent.runtime.docker_runtime import DockerRuntime
from agent.runtime.native_runtime import NativeRuntime

logger = logging.getLogger("brain.agent.runtime.policy")


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

    async def execute(self, *, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if tool_name in {"host_shell", "host_python", "computer_use"}:
            return await self.native.execute(tool_name=tool_name, payload=payload)

        if tool_name == "code_execute":
            language = payload.get("language")
            if self.docker.capabilities.supports_docker_execution:
                return await self.docker.execute(tool_name=tool_name, payload=payload)
            if language in {"python", "javascript", "shell"}:
                return await self.native.execute(tool_name=tool_name, payload=payload)

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
    if mode == RuntimeMode.NATIVE:
        runtime = NativeRuntime()
    elif mode == RuntimeMode.HYBRID:
        runtime = HybridRuntime(docker=DockerRuntime(hands_client=hands_client), native=NativeRuntime())
    else:
        runtime = DockerRuntime(hands_client=hands_client)

    logger.info("Active runtime mode=%s capabilities=%s", mode.value, runtime.capabilities.as_dict())
    return runtime
