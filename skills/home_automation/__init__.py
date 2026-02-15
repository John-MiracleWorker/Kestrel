"""
Home Automation Skill
Run macOS Shortcuts, list Shortcuts, interact with HomeKit devices.
Uses the macOS 'shortcuts' CLI tool (built into macOS 12+).
"""

import json
import subprocess


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_shortcut",
            "description": "Run a macOS Shortcut by name. Shortcuts can control HomeKit devices, automate tasks, send messages, and more. Use when the user says 'run my shortcut called ...', 'turn on the lights', 'run automation', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The exact name of the Shortcut to run"},
                    "input": {"type": "string", "description": "Optional text input to pass to the Shortcut"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_shortcuts",
            "description": "List all available macOS Shortcuts. Use to discover what Shortcuts the user has installed.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "homekit_devices",
            "description": "List HomeKit-connected devices and their states. Uses Shortcuts to query the Home app. Note: Requires a Shortcut named 'List HomeKit Devices' to be set up.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_run_shortcut(name: str, input_text: str = None) -> dict:
    try:
        cmd = ["shortcuts", "run", name]
        input_data = input_text.encode() if input_text else None
        result = subprocess.run(
            cmd, input=input_data, capture_output=True, text=False, timeout=30
        )
        output = result.stdout.decode("utf-8", errors="replace").strip() if result.stdout else ""
        error = result.stderr.decode("utf-8", errors="replace").strip() if result.stderr else ""
        if result.returncode == 0:
            return {
                "shortcut": name, "status": "success",
                "output": output[:5000] if output else "(no output)",
            }
        else:
            return {
                "shortcut": name, "status": "error",
                "error": error or f"Shortcut exited with code {result.returncode}",
                "hint": f"Make sure a Shortcut named '{name}' exists. Run 'shortcuts list' to see available shortcuts.",
            }
    except subprocess.TimeoutExpired:
        return {"shortcut": name, "error": "Shortcut timed out after 30 seconds"}
    except FileNotFoundError:
        return {"error": "'shortcuts' command not found. Requires macOS 12 Monterey or later."}
    except Exception as e:
        return {"shortcut": name, "error": f"Failed to run shortcut: {str(e)}"}


def tool_list_shortcuts() -> dict:
    try:
        result = subprocess.run(
            ["shortcuts", "list"], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return {"error": f"Could not list shortcuts: {result.stderr.strip()}"}
        shortcuts = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
        return {
            "shortcuts": shortcuts,
            "count": len(shortcuts),
            "hint": "Use run_shortcut with any of these names to execute them.",
        }
    except FileNotFoundError:
        return {"error": "'shortcuts' command not found. Requires macOS 12 Monterey or later."}
    except Exception as e:
        return {"error": f"Failed to list shortcuts: {str(e)}"}


def tool_homekit_devices() -> dict:
    # Try running a user-created shortcut that lists HomeKit devices
    result = tool_run_shortcut("List HomeKit Devices")
    if result.get("status") == "success":
        return {
            "devices": result.get("output", ""),
            "source": "Shortcuts",
            "note": "Device info comes from a Shortcut named 'List HomeKit Devices'. Create this Shortcut in the Shortcuts app to customize the output."
        }
    else:
        # Fallback: try AppleScript approach
        try:
            script = '''
            tell application "Home"
                set deviceList to {}
                try
                    set homeList to every home
                    repeat with h in homeList
                        set roomList to every room of h
                        repeat with r in roomList
                            set accList to every accessory of r
                            repeat with a in accList
                                set end of deviceList to (name of a & " (" & name of r & ")")
                            end repeat
                        end repeat
                    end repeat
                end try
                return deviceList as string
            end tell
            '''
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                devices = [d.strip() for d in result.stdout.strip().split(",") if d.strip()]
                return {"devices": devices, "count": len(devices), "source": "Home app (AppleScript)"}
        except Exception:
            pass
        return {
            "devices": [],
            "message": "Could not list HomeKit devices. Create a Shortcut called 'List HomeKit Devices' in the Shortcuts app for best results.",
            "hint": "In Shortcuts app: New Shortcut → Add 'Get State of Home Accessory' for each device, then connect the outputs."
        }


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "run_shortcut": lambda args: tool_run_shortcut(args.get("name", ""), args.get("input")),
    "list_shortcuts": lambda args: tool_list_shortcuts(),
    "homekit_devices": lambda args: tool_homekit_devices(),
}
