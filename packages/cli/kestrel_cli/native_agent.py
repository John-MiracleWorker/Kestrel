from __future__ import annotations

from . import native_agent_run as _native_agent_run

globals().update({name: value for name, value in vars(_native_agent_run).items() if not name.startswith("__")})

class NativeAgentRunner(
    NativeAgentRunnerRunMixin,
    NativeAgentRunnerBase,
):
    pass
