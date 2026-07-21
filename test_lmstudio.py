import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent  # noqa: E402
from nested_memvid_agent.backends.in_memory import InMemoryBackend  # noqa: E402
from nested_memvid_agent.config import AgentConfig  # noqa: E402
from nested_memvid_agent.event_log import JsonlEventLog  # noqa: E402
from nested_memvid_agent.layers import LayeredMemorySystem  # noqa: E402
from nested_memvid_agent.llm.factory import build_llm_provider  # noqa: E402
from nested_memvid_agent.tools.builtin import build_default_tools  # noqa: E402

with tempfile.TemporaryDirectory() as tmpdir:
    workspace = Path(tmpdir) / "workspace"
    workspace.mkdir()
    memory_dir = Path(tmpdir) / "memory"
    memory_dir.mkdir()

    config = AgentConfig(
        provider="openai-compatible",
        model="google/gemma-4-26b-a4b",
        base_url="http://127.0.0.1:1234/v1",
        backend="memory",
        memory_dir=memory_dir,
        workspace=workspace,
        log_dir=Path(tmpdir) / "logs",
        allow_web=False,
        allow_shell=False,
        max_tool_rounds=3,
    )

    memory = LayeredMemorySystem.from_backend_factory(memory_dir, InMemoryBackend)
    tools = build_default_tools()
    event_log = JsonlEventLog(config.log_dir / "events.jsonl")
    llm = build_llm_provider(config)

    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=tools,
            config=config,
            event_log=event_log,
        )
    )

    # Create a file and ask the agent to read it
    (workspace / "hello.txt").write_text("The secret code is 42.")
    result = agent.chat(f"Read {workspace / 'hello.txt'} and tell me the secret code.")
    print("Response:", result.assistant_message)
    print("Tool calls:", [te.call.name for te in result.tool_executions])
    print("Success:", "42" in result.assistant_message)
