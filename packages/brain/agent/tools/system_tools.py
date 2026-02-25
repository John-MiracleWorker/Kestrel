import logging
from typing import Dict, Any
from agent.types import ToolDefinition, RiskLevel
from agent.tools.system_health import get_system_health
from agent.tools.process_manager import list_processes, kill_process

logger = logging.getLogger("brain.agent.tools.system")

def register_system_tools(registry):
    # System Health
    registry.register(
        ToolDefinition(
            name="system_health",
            description="Check the health of the host system (CPU, Memory, Disk, and core service ports).",
            parameters={
                "type": "object",
                "properties": {},
            },
            risk_level=RiskLevel.LOW,
        ),
        handler=get_system_health_handler,
    )

    # Process Management
    registry.register(
        ToolDefinition(
            name="process_list",
            description="List the top 20 resource-intensive processes running on the host.",
            parameters={
                "type": "object",
                "properties": {
                    "filter_name": {"type": "string", "description": "Optional name to filter processes by."}
                },
            },
            risk_level=RiskLevel.LOW,
        ),
        handler=list_processes_handler,
    )

    registry.register(
        ToolDefinition(
            name="process_kill",
            description="Terminate a process by its PID.",
            parameters={
                "type": "object",
                "properties": {
                    "pid": {"type": "integer", "description": "The process ID to terminate."}
                },
                "required": ["pid"],
            },
            risk_level=RiskLevel.HIGH,
        ),
        handler=kill_process_handler,
    )

async def get_system_health_handler(**kwargs):
    return get_system_health()

async def list_processes_handler(filter_name: str = None, **kwargs):
    return list_processes(filter_name=filter_name)

async def kill_process_handler(pid: int, **kwargs):
    return kill_process(pid=pid)
