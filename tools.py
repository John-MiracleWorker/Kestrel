"""
Libre Bird Tool System — Compatibility Shim
Delegates to skill_loader for all tool discovery and execution.
Maintains the same public interface:
    TOOL_DEFINITIONS  — list of OpenAI function-calling dicts
    execute_tool()    — run a tool by name + args → JSON string
"""

import logging

logger = logging.getLogger("libre_bird.tools")

# Import everything from skill_loader so llm_engine.py and server.py
# can keep using `from tools import TOOL_DEFINITIONS, execute_tool`
from skill_loader import TOOL_DEFINITIONS, execute_tool  # noqa: F401

logger.info(f"Tools shim loaded — {len(TOOL_DEFINITIONS)} tools via skill_loader")
