from typing import Optional
"""
Gemini Computer Use — Desktop control via Gemini's Computer Use model.

Uses the Gemini 2.5/3 Computer Use API to let the agent see and control
the user's desktop: take screenshots, click, type, scroll, drag, and
press key combinations.

The agentic loop:
  1. Request a screenshot from the host-side screen agent
  2. Send it to Gemini Computer Use model with the user's goal
  3. Parse the returned FunctionCall actions (click_at, type_text_at, etc.)
  4. Send each action to the screen agent for execution
  5. Request a new screenshot showing the result
  6. Repeat until the model signals completion or the turn limit is hit

Safety:
  - All actions are logged and have a maximum turn limit (HIGH risk level)
  - Safety decisions from the model that require confirmation are surfaced
  - Maximum turn limit prevents runaway loops
  - Fail-open: if screenshot capture fails, the loop halts immediately

Architecture:
  The Brain runs in Docker but needs native screen access.
  A lightweight screen agent runs on the host Mac and exposes:
    GET  /screenshot  → base64 PNG of current screen
    POST /action      → execute a PyAutoGUI action

Coordinate system:
  Gemini returns coordinates on a normalized 0-999 grid.
  The screen agent scales them to actual screen dimensions.
"""

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any, Optional

import httpx

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.computer_use")

# ── Configuration ────────────────────────────────────────────────────

# Model is resolved dynamically from the registry at first use.
# Set GEMINI_COMPUTER_USE_MODEL env var to override.
COMPUTER_USE_MODEL = os.getenv("GEMINI_COMPUTER_USE_MODEL", "")
MAX_TURNS = int(os.getenv("COMPUTER_USE_MAX_TURNS", "30"))
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Models known to support the computerUse tool, in preference order.
# The model registry may return speculative/future names that don't exist yet
# in the real Google API, causing 404s.  This list pins stable, verified IDs.
COMPUTER_USE_KNOWN_MODELS = [
    "gemini-2.5-flash-preview-04-17",
    "gemini-2.0-flash-exp",
    "gemini-2.5-pro-preview-05-06",
    "gemini-2.5-flash-lite-preview-06-17",
]


async def _resolve_computer_use_model() -> str:
    """Resolve the computer use model dynamically from the registry.

    The model registry may return speculative model names (e.g. gemini-3-flash-preview)
    from its hardcoded catalog that don't yet exist in the Google API, causing 404s.
    We validate the resolved name against COMPUTER_USE_KNOWN_MODELS and fall back
    to a stable known-working model when the registry returns something unfamiliar.
    """
    global COMPUTER_USE_MODEL
    if COMPUTER_USE_MODEL:
        return COMPUTER_USE_MODEL
    try:
        from core.model_registry import model_registry
        candidate = await model_registry.get_fast_model("google")
        # Only use the registry result if it looks like a real, versioned Gemini model
        # (contains a date suffix or is in our known-good list).
        import re as _re
        is_dated = bool(_re.search(r"\d{2}-\d{2}", candidate))
        if candidate and (is_dated or candidate in COMPUTER_USE_KNOWN_MODELS):
            COMPUTER_USE_MODEL = candidate
            logger.info(f"Computer use model resolved from registry: {COMPUTER_USE_MODEL}")
        else:
            COMPUTER_USE_MODEL = COMPUTER_USE_KNOWN_MODELS[0]
            logger.info(
                f"Registry returned '{candidate}' which may not support computerUse; "
                f"using known-good fallback: {COMPUTER_USE_MODEL}"
            )
    except Exception:
        COMPUTER_USE_MODEL = COMPUTER_USE_KNOWN_MODELS[0]
    return COMPUTER_USE_MODEL

# Host-side screen agent URL (runs natively on the Mac)
SCREEN_AGENT_URL = os.getenv("SCREEN_AGENT_URL", "http://host.docker.internal:9800")

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
    "open_web_browser",
    "open_url",
}


# ── Screen Agent Communication ───────────────────────────────────────


async def _capture_screenshot() -> bytes:
    """
    Request a screenshot from the host-side screen agent.
    Returns raw PNG bytes.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{SCREEN_AGENT_URL}/screenshot")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Screen agent screenshot failed ({resp.status_code}): {resp.text[:200]}"
                )
            data = resp.json()
            if not data.get("success"):
                raise RuntimeError(f"Screen agent error: {data}")
            return base64.b64decode(data["image_base64"])
    except httpx.ConnectError:
        raise RuntimeError(
            "Cannot connect to the screen agent. "
            "Make sure it's running on the host: "
            "cd packages/screen-agent && python screen_agent.py"
        )
    except httpx.TimeoutException:
        raise RuntimeError("Screen agent timed out capturing screenshot")


async def _execute_action(action_name: str, args: dict) -> str:
    """
    Send an action to the host-side screen agent for execution.
    Returns a human-readable description of what was done.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SCREEN_AGENT_URL}/action",
                json={"action": action_name, "args": args},
            )
            if resp.status_code != 200:
                return f"Screen agent error ({resp.status_code}): {resp.text[:200]}"
            data = resp.json()
            if data.get("success"):
                return data.get("result", "Action completed")
            else:
                return f"Action failed: {data.get('error', 'unknown')}"
    except httpx.ConnectError:
        return "ERROR: Cannot connect to screen agent"
    except httpx.TimeoutException:
        return "ERROR: Screen agent timed out"


# ── Gemini API Interaction ───────────────────────────────────────────


async def _call_gemini_computer_use(
    contents: list[dict],
    api_key: str,
    model: str = "",
    system_instruction: str = "",
    excluded_actions: Optional[list[str]] = None,
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
            "environment": "ENVIRONMENT_BROWSER",
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
    # Use explicit timeouts — screenshot payloads are large and the model
    # needs time to process the image, so the read timeout must be generous.
    timeout = httpx.Timeout(connect=15.0, write=30.0, read=180.0, pool=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(url, json=payload)
            except httpx.ReadTimeout:
                if attempt < max_retries:
                    delay = 2 ** attempt + 2
                    logger.warning(
                        f"Gemini Computer Use API read timeout, "
                        f"retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(
                    "Gemini Computer Use API timed out after all retries. "
                    "The model may be overloaded or the screenshot is too large."
                )

            if resp.status_code == 429:
                error_body = resp.text[:500]
                # Extract retry-after header if present
                retry_after = resp.headers.get("retry-after", "")
                retry_hint = f" Retry after {retry_after}s." if retry_after else ""
                if "quota" in error_body.lower() or "limit: 0" in error_body:
                    raise RuntimeError(
                        f"Gemini API quota exhausted.{retry_hint} "
                        "Check your plan and billing "
                        "at https://ai.google.dev/gemini-api/docs/rate-limits — "
                        "free-tier keys have very low computer-use limits. "
                        "Upgrade to a paid plan or use a different GOOGLE_API_KEY."
                    )
                if attempt < max_retries:
                    delay = 2 ** attempt + 1
                    logger.warning(
                        f"Gemini Computer Use API 429, "
                        f"retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay)
                    continue

            if resp.status_code in (503, 500) and attempt < max_retries:
                delay = 2 ** attempt
                logger.warning(
                    f"Gemini Computer Use API {resp.status_code}, "
                    f"retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 404:
                # The model doesn't exist at this endpoint.  This usually means
                # the model name from the registry is speculative/future or doesn't
                # support computerUse.  Surface a clear error so the caller can
                # try a different model via COMPUTER_USE_KNOWN_MODELS.
                import re
                error_text = re.sub(r"key=[A-Za-z0-9_-]+", "key=***", resp.text[:500])
                raise RuntimeError(
                    f"Gemini Computer Use model not found (404): '{model}' does not exist "
                    f"or does not support the computerUse tool. "
                    f"Set GEMINI_COMPUTER_USE_MODEL to one of: "
                    f"{', '.join(COMPUTER_USE_KNOWN_MODELS)}. "
                    f"API response: {error_text}"
                )

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
    excluded_actions: Optional[list[str]] = None,
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
    global COMPUTER_USE_MODEL

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

    # Build a fallback chain: preferred model first, then remaining known models
    model_chain = [model] + [m for m in COMPUTER_USE_KNOWN_MODELS if m != model]
    model = model_chain[0]
    model_chain_idx = 0

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
        screenshot_bytes = await _capture_screenshot()
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

        # Call Gemini — on 404 automatically try the next model in the chain
        try:
            response = await _call_gemini_computer_use(
                contents=contents,
                api_key=api_key,
                model=model,
                system_instruction=system,
                excluded_actions=excluded_actions,
            )
        except RuntimeError as e:
            err_str = str(e)
            if "404" in err_str and model_chain_idx + 1 < len(model_chain):
                # Try the next known-good model
                model_chain_idx += 1
                model = model_chain[model_chain_idx]
                # Update the global so the next top-level call starts here
                COMPUTER_USE_MODEL = model
                logger.warning(
                    f"Computer Use model {model_chain[model_chain_idx - 1]} returned 404; "
                    f"retrying with {model}"
                )
                continue
            return {
                "success": False,
                "error": err_str,
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

        # Execute each action via the screen agent
        function_responses = []
        for action in actions:
            action_name = action["name"]
            action_args = action["args"]

            if action_name not in SUPPORTED_ACTIONS:
                result_text = f"Unsupported action: {action_name}"
                logger.warning(result_text)
            else:
                try:
                    result_text = await _execute_action(action_name, action_args)
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
            screenshot_bytes = await _capture_screenshot()
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
    risk_level=RiskLevel.HIGH,
    requires_approval=False,
    timeout_seconds=600,
    category="computer_use",
)


def register_computer_use_tools(registry) -> None:
    """Register the computer_use tool with the agent's tool registry."""

    async def computer_use_handler(
        goal: str,
        max_turns: int = 30,
        excluded_actions: Optional[list[str]] = None,
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
