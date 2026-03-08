"""
Host Execution Tools — Native shell/python execution on the host macOS.
WARNING: These tools bypass the container isolation and execute directly
on the host machine. Regulated by an allowlist and native macOS approval dialogs.
"""

import os
import sys
import asyncio
import logging
import tempfile
import json
from pathlib import Path
from datetime import datetime
from agent.types import RiskLevel, ToolDefinition

_SHARED_PATH = Path(__file__).resolve().parents[3] / "shared"
if str(_SHARED_PATH) not in sys.path:
    sys.path.append(str(_SHARED_PATH))

from action_event_schema import build_action_event, stable_hash

logger = logging.getLogger("brain.agent.tools.host_execution")

def _audit_log(command: str, language: str, approved: bool, error: str = "", action_event: dict | None = None):
    """Append execution attempts to the local audit log."""
    audit_dir = os.path.expanduser("~/.kestrel/audit")
    os.makedirs(audit_dir, exist_ok=True)
    log_file = os.path.join(audit_dir, "execution.log")
    
    timestamp = datetime.now().isoformat()
    status = "APPROVED" if approved else "DENIED"
    if error:
        status = f"FAILED ({error})"
        
    log_entry = {
        "timestamp": timestamp,
        "language": language,
        "status": status,
        "command": command,
        "action_event": action_event or {},
    }
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
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
    command_hash = stable_hash(command)
    
    # 1. Check Allowlist
    is_allowed = _check_allowlist(command)
    
    # 2. If not in allowlist, prompt for local approval
    if not is_allowed:
        approved = await _prompt_mac_approval(command)
        if not approved:
            action_event = build_action_event(
                source="brain.host_execution",
                action_type="host_shell",
                status="denied",
                before_state={"command_hash": command_hash, "policy_decision": "approval_required"},
                after_state={"command_hash": command_hash, "policy_decision": "denied"},
            )
            _audit_log(command, "shell", approved=False, action_event=action_event)
            return {
                "success": False,
                "error": "User denied execution of high-risk command via native macOS dialog.",
                "output": "",
                "action_event": action_event,
            }
            
    # 3. Execute and audit
    action_event = build_action_event(
        source="brain.host_execution",
        action_type="host_shell",
        status="running",
        before_state={
            "command_hash": command_hash,
            "policy_decision": "allowlist" if is_allowed else "user_approved",
        },
        after_state={"command_hash": command_hash, "policy_decision": "running"},
    )
    _audit_log(command, "shell", approved=True, action_event=action_event)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        final_event = build_action_event(
            source="brain.host_execution",
            action_type="host_shell",
            status="success" if proc.returncode == 0 else "failed",
            before_state=action_event["before_state"],
            after_state={
                "command_hash": command_hash,
                "policy_decision": "executed",
            },
            metadata={"exit_code": proc.returncode},
        )
        return {
            "success": proc.returncode == 0,
            "output": stdout.decode(),
            "error": stderr.decode(),
            "exit_code": proc.returncode,
            "action_event": final_event,
        }
    except Exception as e:
        failed_event = build_action_event(
            source="brain.host_execution",
            action_type="host_shell",
            status="failed",
            before_state=action_event["before_state"],
            after_state={"command_hash": command_hash, "policy_decision": "execution_error"},
            metadata={"error": str(e)},
        )
        _audit_log(command, "shell", approved=True, error=str(e), action_event=failed_event)
        return {
            "success": False,
            "error": str(e),
            "output": "",
            "action_event": failed_event,
        }

async def execute_host_python(code: str) -> dict:
    """Execute python code directly on the host OS."""
    
    # We serialize the code to a temporary file and run `python3 cache_file.py`
    # Always prompt for native approval for host python unless we build a complex analysis
    # For MVP, prompt approval for all raw python code
    
    command_hash = stable_hash(code)
    approved = await _prompt_mac_approval("Python script with length: " + str(len(code)))
    if not approved:
        denied_event = build_action_event(
            source="brain.host_execution",
            action_type="host_python",
            status="denied",
            before_state={"command_hash": command_hash, "policy_decision": "approval_required"},
            after_state={"command_hash": command_hash, "policy_decision": "denied"},
        )
        _audit_log("Python Script", "python", approved=False, action_event=denied_event)
        return {
            "success": False,
            "error": "User denied execution of high-risk python code via native macOS dialog.",
            "output": "",
            "action_event": denied_event,
        }

    running_event = build_action_event(
        source="brain.host_execution",
        action_type="host_python",
        status="running",
        before_state={"command_hash": command_hash, "policy_decision": "user_approved"},
        after_state={"command_hash": command_hash, "policy_decision": "running"},
    )
    _audit_log("Python Script", "python", approved=True, action_event=running_event)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            tmp_path = f.name

        proc = await asyncio.create_subprocess_exec(
            sys.executable, tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        final_event = build_action_event(
            source="brain.host_execution",
            action_type="host_python",
            status="success" if proc.returncode == 0 else "failed",
            before_state=running_event["before_state"],
            after_state={"command_hash": command_hash, "policy_decision": "executed"},
            metadata={"exit_code": proc.returncode},
        )
        return {
            "success": proc.returncode == 0,
            "output": stdout.decode(),
            "error": stderr.decode(),
            "exit_code": proc.returncode,
            "action_event": final_event,
        }
    except Exception as e:
        failed_event = build_action_event(
            source="brain.host_execution",
            action_type="host_python",
            status="failed",
            before_state=running_event["before_state"],
            after_state={"command_hash": command_hash, "policy_decision": "execution_error"},
            metadata={"error": str(e)},
        )
        _audit_log("Python Script", "python", approved=True, error=str(e), action_event=failed_event)
        return {
            "success": False,
            "error": str(e),
            "output": "",
            "action_event": failed_event,
        }
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def register_host_execution_tools(registry, enable_native_exec: bool = False) -> None:
    if not enable_native_exec:
        logger.info("Skipping host execution tool registration (disabled by startup policy)")
        return

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
    command_hash = stable_hash(command)
