"""
Screen Agent — host-side desktop control node for Kestrel.

Runs natively on the host and exposes screenshot capture and
PyAutoGUI actions over HTTP. In local-native mode it registers
itself as a paired node with the Kestrel daemon.

Usage:
    pip install -r requirements.txt
    python screen_agent.py
"""

import base64
import io
import json
import logging
import os
import socket
import subprocess
import time
import uuid
from threading import Lock
from typing import Any, Optional

import pyautogui
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    import pytesseract
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None

try:
    import pygetwindow as gw
except Exception:  # pragma: no cover - optional dependency
    gw = None

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
    idempotency_key: Optional[str] = None


class ActionResponse(BaseModel):
    success: bool
    result: str = ""
    error: str = ""
    action_id: str = ""
    replayed: bool = False
    validation: dict[str, Any] = {}


class OCRRequest(BaseModel):
    region: Optional[dict[str, int]] = None
    needle: Optional[str] = None


class ValidationRequest(BaseModel):
    app: Optional[str] = None
    window_title: Optional[str] = None
    url_contains: Optional[str] = None
    text_contains: Optional[str] = None


class UIElementsRequest(BaseModel):
    provider: str = "none"
    options: dict[str, Any] = {}


ACTION_RESULT_CACHE: dict[str, dict[str, Any]] = {}
ACTION_CACHE_LOCK = Lock()
ACTION_CACHE_TTL_SECONDS = int(os.getenv("SCREEN_AGENT_ACTION_CACHE_TTL_SECONDS", "600"))
PAIRED_NODE_ID = os.getenv("KESTREL_SCREEN_NODE_ID", "screen-agent-local")


def _cache_set(key: str, value: dict[str, Any]) -> None:
    with ACTION_CACHE_LOCK:
        ACTION_RESULT_CACHE[key] = {"ts": time.time(), "value": value}


def _cache_get(key: str) -> Optional[dict[str, Any]]:
    now = time.time()
    with ACTION_CACHE_LOCK:
        expired = [
            cache_key
            for cache_key, entry in ACTION_RESULT_CACHE.items()
            if now - entry["ts"] > ACTION_CACHE_TTL_SECONDS
        ]
        for cache_key in expired:
            ACTION_RESULT_CACHE.pop(cache_key, None)

        entry = ACTION_RESULT_CACHE.get(key)
        if not entry:
            return None
        return entry["value"]


def _register_with_kestrel() -> None:
    payload = {
        "request_id": str(uuid.uuid4()),
        "method": "paired_nodes.register",
        "params": {
            "node_id": PAIRED_NODE_ID,
            "node_type": "screen",
            "capabilities": ["screenshot", "ocr", "desktop_actions", "validation"],
            "platform": os.name,
            "health": "ok",
            "address": os.getenv("SCREEN_AGENT_URL", "http://127.0.0.1:9800"),
            "workspace_binding": os.getenv("DEFAULT_WORKSPACE_ID", ""),
        },
    }
    raw = (json.dumps(payload) + "\n").encode("utf-8")

    try:
        if os.name == "nt":
            host = os.getenv("KESTREL_CONTROL_HOST", "127.0.0.1")
            port = int(os.getenv("KESTREL_CONTROL_PORT", "8749"))
            with socket.create_connection((host, port), timeout=1.5) as client:
                client.sendall(raw)
                client.recv(4096)
        else:
            kestrel_home = os.path.expanduser(os.getenv("KESTREL_HOME", "~/.kestrel"))
            control_socket = os.path.join(kestrel_home, "run", "control.sock")
            if not os.path.exists(control_socket):
                return
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.5)
                client.connect(control_socket)
                client.sendall(raw)
                client.recv(4096)
        logger.info("Registered screen agent as paired node")
    except Exception as exc:
        logger.info("Kestrel paired-node registration skipped: %s", exc)


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


def _run_osascript(script: str) -> str:
    try:
        output = subprocess.check_output(["osascript", "-e", script], text=True)
        return output.strip()
    except Exception:
        return ""


def _active_context() -> dict[str, str]:
    app_name = ""
    window_title = ""
    current_url = ""

    if gw is not None:
        try:
            active_window = gw.getActiveWindow()
            if active_window:
                window_title = getattr(active_window, "title", "") or ""
        except Exception:
            pass

    app_name = _run_osascript(
        'tell application "System Events" to get name of first application process whose frontmost is true'
    )
    if not window_title:
        window_title = _run_osascript(
            'tell application "System Events" to tell (first application process whose frontmost is true) to get name of front window'
        )

    if app_name == "Safari":
        current_url = _run_osascript('tell application "Safari" to return URL of front document')
    elif app_name == "Google Chrome":
        current_url = _run_osascript('tell application "Google Chrome" to return URL of active tab of front window')

    return {
        "app": app_name,
        "window_title": window_title,
        "url": current_url,
    }


def _capture_screen_image(region: Optional[dict[str, int]] = None):
    if region:
        left = max(0, region.get("left", 0))
        top = max(0, region.get("top", 0))
        width = max(1, region.get("width", SCREEN_WIDTH))
        height = max(1, region.get("height", SCREEN_HEIGHT))
        return pyautogui.screenshot(region=(left, top, width, height))
    return pyautogui.screenshot()


def _run_ocr(region: Optional[dict[str, int]] = None) -> dict[str, Any]:
    if pytesseract is None:
        return {
            "success": False,
            "error": "pytesseract not available in screen-agent environment",
            "text": "",
        }

    image = _capture_screen_image(region)
    text = pytesseract.image_to_string(image)
    return {
        "success": True,
        "text": text,
        "char_count": len(text),
    }


def _validate_expectations(expectations: ValidationRequest) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    context = _active_context()

    if expectations.app:
        expected = expectations.app.lower()
        actual = context.get("app", "")
        checks["app"] = {
            "expected": expectations.app,
            "actual": actual,
            "ok": expected in actual.lower(),
        }

    if expectations.window_title:
        expected = expectations.window_title.lower()
        actual = context.get("window_title", "")
        checks["window_title"] = {
            "expected": expectations.window_title,
            "actual": actual,
            "ok": expected in actual.lower(),
        }

    if expectations.url_contains:
        expected = expectations.url_contains.lower()
        actual = context.get("url", "")
        checks["url_contains"] = {
            "expected": expectations.url_contains,
            "actual": actual,
            "ok": expected in actual.lower(),
        }

    if expectations.text_contains:
        ocr_result = _run_ocr()
        actual_text = ocr_result.get("text", "") if ocr_result.get("success") else ""
        expected = expectations.text_contains.lower()
        checks["text_contains"] = {
            "expected": expectations.text_contains,
            "actual_excerpt": actual_text[:500],
            "ok": expected in actual_text.lower(),
            "ocr_success": ocr_result.get("success", False),
            "ocr_error": ocr_result.get("error", ""),
        }

    passed = all(item.get("ok", False) for item in checks.values()) if checks else True
    return {
        "passed": passed,
        "checks": checks,
        "context": context,
    }


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok", "service": "screen-agent", "node_id": PAIRED_NODE_ID}


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


@app.get("/active_window")
async def active_window():
    try:
        return {"success": True, **_active_context()}
    except Exception as e:
        logger.error(f"Active window introspection failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ocr")
async def ocr(req: OCRRequest):
    try:
        result = _run_ocr(req.region)
        if req.needle and result.get("success"):
            text = result.get("text", "")
            result["contains_needle"] = req.needle.lower() in text.lower()
        return result
    except Exception as e:
        logger.error(f"OCR failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/detect_ui_elements")
async def detect_ui_elements(req: UIElementsRequest):
    """
    Optional UI element detection hook.
    Integrators can replace this with a provider-backed implementation.
    """
    return {
        "success": False,
        "provider": req.provider,
        "elements": [],
        "error": "No UI element detector configured",
    }


@app.post("/validate")
async def validate(req: ValidationRequest):
    try:
        return {"success": True, **_validate_expectations(req)}
    except Exception as e:
        logger.error(f"Validation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/action", response_model=ActionResponse)
async def perform_action(req: ActionRequest):
    """Execute a desktop action via PyAutoGUI."""
    action_id = req.idempotency_key or str(uuid.uuid4())

    cached = _cache_get(action_id)
    if cached:
        return ActionResponse(
            success=cached.get("success", False),
            result=cached.get("result", ""),
            error=cached.get("error", ""),
            validation=cached.get("validation", {}),
            action_id=action_id,
            replayed=True,
        )

    try:
        result = execute_action(req.action, req.args)
        logger.info(f"Action: {req.action} -> {result}")
        validation = {}
        validate_args = req.args.get("validate")
        if isinstance(validate_args, dict):
            validation = _validate_expectations(ValidationRequest(**validate_args))
        response = {
            "success": True,
            "result": result,
            "error": "",
            "validation": validation,
        }
        _cache_set(action_id, response)
        return ActionResponse(**response, action_id=action_id)
    except Exception as e:
        logger.error(f"Action failed: {req.action} — {e}", exc_info=True)
        response = {
            "success": False,
            "result": "",
            "error": str(e),
            "validation": {},
        }
        _cache_set(action_id, response)
        return ActionResponse(**response, action_id=action_id)


# ── Entry Point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Kestrel Screen Agent on port 9800...")
    logger.info("This service must run directly on the host (not in Docker)")
    _register_with_kestrel()
    uvicorn.run(app, host="0.0.0.0", port=9800, log_level="info")
