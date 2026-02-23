"""
Gemini Computer Use — Desktop control via Gemini's Computer Use model.

Uses the Gemini 2.5/3 Computer Use API to let the agent see and control
the user's desktop: take screenshots, click, type, scroll, drag, and
press key combinations.

The agentic loop:
  1. Capture a screenshot of the desktop
  2. Send it to Gemini Computer Use model with the user's goal
  3. Parse the returned FunctionCall actions (click_at, type_text_at, etc.)
  4. Execute each action via PyAutoGUI
  5. Capture a new screenshot showing the result
  6. Repeat until the model signals completion or the turn limit is hit

Safety:
  - All actions require approval by default (CRITICAL risk level)
  - Safety decisions from the model that require confirmation are surfaced
  - Maximum turn limit prevents runaway loops
  - Fail-open: if screenshot capture fails, the loop halts immediately

Coordinate system:
  Gemini returns coordinates on a normalized 0-999 grid.
  We scale them to actual screen dimensions before executing.
"""

import asyncio
import base64
import io
import json
import logging
import os
import time
from typing import Any, Optional

import httpx

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.computer_use")

# ── Configuration ────────────────────────────────────────────────────

COMPUTER_USE_MODEL = os.getenv(
    "GEMINI_COMPUTER_USE_MODEL",
    "gemini-2.5-computer-use-preview-10-2025",
)
# Gemini normalizes to a 1000x1000 grid; recommended real resolution:
SCREEN_WIDTH = int(os.getenv("COMPUTER_USE_SCREEN_WIDTH", "1440"))
SCREEN_HEIGHT = int(os.getenv("COMPUTER_USE_SCREEN_HEIGHT", "900"))
MAX_TURNS = int(os.getenv("COMPUTER_USE_MAX_TURNS", "30"))
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Actions the model can request
SUPPORTED_ACTIONS = {
    "click_at",
    "type_text_at",
    "scroll_document",
    "scroll_at",
    "navigate",
    "go_back",
    "go_forward",
    "hover_at",
    "key_combination",
    "drag_and_drop",
    "wait_5_seconds",
}


# ── Coordinate Helpers ───────────────────────────────────────────────


def _denormalize_x(x: int) -> int:
    """Map Gemini's 0-999 x-coordinate to actual screen pixels."""
    return int(x / 1000 * SCREEN_WIDTH)


def _denormalize_y(y: int) -> int:
    """Map Gemini's 0-999 y-coordinate to actual screen pixels."""
    return int(y / 1000 * SCREEN_HEIGHT)


# ── Screenshot Capture ───────────────────────────────────────────────


def _capture_screenshot() -> bytes:
    """
    Capture the current desktop as a PNG byte string.
    Falls back gracefully if pyautogui isn't available.
    """
    try:
        import pyautogui

        img = pyautogui.screenshot()
        # Resize to the expected resolution for consistent coordinates
        img = img.resize((SCREEN_WIDTH, SCREEN_HEIGHT))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        raise RuntimeError(
            "pyautogui is required for desktop computer use. "
            "Install it with: pip install pyautogui"
        )
    except Exception as e:
        raise RuntimeError(f"Screenshot capture failed: {e}")


# ── Action Execution ─────────────────────────────────────────────────


def _execute_action(action_name: str, args: dict) -> str:
    """
    Execute a single UI action on the desktop.
    Returns a human-readable description of what was done.
    """
    import pyautogui

    # Safety: small delay between actions so the user can observe
    pyautogui.PAUSE = 0.3

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
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("delete")
        pyautogui.typewrite(text, interval=0.02) if text.isascii() else pyautogui.write(text)
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
        # Gemini sends keys like "Control+A" — pyautogui wants ("ctrl", "a")
        key_list = [k.strip().lower().replace("control", "ctrl") for k in keys.split("+")]
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
        # Open URL in the default browser
        import webbrowser
        webbrowser.open(url)
        return f"Opened URL: {url}"

    elif action_name == "go_back":
        pyautogui.hotkey("alt", "left")
        return "Browser: go back"

    elif action_name == "go_forward":
        pyautogui.hotkey("alt", "right")
        return "Browser: go forward"

    else:
        return f"Unknown action: {action_name}"


# ── Gemini API Interaction ───────────────────────────────────────────


async def _call_gemini_computer_use(
    contents: list[dict],
    api_key: str,
    model: str = "",
    system_instruction: str = "",
    excluded_actions: list[str] | None = None,
) -> dict:
    """
    Call the Gemini Computer Use endpoint.

    Returns the raw response JSON from the API.
    """
    model = model or COMPUTER_USE_MODEL
    url = f"{GEMINI_BASE_URL}/{model}:generateContent?key={api_key}"

    # Build the computer use tool config
    computer_use_tool: dict[str, Any] = {
        "computerUse": {
            "environment": "ENVIRONMENT_SCREEN",
        }
    }
    if excluded_actions:
        computer_use_tool["computerUse"]["excludedPredefinedFunctions"] = excluded_actions

    payload: dict[str, Any] = {
        "contents": contents,
        "tools": [computer_use_tool],
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature": 0.1,
        },
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    max_retries = 3
    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(max_retries + 1):
            resp = await client.post(url, json=payload)
            if resp.status_code in (503, 429, 500) and attempt < max_retries:
                delay = 2 ** attempt
                logger.warning(
                    f"Gemini Computer Use API {resp.status_code}, "
                    f"retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
                continue
            if resp.status_code != 200:
                error_text = resp.text[:1000]
                # Scrub API key from error
                import re
                error_text = re.sub(r"key=[A-Za-z0-9_-]+", "key=***", error_text)
                raise RuntimeError(
                    f"Gemini Computer Use API error ({resp.status_code}): {error_text}"
                )
            return resp.json()

    raise RuntimeError("Gemini Computer Use API: max retries exceeded")


def _parse_actions(response_data: dict) -> tuple[str, list[dict], bool]:
    """
    Parse the Gemini response into text, actions, and a completion flag.

    Returns:
        (model_text, actions_list, has_safety_confirmation_needed)
    """
    parts = (
        response_data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [])
    )

    text = ""
    actions = []
    needs_confirmation = False

    for part in parts:
        if "text" in part:
            text += part["text"]
        elif "functionCall" in part:
            fc = part["functionCall"]
            action = {
                "name": fc.get("name", ""),
                "args": fc.get("args", {}),
            }
            # Check for safety decisions requiring confirmation
            safety = action["args"].pop("safety_decision", None)
            if safety and "require_confirmation" in str(safety).lower():
                needs_confirmation = True
                action["needs_confirmation"] = True
            actions.append(action)

    return text, actions, needs_confirmation


# ── Main Agent Loop ──────────────────────────────────────────────────


async def run_computer_use(
    goal: str,
    api_key: str = "",
    model: str = "",
    max_turns: int = 0,
    system_prompt: str = "",
    excluded_actions: list[str] | None = None,
    require_confirmation: bool = True,
) -> dict:
    """
    Run the Gemini Computer Use agent loop on the user's desktop.

    Args:
        goal: Natural language description of what to accomplish.
        api_key: Google API key. Falls back to GOOGLE_API_KEY env var.
        model: Model ID override. Defaults to COMPUTER_USE_MODEL.
        max_turns: Max screenshot->action cycles. Defaults to MAX_TURNS.
        system_prompt: Additional instructions for the model.
        excluded_actions: Actions to disable (e.g., ["drag_and_drop"]).
        require_confirmation: If True, pause before executing actions
            and return early so the caller can confirm.

    Returns:
        Dict with keys: success, turns, actions_taken, model_commentary, error
    """
    api_key = api_key or os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return {
            "success": False,
            "error": "No Google API key found. Set GOOGLE_API_KEY or pass api_key.",
            "turns": 0,
            "actions_taken": [],
            "model_commentary": "",
        }

    max_turns = max_turns or MAX_TURNS
    model = model or COMPUTER_USE_MODEL
    actions_taken: list[dict] = []
    commentary: list[str] = []

    default_system = (
        "You are controlling a desktop computer to accomplish the user's goal. "
        "Observe the screenshot carefully. Take one action at a time. "
        "When the goal is accomplished, respond with text only (no function calls) "
        "summarizing what you did."
    )
    system = system_prompt or default_system

    # Initial screenshot
    try:
        screenshot_bytes = await asyncio.to_thread(_capture_screenshot)
    except RuntimeError as e:
        return {
            "success": False,
            "error": str(e),
            "turns": 0,
            "actions_taken": [],
            "model_commentary": "",
        }

    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    # Build initial message
    contents = [
        {
            "role": "user",
            "parts": [
                {"text": goal},
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": screenshot_b64,
                    }
                },
            ],
        }
    ]

    for turn in range(max_turns):
        logger.info(f"Computer Use turn {turn + 1}/{max_turns}")

        # Call Gemini
        try:
            response = await _call_gemini_computer_use(
                contents=contents,
                api_key=api_key,
                model=model,
                system_instruction=system,
                excluded_actions=excluded_actions,
            )
        except RuntimeError as e:
            return {
                "success": False,
                "error": str(e),
                "turns": turn + 1,
                "actions_taken": actions_taken,
                "model_commentary": "\n".join(commentary),
            }

        # Parse response
        model_text, actions, needs_confirm = _parse_actions(response)

        if model_text:
            commentary.append(model_text)
            logger.info(f"Model says: {model_text[:200]}")

        # If no actions returned, the model considers the task done
        if not actions:
            logger.info("No actions returned — task complete")
            return {
                "success": True,
                "turns": turn + 1,
                "actions_taken": actions_taken,
                "model_commentary": "\n".join(commentary),
            }

        # If confirmation is needed and enabled, return for human review
        if needs_confirm and require_confirmation:
            return {
                "success": False,
                "error": "safety_confirmation_required",
                "pending_actions": actions,
                "turns": turn + 1,
                "actions_taken": actions_taken,
                "model_commentary": "\n".join(commentary),
            }

        # Append the model's response to the conversation
        model_parts = (
            response.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        contents.append({"role": "model", "parts": model_parts})

        # Execute each action
        function_responses = []
        for action in actions:
            action_name = action["name"]
            action_args = action["args"]

            if action_name not in SUPPORTED_ACTIONS:
                result_text = f"Unsupported action: {action_name}"
                logger.warning(result_text)
            else:
                try:
                    result_text = await asyncio.to_thread(
                        _execute_action, action_name, action_args
                    )
                    logger.info(f"Executed: {result_text}")
                except Exception as e:
                    result_text = f"Action failed: {e}"
                    logger.error(result_text, exc_info=True)

            actions_taken.append({
                "action": action_name,
                "args": action_args,
                "result": result_text,
                "turn": turn + 1,
            })

            function_responses.append({
                "functionResponse": {
                    "name": action_name,
                    "response": {"result": result_text},
                }
            })

        # Short pause for the UI to settle after actions
        await asyncio.sleep(0.5)

        # Capture fresh screenshot after actions
        try:
            screenshot_bytes = await asyncio.to_thread(_capture_screenshot)
        except RuntimeError as e:
            return {
                "success": False,
                "error": f"Screenshot failed after actions: {e}",
                "turns": turn + 1,
                "actions_taken": actions_taken,
                "model_commentary": "\n".join(commentary),
            }

        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        # Send function responses + new screenshot back to the model
        user_parts = function_responses + [
            {
                "inlineData": {
                    "mimeType": "image/png",
                    "data": screenshot_b64,
                }
            }
        ]
        contents.append({"role": "user", "parts": user_parts})

    # Hit the turn limit
    return {
        "success": False,
        "error": f"Reached maximum turn limit ({max_turns})",
        "turns": max_turns,
        "actions_taken": actions_taken,
        "model_commentary": "\n".join(commentary),
    }


# ── Tool Registration ────────────────────────────────────────────────

COMPUTER_USE_TOOL = ToolDefinition(
    name="computer_use",
    description=(
        "Control the user's desktop using Gemini Computer Use. "
        "Takes a natural language goal and autonomously operates the screen — "
        "clicking, typing, scrolling, and navigating — to accomplish it. "
        "Captures screenshots to observe the current state and decides "
        "the next action. Use this for tasks that require interacting with "
        "GUI applications, browsers, or any on-screen element."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "Natural language description of what to accomplish on the desktop. "
                    "Be specific: 'Open Firefox and search for weather in London' "
                    "rather than 'search the web'."
                ),
            },
            "max_turns": {
                "type": "integer",
                "description": "Maximum screenshot-action cycles (default: 30).",
                "default": 30,
            },
            "excluded_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Actions to disable. Options: click_at, type_text_at, "
                    "scroll_document, scroll_at, navigate, go_back, go_forward, "
                    "hover_at, key_combination, drag_and_drop, wait_5_seconds."
                ),
            },
        },
        "required": ["goal"],
    },
    risk_level=RiskLevel.CRITICAL,
    requires_approval=True,
    timeout_seconds=600,
    category="computer_use",
)


def register_computer_use_tools(registry) -> None:
    """Register the computer_use tool with the agent's tool registry."""

    async def computer_use_handler(
        goal: str,
        max_turns: int = 30,
        excluded_actions: list[str] | None = None,
    ) -> dict:
        """Handle computer_use tool calls from the agent loop."""
        return await run_computer_use(
            goal=goal,
            max_turns=max_turns,
            excluded_actions=excluded_actions,
            require_confirmation=True,
        )

    registry.register(definition=COMPUTER_USE_TOOL, handler=computer_use_handler)
    logger.info("Computer Use tool registered")
