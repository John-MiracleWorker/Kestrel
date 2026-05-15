from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.models import MemoryKind, MemoryLayer
from nested_memvid_agent.orchestrator import (
    NestedMemoryAgentRuntime,
    build_memory_system,
    default_event_log,
)


def main() -> None:
    memory_dir = Path("./memory")
    memory = build_memory_system("memory", memory_dir)
    runtime = NestedMemoryAgentRuntime(memory=memory, event_log=default_event_log(memory_dir))

    runtime.observe(
        "The latest auth failure was caused by editing global env vars instead of provider-specific agent auth profiles.",
        title="Auth failure cause",
        layer=MemoryLayer.EPISODIC,
        kind=MemoryKind.FAILURE,
        confidence=0.72,
    )

    prompt = runtime.compile_context("Fix provider-specific auth startup failures without over-editing global shell config.")
    print(prompt)

    memory.seal_all()
    memory.close_all()


if __name__ == "__main__":
    main()
