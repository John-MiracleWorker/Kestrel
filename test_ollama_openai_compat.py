import sys
sys.path.insert(0, "src")

import os
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.factory import build_llm_provider
from nested_memvid_agent.runtime_models import ChatMessage
from nested_memvid_agent.tools.builtin import build_default_tools

config = AgentConfig(
    provider="openai-compatible",
    model="kimi-k2.5",
    base_url="https://ollama.com/v1",
    api_key_env="OLLAMA_API_KEY",
    backend="memory",
)

tools = build_default_tools()
file_read_spec = next((s for s in tools.specs() if s.name == "file.read"), None)
llm = build_llm_provider(config)
response = llm.generate(
    messages=[ChatMessage(role="user", content="Read the file at /tmp/hello.txt and tell me its contents.")],
    tools=[file_read_spec] if file_read_spec else [],
)
print("Content:", response.content)
print("Tool calls:", [(tc.name, tc.arguments) for tc in response.tool_calls])
print("Usage:", response.usage)
