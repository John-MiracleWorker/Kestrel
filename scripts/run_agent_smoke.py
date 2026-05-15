from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.app_factory import build_agent
from nested_memvid_agent.config import AgentConfig


def main() -> None:
    config = AgentConfig(provider="mock", model="mock", backend="memory", memory_dir=Path("./tmp-smoke/memory"), log_dir=Path("./tmp-smoke/logs"))
    agent = build_agent(config)
    try:
        first = agent.chat("Remember that the project codename is Kestrel.", session_id="smoke")
        second = agent.chat("/search Kestrel", session_id="smoke")
        print({
            "first": first.assistant_message,
            "second": second.assistant_message,
            "tool_count": len(second.tool_executions),
            "memory_writes": len(first.memory_writes) + len(second.memory_writes),
        })
    finally:
        agent.close()


if __name__ == "__main__":
    main()
