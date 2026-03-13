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
        "Results are emitted as shared channel artifacts so Telegram, desktop, CLI, and web "
        "see the same media receipt. send_telegram is retained only for backward compatibility."
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
                "description": "Deprecated compatibility flag. Media delivery now flows through shared channel events.",
            },
        },
        "required": ["prompt"],
    },
    risk_level=RiskLevel.LOW,
    timeout_seconds=960,  # 16 min ceiling (video can take 15 min)
    category="media",
    availability_requirements=("media",),
    use_cases=("generate images", "generate videos", "render visual concepts"),
)

VRAM_GENERATE_IMAGE_TOOL = ToolDefinition(
    name="vram_generate_image",
    description=(
        "Generate an AI image or video using the local dual-GPU setup with automatic VRAM "
        "orchestration. This tool unloads the active LLM from VRAM, generates the "
        "media via SwarmUI, then reloads the LLM — preventing out-of-memory crashes. "
        "IMPORTANT: If the user asks for a video, animation, clip, or any moving/animated content, "
        "you MUST set media_type='video'. Default is 'image' for still pictures. "
        "Results are emitted as shared channel artifacts so Telegram and companion surfaces stay in sync. "
        "send_telegram is retained only for backward compatibility."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The text prompt describing what to generate. Be descriptive "
                    "for best results."
                ),
            },
            "negative_prompt": {
                "type": "string",
                "description": "Optional negative prompt to exclude unwanted features.",
            },
            "steps": {
                "type": "integer",
                "description": "Diffusion sampling steps (default: 20).",
            },
            "width": {
                "type": "integer",
                "description": "Output width in pixels (default: 1024).",
            },
            "height": {
                "type": "integer",
                "description": "Output height in pixels (default: 1024).",
            },
            "send_telegram": {
                "type": "boolean",
                "description": "Deprecated compatibility flag. Media delivery now flows through shared channel events.",
            },
            "media_type": {
                "type": "string",
                "enum": ["image", "video"],
                "description": "Type of media to generate. Use 'video' for animated content (default: 'image').",
            },
            "video_frames": {
                "type": "integer",
                "description": "Number of video frames to generate (default: 25). Only used when media_type='video'.",
            },
        },
        "required": ["prompt"],
    },
    risk_level=RiskLevel.LOW,
    timeout_seconds=960,  # 16 min ceiling (video can take 15 min)
    category="media",
    availability_requirements=("media",),
    use_cases=("generate images", "generate videos", "use local VRAM orchestration"),
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
    use_cases=("check media host health", "inspect GPU queue status"),
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


async def _vram_generate_image_handler(
    prompt: str = "",
    negative_prompt: str = "",
    steps: int = 30,
    width: int = 1024,
    height: int = 1024,
    send_telegram: bool = False,
    media_type: str = "image",
    video_frames: int = 25,
    **kwargs,
) -> dict:
    """Handler for the vram_generate_image tool."""
    import asyncio
    from agent.tools.media_gen.vram_orchestrator import generate_image

    # generate_image is synchronous — run in executor to avoid blocking
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: generate_image(
            prompt=prompt,
            negative_prompt=negative_prompt,
            steps=steps,
            width=width,
            height=height,
            media_type=media_type,
            video_frames=video_frames,
        ),
    )

    # Build markdown for display
    if result.get("image_urls"):
        is_video = media_type == "video"
        md_items = []
        for url in result["image_urls"]:
            # Video files use the same ![alt](url) syntax — the frontend
            # detects video extensions and renders <video> tags
            label = "Generated video" if is_video else "Generated image"
            md_items.append(f"![{label}]({url})")
        result["display_markdown"] = "\n".join(md_items)
        result["instruction"] = (
            "Include the display_markdown content in your response "
            "so the user can see the generated media inline."
        )

    if send_telegram:
        result["delivery"] = {
            "mode": "channel_event",
            "requested": True,
            "target": "telegram",
            "note": "Direct Brain-to-Telegram delivery is disabled; Gateway delivers shared media artifacts.",
        }

    return result


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
        definition=VRAM_GENERATE_IMAGE_TOOL,
        handler=_vram_generate_image_handler,
    )
    registry.register(
        definition=CHECK_MEDIA_HOST_TOOL,
        handler=_check_host_handler,
    )
