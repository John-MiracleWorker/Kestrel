"""
Agent runtime — typed container for service dependencies.

Replaces bare module-level globals with a typed dataclass so that:
  1. Missing dependencies are caught at attribute access (AttributeError)
     instead of silently returning None.
  2. Dependency injection is explicit — tests can construct a Runtime
     with only the services they need.
  3. Type checkers can validate usage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("brain.runtime")


@dataclass
class Runtime:
    """Typed container for all runtime service dependencies."""

    retrieval: Any = None
    embedding_pipeline: Any = None
    vector_store: Any = None

    agent_loop: Any = None
    agent_persistence: Any = None
    running_tasks: Dict[str, Any] = field(default_factory=dict)

    hands_client: Any = None
    cron_scheduler: Any = None
    webhook_handler: Any = None
    memory_graph: Any = None
    tool_registry: Any = None
    persona_learner: Any = None
    task_predictor: Any = None
    command_parser: Any = None
    metrics_collector: Any = None
    notification_router: Any = None
    workflow_registry: Any = None
    skill_manager: Any = None
    session_manager: Any = None
    sandbox_manager: Any = None

    def validate_required(self) -> list[str]:
        """Return names of critical services that are still None."""
        required = [
            "agent_loop",
            "agent_persistence",
            "tool_registry",
            "vector_store",
        ]
        return [name for name in required if getattr(self, name) is None]


# Module-level singleton — used by services that import `from core.runtime import *`
# During the transition period, attribute access on the module is proxied to this
# instance so that existing `runtime.foo` imports continue to work.
_instance = Runtime()


def __getattr__(name: str) -> Any:
    """Proxy module-level attribute access to the Runtime instance."""
    if name.startswith("_"):
        raise AttributeError(name)
    return getattr(_instance, name)


def __setattr__(name: str, value: Any) -> None:
    """Proxy module-level attribute writes to the Runtime instance."""
    setattr(_instance, name, value)


def get_runtime() -> Runtime:
    """Return the global Runtime instance (for explicit DI)."""
    return _instance
