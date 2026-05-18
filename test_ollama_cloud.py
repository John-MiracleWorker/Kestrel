import sys
sys.path.insert(0, "src")

import os
os.environ.setdefault("OLLAMA_API_KEY", os.getenv("OLLAMA_API_KEY", ""))

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.factory import build_llm_provider
from nested_memvid_agent.runtime_models import ChatMessage

config = AgentConfig(
    provider="ollama-cloud",
    model="gemma3:27b",
    backend="memory",
)

llm = build_llm_provider(config)
response = llm.generate(
    messages=[ChatMessage(role="user", content="Say hello in one word.")],
    tools=[],
)
print("Response:", response.content)
print("Usage:", response.usage)
