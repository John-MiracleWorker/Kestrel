"""
Computer Use Skill — Mouse, keyboard, and screenshot control via pyautogui.
Inspired by Open Interpreter's OS mode and PyGPT's mouse/keyboard plugin.

Allows the AI to interact with the GUI like a human: click buttons,
type text, press hotkeys, take screenshots, and move the mouse.

Dependencies: pyautogui (auto-installed on first use).
"""

import json
import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger("libre_bird.skills.computer_use")

_pyautogui = None


def _ensure_pyautogui():
    """Lazy-load pyautogui."""
    global _pyautogui
    if _pyautogui is not None:
        return _pyautogui

    try:
        import pyautogui
    except ImportError:
        logger.info("Installing pyautogui...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pyautogui"])
        import pyautogui

    # Safety: add a small pause between actions and enable fail-safe
    pyautogui.PAUSE = 0.3
    pyautogui.FAILSAFE = True  # Move mouse to corner to abort

    _pyautogui = pyautogui
    return pyautogui


def tool_mouse_click(args: dict) -> dict:
    """Click at specific screen coordinates."""
    x = args.get("x")
    y = args.get("y")
    button = args.get("button", "left")
    clicks = int(args.get("clicks", 1))

    if x is None or y is None:
        return {"error": "x and y coordinates are required"}

    try:
        gui = _ensure_pyautogui()
        gui.click(x=int(x), y=int(y), button=button, clicks=clicks)
        return {
            "success": True,
            "action": f"{button}-clicked at ({x}, {y})",
            "clicks": clicks,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_type_text(args: dict) -> dict:
    """Type text using the keyboard (types into the currently focused window)."""
    text = args.get("text", "")
    if not text:
        return {"error": "text is required"}

    interval = float(args.get("interval", 0.02))

    try:
        gui = _ensure_pyautogui()
        gui.typewrite(text, interval=interval) if text.isascii() else gui.write(text)
        return {"success": True, "typed": text[:100], "length": len(text)}
    except Exception as e:
        return {"error": str(e)}


def tool_hotkey(args: dict) -> dict:
    """Press a keyboard shortcut (e.g. Cmd+C, Cmd+Tab, Cmd+Space)."""
    keys = args.get("keys", "")
    if not keys:
        return {"error": "keys is required (e.g. 'command,c' or 'command,shift,3')"}

    key_list = [k.strip() for k in keys.split(",")]

    try:
        gui = _ensure_pyautogui()
        gui.hotkey(*key_list)
        return {"success": True, "pressed": "+".join(key_list)}
    except Exception as e:
        return {"error": str(e)}


def tool_screenshot(args: dict) -> dict:
    """Take a screenshot of the current screen."""
    filename = args.get("filename", "screenshot.png")
    region = args.get("region", None)  # [x, y, width, height]

    try:
        gui = _ensure_pyautogui()

        # Determine save path
        save_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "screenshots")
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, filename)

        if region:
            if isinstance(region, str):
                region = json.loads(region)
            img = gui.screenshot(region=tuple(region))
        else:
            img = gui.screenshot()

        img.save(save_path)
        width, height = img.size

        return {
            "success": True,
            "path": save_path,
            "width": width,
            "height": height,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_mouse_move(args: dict) -> dict:
    """Move the mouse to specific coordinates."""
    x = args.get("x")
    y = args.get("y")
    duration = float(args.get("duration", 0.3))

    if x is None or y is None:
        return {"error": "x and y coordinates are required"}

    try:
        gui = _ensure_pyautogui()
        gui.moveTo(int(x), int(y), duration=duration)
        return {"success": True, "position": {"x": int(x), "y": int(y)}}
    except Exception as e:
        return {"error": str(e)}


def tool_get_mouse_position(args: dict) -> dict:
    """Get the current mouse cursor position."""
    try:
        gui = _ensure_pyautogui()
        pos = gui.position()
        screen_size = gui.size()
        return {
            "x": pos.x,
            "y": pos.y,
            "screen_width": screen_size.width,
            "screen_height": screen_size.height,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "mouse_click",
            "description": "Click the mouse at specific screen coordinates. Supports left/right click and double-click.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate on screen"},
                    "y": {"type": "integer", "description": "Y coordinate on screen"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default left)"},
                    "clicks": {"type": "integer", "description": "Number of clicks (default 1, use 2 for double-click)"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the currently focused window, as if typed on the keyboard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type"},
                    "interval": {"type": "number", "description": "Seconds between keystrokes (default 0.02)"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hotkey",
            "description": "Press a keyboard shortcut. Keys are comma-separated (e.g. 'command,c' for ⌘C, 'command,shift,3' for screenshot).",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string", "description": "Comma-separated key names (e.g. 'command,c', 'alt,tab', 'command,space')"},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Take a screenshot of the current screen. Optionally capture a specific region.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Filename for the screenshot (default 'screenshot.png')"},
                    "region": {"type": "string", "description": "Optional region as JSON array: [x, y, width, height]"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_move",
            "description": "Move the mouse cursor to specific screen coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate"},
                    "y": {"type": "integer", "description": "Y coordinate"},
                    "duration": {"type": "number", "description": "Seconds for the move animation (default 0.3)"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_mouse_position",
            "description": "Get the current mouse cursor position and screen dimensions.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

TOOL_HANDLERS = {
    "mouse_click": tool_mouse_click,
    "type_text": tool_type_text,
    "hotkey": tool_hotkey,
    "screenshot": tool_screenshot,
    "mouse_move": tool_mouse_move,
    "get_mouse_position": tool_get_mouse_position,
}
