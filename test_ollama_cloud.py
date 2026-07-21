import os
import sys

sys.path.insert(0, "src")

from nested_memvid_agent.config import AgentConfig  # noqa: E402
from nested_memvid_agent.llm.factory import build_llm_provider  # noqa: E402
from nested_memvid_agent.runtime_models import ChatMessage  # noqa: E402

os.environ.setdefault("OLLAMA_API_KEY", os.getenv("OLLAMA_API_KEY", ""))

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
