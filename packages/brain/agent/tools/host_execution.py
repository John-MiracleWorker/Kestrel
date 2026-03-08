"""
Host Execution Tools — Native shell/python execution on the host macOS.
WARNING: These tools bypass the container isolation and execute directly
on the host machine. Regulated by an allowlist and native macOS approval dialogs.
"""

import os
import asyncio
import logging
from datetime import datetime
from agent.runtime import get_active_runtime
from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.host_execution")

def _audit_log(command: str, language: str, approved: bool, error: str = ""):
    """Append execution attempts to the local audit log."""
    audit_dir = os.path.expanduser("~/.kestrel/audit")
    os.makedirs(audit_dir, exist_ok=True)
    log_file = os.path.join(audit_dir, "execution.log")
    
    timestamp = datetime.now().isoformat()
    status = "APPROVED" if approved else "DENIED"
    if error:
        status = f"FAILED ({error})"
        
    log_entry = f"[{timestamp}] [{language}] [{status}] {command}\n"
    try:
        with open(log_file, "a") as f:
            f.write(log_entry)
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")

async def _prompt_mac_approval(command: str) -> bool:
    """Prompt the user for approval using macOS native dialog via osascript."""
    safe_command = command.replace('"', '\\"')
    if len(safe_command) > 150:
         safe_command = safe_command[:147] + "..."
         
    script = f'''
    try
        set dialogResult to display dialog "Kestrel Agent OS is requesting to natively execute:\\n\\n{safe_command}" buttons {{"Deny", "Approve"}} default button "Deny" with icon caution with title "Security Authorization"
        if button returned of dialogResult is "Approve" then
            return "true"
        else
            return "false"
        end if
    on error number -128
        return "false"
    end try
    '''
    
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        result = stdout.decode().strip()
        return result == "true"
    except Exception as e:
        logger.error(f"macOS approval dialog failed: {e}")
        return False

def _check_allowlist(command: str) -> bool:
    """Check if a command matches the allowlist in ~/.kestrel/allowlist.yml"""
    allowlist_path = os.path.expanduser("~/.kestrel/allowlist.yml")
    if not os.path.exists(allowlist_path):
        return False
        
    try:
        import yaml
        import re
        with open(allowlist_path, "r") as f:
            config = yaml.safe_load(f) or {}
            allowed_patterns = config.get("allowed_commands", [])
            for pattern in allowed_patterns:
                try:
                    if re.search(pattern, command):
                        return True
                except re.error as re_err:
                    logger.warning(f"Invalid allowlist regex pattern '{pattern}': {re_err}")
    except Exception as e:
        logger.warning(f"Error reading allowlist: {e}")
        
    return False

async def execute_host_shell(command: str) -> dict:
    """Execute a shell command directly on the host OS."""
    active_runtime = get_active_runtime()
    if not active_runtime:
        return {"success": False, "error": "Runtime policy is not initialized.", "output": ""}

    capabilities = active_runtime.capabilities
    if not capabilities.supports_host_shell:
        return {
            "success": False,
            "error": f"host_shell is disabled in runtime mode '{capabilities.mode.value}'.",
            "output": "",
            "capabilities": capabilities.as_dict(),
        }
    
    # 1. Check Allowlist
    is_allowed = _check_allowlist(command)
    
    # 2. If not in allowlist, prompt for local approval
    if not is_allowed:
        approved = await _prompt_mac_approval(command)
        if not approved:
            _audit_log(command, "shell", approved=False)
            return {
                "success": False,
                "error": "User denied execution of high-risk command via native macOS dialog.",
                "output": ""
            }
            
    # 3. Execute and audit
    _audit_log(command, "shell", approved=True)
    try:
        result = await active_runtime.execute(tool_name="host_shell", payload={"command": command})
        result["capabilities"] = capabilities.as_dict()
        return result
    except Exception as e:
        _audit_log(command, "shell", approved=True, error=str(e))
        return {
            "success": False,
            "error": str(e),
            "output": ""
        }

async def execute_host_python(code: str) -> dict:
    """Execute python code directly on the host OS."""
    active_runtime = get_active_runtime()
    if not active_runtime:
        return {"success": False, "error": "Runtime policy is not initialized.", "output": ""}

    capabilities = active_runtime.capabilities
    if not capabilities.supports_host_python:
        return {
            "success": False,
            "error": f"host_python is disabled in runtime mode '{capabilities.mode.value}'.",
            "output": "",
            "capabilities": capabilities.as_dict(),
        }
    
    # We serialize the code to a temporary file and run `python3 cache_file.py`
    # Always prompt for native approval for host python unless we build a complex analysis
    # For MVP, prompt approval for all raw python code
    
    approved = await _prompt_mac_approval("Python script with length: " + str(len(code)))
    if not approved:
        _audit_log("Python Script", "python", approved=False)
        return {
            "success": False,
            "error": "User denied execution of high-risk python code via native macOS dialog.",
            "output": ""
        }

    _audit_log("Python Script", "python", approved=True)
    try:
        result = await active_runtime.execute(tool_name="host_python", payload={"code": code})
        result["capabilities"] = capabilities.as_dict()
        return result
    except Exception as e:
        _audit_log("Python Script", "python", approved=True, error=str(e))
        return {
            "success": False,
            "error": str(e),
            "output": ""
        }

def register_host_execution_tools(registry) -> None:
    registry.register(
        definition=ToolDefinition(
            name="host_shell",
            description=(
                "Execute a shell command DIRECTLY on the host macOS. "
                "Use this for tasks that require native OS access. "
                "WARNING: High risk. Commands not in the allowlist will trigger a native popup dialog."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute"
                    }
                },
                "required": ["command"]
            },
            risk_level=RiskLevel.HIGH,
            category="control"
        ),
        handler=execute_host_shell
    )
    
    registry.register(
        definition=ToolDefinition(
            name="host_python",
            description=(
                "Execute Python code DIRECTLY on the host macOS. "
                "Allows the agent to use host resources, credentials, and modules. "
                "WARNING: High risk. Will trigger a native popup dialog for authorization."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The python code to execute"
                    }
                },
                "required": ["code"]
            },
            risk_level=RiskLevel.HIGH,
            category="control"
        ),
        handler=execute_host_python
    )
