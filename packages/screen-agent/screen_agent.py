"""
Screen Agent — Host-side HTTP bridge for desktop control.

Runs natively on the Mac (NOT in Docker) and exposes screenshot
capture and PyAutoGUI actions over HTTP. The Brain container calls
this service via host.docker.internal:9800.

Usage:
    pip install -r requirements.txt
    python screen_agent.py
"""

import base64
import io
import logging
import time
from typing import Any, Optional

import pyautogui
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("screen-agent")

app = FastAPI(title="Kestrel Screen Agent", version="1.0.0")

# ── Configuration ────────────────────────────────────────────────────

SCREEN_WIDTH = 1440
SCREEN_HEIGHT = 900

# Safety: small delay between actions so the user can observe
pyautogui.PAUSE = 0.3
pyautogui.FAILSAFE = True  # Move mouse to corner to abort


# ── Models ───────────────────────────────────────────────────────────


class ActionRequest(BaseModel):
    action: str
    args: dict[str, Any] = {}


class ActionResponse(BaseModel):
    success: bool
    result: str = ""
    error: str = ""


# ── Coordinate Helpers ───────────────────────────────────────────────


def _denormalize_x(x: int) -> int:
    """Map Gemini's 0-999 x-coordinate to actual screen pixels."""
    return int(x / 1000 * SCREEN_WIDTH)


def _denormalize_y(y: int) -> int:
    """Map Gemini's 0-999 y-coordinate to actual screen pixels."""
    return int(y / 1000 * SCREEN_HEIGHT)


# ── Action Execution ─────────────────────────────────────────────────


def execute_action(action_name: str, args: dict) -> str:
    """Execute a single UI action on the desktop."""

    if action_name == "click_at":
        x = _denormalize_x(args.get("x", 0))
        y = _denormalize_y(args.get("y", 0))
        button = args.get("button", "left")
        pyautogui.click(x, y, button=button)
        return f"Clicked ({x}, {y}) [{button}]"

    elif action_name == "type_text_at":
        x = _denormalize_x(args.get("x", 0))
        y = _denormalize_y(args.get("y", 0))
        text = args.get("text", "")
        press_enter = args.get("press_enter", False)
        clear_before = args.get("clear_before_typing", False)

        pyautogui.click(x, y)
        if clear_before:
            pyautogui.hotkey("command", "a")
            pyautogui.press("delete")
        if text.isascii():
            pyautogui.typewrite(text, interval=0.02)
        else:
            pyautogui.write(text)
        if press_enter:
            pyautogui.press("enter")
        return f"Typed '{text[:50]}...' at ({x}, {y})"

    elif action_name == "scroll_document":
        direction = args.get("direction", "down")
        clicks = 5 if direction in ("down", "right") else -5
        if direction in ("up", "down"):
            pyautogui.scroll(clicks)
        else:
            pyautogui.hscroll(clicks)
        return f"Scrolled {direction}"

    elif action_name == "scroll_at":
        x = _denormalize_x(args.get("x", 0))
        y = _denormalize_y(args.get("y", 0))
        direction = args.get("direction", "down")
        magnitude = args.get("magnitude", 3)
        clicks = magnitude if direction in ("down", "right") else -magnitude
        pyautogui.moveTo(x, y)
        pyautogui.scroll(clicks)
        return f"Scrolled {direction} ({magnitude}) at ({x}, {y})"

    elif action_name == "hover_at":
        x = _denormalize_x(args.get("x", 0))
        y = _denormalize_y(args.get("y", 0))
        pyautogui.moveTo(x, y, duration=0.3)
        return f"Hovered at ({x}, {y})"

    elif action_name == "key_combination":
        keys = args.get("keys", "")
        # Gemini sends "Control+A" — adapt for macOS
        key_list = [
            k.strip().lower().replace("control", "command").replace("ctrl", "command")
            for k in keys.split("+")
        ]
        pyautogui.hotkey(*key_list)
        return f"Pressed {keys}"

    elif action_name == "drag_and_drop":
        sx = _denormalize_x(args.get("x", 0))
        sy = _denormalize_y(args.get("y", 0))
        dx = _denormalize_x(args.get("destination_x", 0))
        dy = _denormalize_y(args.get("destination_y", 0))
        pyautogui.moveTo(sx, sy)
        pyautogui.drag(dx - sx, dy - sy, duration=0.5)
        return f"Dragged ({sx},{sy}) -> ({dx},{dy})"

    elif action_name == "wait_5_seconds":
        time.sleep(5)
        return "Waited 5 seconds"

    elif action_name == "navigate":
        url = args.get("url", "")
        import webbrowser
        webbrowser.open(url)
        return f"Opened URL: {url}"

    elif action_name == "go_back":
        pyautogui.hotkey("command", "left")
        return "Browser: go back"

    elif action_name == "go_forward":
        pyautogui.hotkey("command", "right")
        return "Browser: go forward"

    else:
        return f"Unknown action: {action_name}"


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok", "service": "screen-agent"}


@app.get("/screenshot")
async def screenshot():
    """Capture the current screen and return as base64 PNG."""
    try:
        img = pyautogui.screenshot()
        img = img.resize((SCREEN_WIDTH, SCREEN_HEIGHT))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        return {"success": True, "image_base64": b64}
    except Exception as e:
        logger.error(f"Screenshot failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/action", response_model=ActionResponse)
async def perform_action(req: ActionRequest):
    """Execute a desktop action via PyAutoGUI."""
    try:
        result = execute_action(req.action, req.args)
        logger.info(f"Action: {req.action} -> {result}")
        return ActionResponse(success=True, result=result)
    except Exception as e:
        logger.error(f"Action failed: {req.action} — {e}", exc_info=True)
        return ActionResponse(success=False, error=str(e))


# ── Entry Point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Kestrel Screen Agent on port 9800...")
    logger.info("This service must run directly on the host (not in Docker)")
    uvicorn.run(app, host="0.0.0.0", port=9800, log_level="info")
