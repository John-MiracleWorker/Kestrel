from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import requests

from .vram_orchestrator_config import (
    IMAGE_GEN_TIMEOUT,
    OUTPUT_DIR,
    SWARMUI_BASE_URL,
    SWARM_DEFAULT_MODEL,
    SWARM_DEFAULT_VIDEO_MODEL,
    VIDEO_GEN_TIMEOUT,
    logger,
)

def generate_image_swarmui(
    prompt: str,
    negative_prompt: str = "",
    images: int = 1,
    steps: int = 30,
    width: int = 1024,
    height: int = 1024,
    cfg_scale: float = 1.0,
    seed: int = -1,
    model: str = "",
    base_url: str = None,
    # Video generation params
    video_model: str = "",
    video_frames: int = 0,
    video_steps: int = 20,
    video_cfg: float = 3.0,
    video_fps: int = 24,
    video_format: str = "mp4",
) -> dict:
    """
    Send an image generation request to SwarmUI's Text2Image endpoint.

    Args:
        prompt: The positive text prompt.
        negative_prompt: Things to avoid in the image.
        images: Number of images to generate.
        steps: Diffusion sampling steps.
        width: Output width in pixels.
        height: Output height in pixels.
        cfg_scale: Classifier-free guidance scale.
        seed: RNG seed (-1 for random).
        model: Override the SwarmUI model. Empty string uses SwarmUI's default.
        base_url: SwarmUI base URL. Defaults to SWARMUI_BASE_URL.

    Returns:
        dict with keys: success (bool), images (list[str]), detail (str).
        On success, images contains base64-encoded strings or file paths
        depending on SwarmUI configuration.
    """
    base_url = (base_url or SWARMUI_BASE_URL).rstrip("/")

    # ── Get a SwarmUI session (required by all API calls) ────────────
    try:
        sess_resp = requests.post(
            f"{base_url}/API/GetNewSession", json={}, timeout=15
        )
        if sess_resp.status_code != 200:
            detail = f"SwarmUI GetNewSession returned {sess_resp.status_code}"
            logger.error(detail)
            return {"success": False, "images": [], "detail": detail}
        session_id = sess_resp.json().get("session_id", "")
        if not session_id:
            detail = "SwarmUI GetNewSession returned no session_id"
            logger.error(detail)
            return {"success": False, "images": [], "detail": detail}
        logger.info(f"SwarmUI session acquired: {session_id[:12]}...")
    except requests.RequestException as exc:
        detail = f"SwarmUI session request failed: {exc}"
        logger.error(detail)
        return {"success": False, "images": [], "detail": detail}

    # ── Build and send the generation request ────────────────────────
    url = f"{base_url}/API/GenerateText2Image"
    payload = {
        "session_id": session_id,
        "prompt": prompt,
        "negativeprompt": negative_prompt,
        "images": images,
        "steps": steps,
        "width": width,
        "height": height,
        "cfgscale": cfg_scale,
        "seed": seed,
    }
    # Always include a model — SwarmUI requires it
    payload["model"] = model or SWARM_DEFAULT_MODEL

    # Add video generation params if a video model is specified
    is_video = bool(video_model or video_frames > 0)
    if is_video:
        payload["videomodel"] = video_model or SWARM_DEFAULT_VIDEO_MODEL
        payload["videoframes"] = video_frames or 25
        payload["videosteps"] = video_steps
        payload["videocfg"] = video_cfg
        payload["videofps"] = video_fps
        payload["videoformat"] = video_format

    media_type = "video" if is_video else "image"
    logger.info(
        f"Sending {media_type} generation request to SwarmUI: "
        f"prompt={prompt[:80]!r}  steps={steps}  size={width}x{height}"
        + (f"  video_frames={payload.get('videoframes')}  video_model={payload.get('videomodel', '')[:40]}" if is_video else "")
    )

    timeout = VIDEO_GEN_TIMEOUT if is_video else IMAGE_GEN_TIMEOUT
    try:
        resp = requests.post(url, json=payload, timeout=timeout)

        if resp.status_code != 200:
            detail = f"SwarmUI returned {resp.status_code}: {resp.text[:500]}"
            logger.error(detail)
            return {"success": False, "images": [], "detail": detail}

        data = resp.json()

        if "error" in data and data["error"]:
            detail = f"SwarmUI API error: {data['error']}"
            logger.error(detail)
            return {"success": False, "images": [], "detail": detail}

        # SwarmUI returns relative URL paths in the "images" field
        result_images = data.get("images", [])
        if not result_images:
            detail = f"SwarmUI returned 200 but no images in response: {list(data.keys())}"
            logger.warning(detail)
            return {"success": False, "images": [], "detail": detail}

        logger.info(f"SwarmUI returned {len(result_images)} image(s).")
        return {
            "success": True,
            "images": result_images,
            "base_url": base_url,
            "detail": f"Generated {len(result_images)} image(s)",
        }

    except requests.ConnectionError:
        detail = (
            f"Cannot reach SwarmUI at {base_url}. "
            "Is SwarmUI running?"
        )
        logger.error(detail)
        return {"success": False, "images": [], "detail": detail}
    except requests.Timeout:
        detail = f"SwarmUI request timed out after {timeout}s."
        logger.error(detail)
        return {"success": False, "images": [], "detail": detail}
    except requests.RequestException as exc:
        detail = f"SwarmUI request failed: {exc}"
        logger.error(detail)
        return {"success": False, "images": [], "detail": detail}


def _save_images(images_data: list, prompt: str, base_url: str = None) -> list[dict]:
    """
    Save images to disk. Handles both:
      - URL paths from SwarmUI (e.g. 'View/local/raw/...')
      - base64-encoded strings

    Args:
        images_data: List of image references (URL paths or base64 strings).
        prompt: The original prompt (used to build a readable filename).
        base_url: SwarmUI base URL (needed to download URL-path images).

    Returns:
        List of dicts with keys: path (abs file path), url (gateway-relative URL).
    """
    import base64

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Build a filesystem-safe prefix from the prompt
    safe_prefix = "".join(c if c.isalnum() or c in " _-" else "" for c in prompt[:50]).strip()
    safe_prefix = safe_prefix.replace(" ", "_") or "image"
    timestamp = int(time.time())

    saved = []
    for idx, img_ref in enumerate(images_data):
        img_bytes = None

        # Case 1: URL path from SwarmUI (e.g. "View/local/raw/...")
        if img_ref and not img_ref.startswith("data:") and "/" in img_ref and base_url:
            url = img_ref if img_ref.startswith("http") else f"{base_url}/{img_ref}"
            try:
                logger.info(f"Downloading image from SwarmUI: {url[:120]}")
                dl = requests.get(url, timeout=30)
                if dl.status_code == 200 and len(dl.content) > 100:
                    img_bytes = dl.content
                else:
                    logger.error(f"Image download failed: status={dl.status_code} size={len(dl.content)}")
            except requests.RequestException as exc:
                logger.error(f"Image download failed: {exc}")

        # Case 2: base64-encoded (data URI or raw)
        if img_bytes is None and img_ref:
            if "," in img_ref and img_ref.startswith("data:"):
                img_ref = img_ref.split(",", 1)[1]
            try:
                img_bytes = base64.b64decode(img_ref)
            except Exception as exc:
                logger.error(f"Failed to decode image {idx}: {exc}")
                continue

        if img_bytes is None:
            logger.error(f"No image data for index {idx}")
            continue

        suffix = idx if len(images_data) > 1 else ""
        # Detect file type from content or URL
        ext = ".png"
        if isinstance(img_ref, str):
            lower_ref = img_ref.lower()
            if any(lower_ref.endswith(ve) for ve in (".mp4", ".webm", ".mov", ".gif")):
                ext = os.path.splitext(lower_ref)[1]
            elif lower_ref.endswith(".webp"):
                ext = ".webp"
        # Also check binary magic bytes for video
        if img_bytes[:4] in (b'\x00\x00\x00\x1c', b'\x00\x00\x00\x18', b'\x00\x00\x00\x20'):
            ext = ".mp4"  # ftyp box
        elif img_bytes[:4] == b'\x1a\x45\xdf\xa3':
            ext = ".webm"  # EBML header

        filename = f"{safe_prefix}_{timestamp}{f'_{suffix}' if suffix != '' else ''}{ext}"
        filepath = os.path.join(OUTPUT_DIR, filename)

        with open(filepath, "wb") as f:
            f.write(img_bytes)

        abs_path = os.path.abspath(filepath)
        media_url = f"/media/{filename}"
        saved.append({"path": abs_path, "url": media_url})
        media_label = "video" if ext in (".mp4", ".webm", ".mov") else "image"
        logger.info(f"Saved {media_label}: {abs_path} ({len(img_bytes):,} bytes) → {media_url}")

    return saved


# ── Telegram Delivery ────────────────────────────────────────────────────────


def _infer_media_type(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    return "video" if ext in {".mp4", ".webm", ".mov"} else "image"


async def _send_to_telegram(
    file_path: str,
    caption: str,
    media_type: Optional[str] = None,
) -> bool:
    """
    Legacy compatibility shim.

    Direct Brain-to-Telegram delivery is disabled so Gateway remains the
    single owner of channel delivery.
    """
    resolved_media_type = (media_type or _infer_media_type(file_path)).strip().lower()
    logger.info(
        "Skipping legacy direct Telegram delivery for %s (%s); Gateway will deliver channel artifacts instead.",
        file_path,
        resolved_media_type,
    )
    return False
