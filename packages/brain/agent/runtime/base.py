"""Runtime abstraction for agent execution environments."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol


class RuntimeMode(str, Enum):
    """Supported runtime profiles."""

    DOCKER = "docker"
    NATIVE = "native"
    HYBRID = "hybrid"


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Capability flags exposed to tools and policy checks."""

    mode: RuntimeMode
    supports_docker_execution: bool = False
    supports_native_execution: bool = False
    supports_computer_use: bool = False
    supports_host_shell: bool = False
    supports_host_python: bool = False
    supports_code_languages: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """Serialize capabilities for tool responses and logging."""
        return {
            "mode": self.mode.value,
            "supports_docker_execution": self.supports_docker_execution,
            "supports_native_execution": self.supports_native_execution,
            "supports_computer_use": self.supports_computer_use,
            "supports_host_shell": self.supports_host_shell,
            "supports_host_python": self.supports_host_python,
            "supports_code_languages": list(self.supports_code_languages),
        }


class ExecutionRuntime(Protocol):
    """Common execute() contract for runtime backends."""

    @property
    def capabilities(self) -> RuntimeCapabilities:
        ...

    async def execute(
        self,
        *,
        tool_name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        ...


class RuntimeErrorResult(Exception):
    """Raised for policy/runtime resolution errors."""

    pass
