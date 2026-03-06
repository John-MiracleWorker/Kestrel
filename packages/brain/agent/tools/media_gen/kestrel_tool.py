"""
Kestrel Tool Interface — main entry point for AI media generation.

This module is the orchestrator that ties together the SwarmClient, WebSocket
monitor, and workflow injection to provide a single generate_media() function
that the Kestrel Agent calls as a tool.

Flow:
  1. Load the appropriate ComfyUI workflow JSON template
  2. Inject the user's prompt into CLIPTextEncode nodes
  3. Submit the job to the remote SwarmUI host
  4. Monitor progress via WebSocket
  5. Download the final artifact to local disk
  6. Return the file path (and optionally deliver via Telegram)
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from agent.tools.media_gen.config import (
    IMAGE_TIMEOUT,
    OUTPUT_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    VIDEO_TIMEOUT,
)
from agent.tools.media_gen.swarm_client import SwarmClient
from agent.tools.media_gen.ws_monitor import monitor_generation

logger = logging.getLogger("brain.agent.tools.media_gen.kestrel_tool")

# Workflow templates live alongside this module
WORKFLOWS_DIR = Path(__file__).parent / "workflows"


# ── Workflow Injection ───────────────────────────────────────────────


def load_workflow(media_type: str) -> dict:
    """
    Load a ComfyUI API-format workflow from the workflows directory.

    Args:
        media_type: Either "image" or "video".

    Returns:
        The parsed workflow dict.

    Raises:
        FileNotFoundError: If no workflow template exists for this media type.
    """
    filename = f"{media_type}_workflow.json"
    filepath = WORKFLOWS_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(
            f"No workflow template found at {filepath}. "
            f"Please add a ComfyUI API-format JSON file named '{filename}' "
            f"to {WORKFLOWS_DIR}/"
        )
    with open(filepath) as f:
        return json.load(f)


def inject_prompt(workflow: dict, prompt_text: str, negative_prompt: str = "") -> dict:
    """
    Dynamically locate CLIPTextEncode nodes and inject the user's prompt.

    This traverses the entire workflow dict, finds nodes with
    class_type == "CLIPTextEncode", and replaces their text input.
    It does NOT hardcode node IDs — it discovers them dynamically.

    For workflows with multiple CLIPTextEncode nodes:
      - The first node found with non-empty text (or the first overall) gets
        the positive prompt.
      - If a negative_prompt is provided and a second CLIPTextEncode node
        exists, it receives the negative prompt.

    Args:
        workflow: The ComfyUI workflow dict (modified in-place and returned).
        prompt_text: The user's positive prompt.
        negative_prompt: Optional negative prompt.

    Returns:
        The modified workflow dict.

    Raises:
        ValueError: If no CLIPTextEncode node is found in the workflow.
    """
    clip_nodes = []

    for node_id, node_data in workflow.items():
        if not isinstance(node_data, dict):
            continue
        class_type = node_data.get("class_type", "")
        if class_type == "CLIPTextEncode":
            clip_nodes.append((node_id, node_data))

    if not clip_nodes:
        raise ValueError(
            "No CLIPTextEncode node found in the workflow. "
            "Ensure the workflow JSON is in ComfyUI API format."
        )

    # Heuristic: identify positive vs negative prompt nodes.
    # Many workflows name the negative node's title with "negative" or
    # its default text contains "bad", "ugly", etc.
    positive_node = None
    negative_node = None

    for node_id, node_data in clip_nodes:
        meta = node_data.get("_meta", {})
        title = meta.get("title", "").lower()
        current_text = node_data.get("inputs", {}).get("text", "").lower()

        if any(kw in title for kw in ("negative", "neg")):
            negative_node = (node_id, node_data)
        elif any(kw in current_text for kw in ("bad", "ugly", "worst", "blurry", "nsfw")):
            negative_node = (node_id, node_data)
        else:
            if positive_node is None:
                positive_node = (node_id, node_data)

    # Fallback: first node is positive
    if positive_node is None:
        positive_node = clip_nodes[0]
        if len(clip_nodes) > 1 and negative_node is None:
            negative_node = clip_nodes[1]

    # Inject positive prompt
    pos_id, pos_data = positive_node
    pos_data.setdefault("inputs", {})["text"] = prompt_text
    logger.info(f"Injected positive prompt into node {pos_id}")

    # Inject negative prompt if applicable
    if negative_prompt and negative_node:
        neg_id, neg_data = negative_node
        neg_data.setdefault("inputs", {})["text"] = negative_prompt
        logger.info(f"Injected negative prompt into node {neg_id}")

    return workflow


def _extract_output_filenames(history: dict) -> list[dict]:
    """
    Extract output filenames from ComfyUI job history.

    Returns:
        List of dicts with keys: filename, subfolder, type.
    """
    outputs = []
    for node_id, node_output in history.get("outputs", {}).items():
        for key in ("images", "videos", "gifs"):
            for item in node_output.get(key, []):
                if "filename" in item:
                    outputs.append({
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    })
    return outputs


# ── Main Entry Point ─────────────────────────────────────────────────


async def generate_media(
    prompt: str,
    media_type: str = "image",
    negative_prompt: str = "",
    send_telegram: bool = False,
    workflow_override: dict = None,
) -> dict:
    """
    Generate an image or video via the remote SwarmUI host.

    This is the primary function called by the Kestrel Agent tool system.

    Args:
        prompt: The user's text prompt describing what to generate.
        media_type: "image" or "video".
        negative_prompt: Optional negative prompt for quality control.
        send_telegram: If True, also deliver the result via Telegram.
        workflow_override: Optional custom workflow dict (skips template loading).

    Returns:
        A dict with:
          - success (bool)
          - file_path (str): Local path to the downloaded file
          - filename (str): The original filename
          - prompt_id (str): The ComfyUI job ID
          - progress_log (list): Summary of progress events
          - error (str): Error message if failed
          - telegram_sent (bool): Whether Telegram delivery succeeded
    """
    if not prompt:
        return {"success": False, "error": "Prompt text is required"}

    if media_type not in ("image", "video"):
        return {"success": False, "error": f"Invalid media_type '{media_type}'. Use 'image' or 'video'."}

    timeout = VIDEO_TIMEOUT if media_type == "video" else IMAGE_TIMEOUT
    client = SwarmClient()
    progress_log = []

    try:
        # 1. Load and prepare workflow
        if workflow_override:
            workflow = workflow_override
        else:
            workflow = load_workflow(media_type)

        workflow = inject_prompt(workflow, prompt, negative_prompt)

        # 2. Submit the job
        prompt_id = await client.submit_job(workflow)
        progress_log.append(f"Job submitted: {prompt_id}")

        # 3. Monitor progress via WebSocket
        last_pct = -1
        async for event in monitor_generation(prompt_id, timeout=timeout):
            if event.is_error:
                return {
                    "success": False,
                    "error": event.error_message,
                    "prompt_id": prompt_id,
                    "progress_log": progress_log,
                }

            # Log meaningful progress milestones (every 10%)
            if event.max_steps > 0:
                pct_bucket = int(event.percentage // 10) * 10
                if pct_bucket > last_pct:
                    last_pct = pct_bucket
                    progress_log.append(event.progress_text)
            elif event.is_complete:
                progress_log.append("Generation complete")

        # 4. Fetch history to find output filenames
        history = await client.get_history(prompt_id)
        output_files = _extract_output_filenames(history)

        if not output_files:
            return {
                "success": False,
                "error": "Generation completed but no output files found in history.",
                "prompt_id": prompt_id,
                "progress_log": progress_log,
            }

        # 5. Download the primary output
        primary = output_files[0]
        media_bytes = await client.download_media(
            filename=primary["filename"],
            subfolder=primary["subfolder"],
            folder_type=primary["type"],
        )

        # 6. Save to local disk
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        local_path = os.path.join(OUTPUT_DIR, primary["filename"])
        with open(local_path, "wb") as f:
            f.write(media_bytes)

        abs_path = os.path.abspath(local_path)
        progress_log.append(f"Saved to {abs_path} ({len(media_bytes):,} bytes)")
        logger.info(f"Media saved: {abs_path}")

        result = {
            "success": True,
            "file_path": abs_path,
            "filename": primary["filename"],
            "prompt_id": prompt_id,
            "media_type": media_type,
            "file_size_bytes": len(media_bytes),
            "progress_log": progress_log,
            "telegram_sent": False,
        }

        # 7. Optional Telegram delivery
        if send_telegram:
            result["telegram_sent"] = await _send_to_telegram(
                abs_path, media_type, prompt
            )

        return result

    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}
    except ConnectionError as e:
        return {
            "success": False,
            "error": str(e),
            "hint": "Check that the Windows host is awake and SwarmUI is running.",
        }
    except Exception as e:
        logger.error(f"Media generation failed: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"Unexpected error: {e}",
            "progress_log": progress_log,
        }
    finally:
        await client.close()


# ── Telegram Delivery ────────────────────────────────────────────────


async def _send_to_telegram(file_path: str, media_type: str, caption: str) -> bool:
    """
    Send the generated media to Telegram using python-telegram-bot.

    Returns True on success, False on failure (non-fatal).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram delivery skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False

    try:
        from telegram import Bot

        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        truncated_caption = caption[:1024] if len(caption) > 1024 else caption

        with open(file_path, "rb") as f:
            if media_type == "video":
                await bot.send_video(
                    chat_id=int(TELEGRAM_CHAT_ID),
                    video=f,
                    caption=truncated_caption,
                )
            else:
                await bot.send_photo(
                    chat_id=int(TELEGRAM_CHAT_ID),
                    photo=f,
                    caption=truncated_caption,
                )

        logger.info(f"Sent {media_type} to Telegram chat {TELEGRAM_CHAT_ID}")
        return True

    except ImportError:
        logger.warning("python-telegram-bot not installed. Skipping Telegram delivery.")
        return False
    except Exception as e:
        logger.error(f"Telegram delivery failed: {e}")
        return False


# ── Utility: Check Host Status ───────────────────────────────────────


async def check_host_status() -> dict:
    """
    Quick health check for the remote SwarmUI host.

    Returns system stats and queue info, or error details if unreachable.
    """
    client = SwarmClient()
    try:
        stats = await client.get_system_stats()
        queue = await client.get_queue()
        return {
            "reachable": "error" not in stats,
            "system_stats": stats,
            "queue": queue,
        }
    finally:
        await client.close()
