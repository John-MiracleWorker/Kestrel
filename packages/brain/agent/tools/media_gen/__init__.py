"""
Media Generation Tools — remote AI image/video generation via SwarmUI (ComfyUI).

Provides Kestrel Agent with the ability to trigger, monitor, and retrieve
AI-generated images and videos from a remote Windows host running SwarmUI
with an RTX 4070 GPU on the same LAN.

Tools registered:
  - generate_media: Generate an image or video from a text prompt
  - check_media_host: Check if the SwarmUI host is reachable and get GPU stats
"""

from agent.types import RiskLevel, ToolDefinition

# ── Tool Definitions ─────────────────────────────────────────────────

GENERATE_MEDIA_TOOL = ToolDefinition(
    name="generate_media",
    description=(
        "Generate an AI image or video using the remote SwarmUI/ComfyUI host. "
        "Sends the prompt to the Windows GPU machine on the LAN, monitors "
        "generation progress in real-time, downloads the result, and returns "
        "the local file path. Supports both image (SDXL) and video (SVD) "
        "generation. Use media_type='image' for images, media_type='video' "
        "for videos. Video generation takes significantly longer (up to 15 min). "
        "Set send_telegram=true to also deliver the result to Telegram."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The text prompt describing what to generate. Be descriptive "
                    "for best results. Example: 'a cyberpunk cityscape at sunset, "
                    "neon lights reflecting on wet streets, cinematic lighting'"
                ),
            },
            "media_type": {
                "type": "string",
                "enum": ["image", "video"],
                "description": "Type of media to generate: 'image' (fast, ~30s) or 'video' (slow, up to 15 min).",
            },
            "negative_prompt": {
                "type": "string",
                "description": (
                    "Optional negative prompt to exclude unwanted features. "
                    "Example: 'blurry, low quality, distorted'"
                ),
            },
            "send_telegram": {
                "type": "boolean",
                "description": "If true, also send the generated media to the configured Telegram chat.",
            },
        },
        "required": ["prompt"],
    },
    risk_level=RiskLevel.LOW,
    timeout_seconds=960,  # 16 min ceiling (video can take 15 min)
    category="media",
)

CHECK_MEDIA_HOST_TOOL = ToolDefinition(
    name="check_media_host",
    description=(
        "Check if the remote SwarmUI/ComfyUI host is reachable and get its "
        "current status. Returns GPU info, system stats, and queue status. "
        "Use this before generating media to verify the host is awake and ready."
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
    risk_level=RiskLevel.LOW,
    timeout_seconds=15,
    category="media",
)


# ── Tool Handlers ────────────────────────────────────────────────────


async def _generate_media_handler(
    prompt: str = "",
    media_type: str = "image",
    negative_prompt: str = "",
    send_telegram: bool = False,
    **kwargs,
) -> dict:
    """Handler for the generate_media tool."""
    from agent.tools.media_gen.kestrel_tool import generate_media
    return await generate_media(
        prompt=prompt,
        media_type=media_type,
        negative_prompt=negative_prompt,
        send_telegram=send_telegram,
    )


async def _check_host_handler(**kwargs) -> dict:
    """Handler for the check_media_host tool."""
    from agent.tools.media_gen.kestrel_tool import check_host_status
    return await check_host_status()


# ── Registration ─────────────────────────────────────────────────────


def register_media_gen_tools(registry) -> None:
    """Register media generation tools in the agent tool registry."""
    registry.register(
        definition=GENERATE_MEDIA_TOOL,
        handler=_generate_media_handler,
    )
    registry.register(
        definition=CHECK_MEDIA_HOST_TOOL,
        handler=_check_host_handler,
    )
