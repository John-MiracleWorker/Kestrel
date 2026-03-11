from __future__ import annotations

from typing import Any

from agent.runtime.state import KestrelState


async def policy_node(
    state: KestrelState,
    *,
    kernel_policy_service=None,
    subsystem_bootstrapper=None,
) -> dict[str, Any]:
    task = state["task"]
    execution_context = getattr(task, "execution_context", None)
    subsystem_health = subsystem_bootstrapper.snapshot() if subsystem_bootstrapper else {}

    if kernel_policy_service is None:
        return {
            "kernel_policy": {},
            "subsystem_health": subsystem_health,
        }

    policy = kernel_policy_service.evaluate(
        task=task,
        execution_context=execution_context,
        subsystem_health=subsystem_health,
        persona_context=state.get("persona_context", ""),
    )
    return {
        "kernel_policy": policy.to_dict(),
        "subsystem_health": subsystem_health,
    }
