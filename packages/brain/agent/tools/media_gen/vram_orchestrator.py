"""
VRAM Orchestrator — dynamic VRAM juggling between LLM and image generation.

On a dual-GPU setup (RTX 4070 12GB + RTX 3060 12GB = 24GB total), the GLM 4.7
Flash 30B MoE model consumes ~19GB VRAM, leaving no room for SwarmUI/ComfyUI to
run concurrently. This module acts as a VRAM traffic cop:

  1. Unload the LLM from VRAM via LM Studio's REST API
  2. Verify VRAM is freed
  3. Dispatch the image generation request to SwarmUI
  4. Wait for the image to complete
  5. Reload the LLM so Kestrel can resume chatting

Targets:
  - LM Studio:  http://localhost:1234
  - SwarmUI:    http://localhost:7801

Dependencies: requests (stdlib: time, json, logging, os, pathlib)
Platform: Windows + CUDA only.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

from agent.tools.media_gen.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("kestrel.vram_orchestrator")

# ── Configuration ────────────────────────────────────────────────────────────

# LM Studio endpoint
LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234")

# SwarmUI endpoint
SWARMUI_BASE_URL = os.getenv("SWARMUI_BASE_URL", "http://localhost:7801")

# Default SwarmUI diffusion model — used when the LLM doesn't specify one
SWARM_DEFAULT_MODEL = os.getenv(
    "KESTREL_SWARM_MODEL",
    "Flux/flux1-dev-fp8.safetensors",
)

# Default SwarmUI video model — used for video generation
SWARM_DEFAULT_VIDEO_MODEL = os.getenv(
    "KESTREL_SWARM_VIDEO_MODEL",
    "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors",
)

# Drop in your exact LM Studio model identifier here, e.g.:
#   "glm-4-9b-chat-q4_k_m"  or  "THUDM/glm-4-flash-30b-gguf"
GLM_MODEL_ID = os.getenv(
    "KESTREL_GLM_MODEL_ID",
    "zai-org/glm-4.7-flash",
)

# Seconds to wait after unload before hitting SwarmUI.
# Windows needs time to actually release 19GB from the GPU driver.
VRAM_FLUSH_DELAY = float(os.getenv("KESTREL_VRAM_FLUSH_DELAY", "6.0"))

# How long to wait for a single image generation (seconds).
IMAGE_GEN_TIMEOUT = int(os.getenv("KESTREL_IMAGE_GEN_TIMEOUT", "300"))

# How long to wait for video generation (seconds) — much longer than images.
VIDEO_GEN_TIMEOUT = int(os.getenv("KESTREL_VIDEO_GEN_TIMEOUT", "900"))

# Directory to save generated images (shared volume with gateway for serving)
OUTPUT_DIR = os.getenv("KESTREL_IMAGE_OUTPUT_DIR", "/app/generated_media")

# HTTP timeouts for API calls (seconds)
API_TIMEOUT = 30


# ── LM Studio: Model Lifecycle ──────────────────────────────────────────────


def _get_all_loaded_instance_ids(
    model_id: str,
    base_url: str,
) -> list[str]:
    """
    Query LM Studio's /v1/models endpoint to find ALL instance_ids for a
    model. LM Studio appends ':N' suffixes for duplicate loads (e.g.
    'zai-org/glm-4.7-flash:2'), so we match by prefix.

    Returns a list of instance_id strings (may be empty).
    """
    instance_ids = []
    try:
        url = f"{base_url}/v1/models"
        resp = requests.get(url, timeout=API_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            for model in data.get("data", []):
                mid = model.get("id", "")
                # Match exact ID or ID with ':N' suffix
                if mid == model_id or mid.startswith(f"{model_id}:"):
                    iid = model.get("instance_id") or mid
                    instance_ids.append(iid)
            if instance_ids:
                logger.info(f"Found {len(instance_ids)} instance(s) for {model_id}: {instance_ids}")
            else:
                logger.warning(f"Model {model_id} not found in loaded models list")
    except requests.RequestException as exc:
        logger.warning(f"Failed to query loaded models: {exc}")
    return instance_ids


def unload_llm(
    model_id: str = None,
    base_url: str = None,
) -> dict:
    """
    Unload ALL instances of the GLM model from VRAM via LM Studio's REST API.

    First queries /v1/models to discover all instance_ids (LM Studio may
    have multiple instances loaded after failed reload attempts). Unloads
    each one via POST /api/v1/models/unload.

    Args:
        model_id: The LM Studio model identifier. Defaults to GLM_MODEL_ID.
        base_url: LM Studio base URL. Defaults to LMSTUDIO_BASE_URL.

    Returns:
        dict with keys: success (bool), method (str), detail (str).
    """
    model_id = model_id or GLM_MODEL_ID
    base_url = (base_url or LMSTUDIO_BASE_URL).rstrip("/")

    if model_id == "YOUR_GLM_MODEL_ID_HERE":
        return {
            "success": False,
            "method": "none",
            "detail": (
                "GLM_MODEL_ID is still the placeholder value. "
                "Set KESTREL_GLM_MODEL_ID or update GLM_MODEL_ID in vram_orchestrator.py."
            ),
        }

    # ── Discover all instance_ids from loaded models ────────────────
    instance_ids = _get_all_loaded_instance_ids(model_id, base_url)

    # ── Attempt 1: POST /api/v1/models/unload for each instance ─────
    if instance_ids:
        all_ok = True
        for iid in instance_ids:
            try:
                url = f"{base_url}/api/v1/models/unload"
                payload = {"instance_id": iid}
                logger.info(f"Unloading instance via POST {url}  instance_id={iid}")

                resp = requests.post(url, json=payload, timeout=60)

                if resp.status_code in (200, 204):
                    logger.info(f"Instance {iid} unloaded successfully")
                else:
                    logger.warning(
                        f"POST /api/v1/models/unload for {iid} returned "
                        f"{resp.status_code}: {resp.text}"
                    )
                    all_ok = False
            except requests.ConnectionError:
                logger.error(f"Cannot reach LM Studio at {base_url}. Is it running?")
                return {
                    "success": False,
                    "method": "post_unload",
                    "detail": f"Connection refused: {base_url}",
                }
            except requests.RequestException as exc:
                logger.warning(f"POST /api/v1/models/unload for {iid} failed: {exc}")
                all_ok = False

        if all_ok:
            return {
                "success": True,
                "method": "post_unload",
                "detail": f"Unloaded {len(instance_ids)} instance(s)",
            }

    # ── Attempt 2: DELETE /v1/models/{model_id} ──────────────────────
    try:
        url = f"{base_url}/v1/models/{model_id}"
        logger.info(f"Unloading LLM via DELETE {url}")

        resp = requests.delete(url, timeout=60)

        if resp.status_code in (200, 204):
            logger.info("LLM unloaded successfully (DELETE /v1/models/)")
            return {"success": True, "method": "delete_model", "detail": resp.text}

        detail = f"DELETE /v1/models/ returned {resp.status_code}: {resp.text}"
        logger.error(detail)
        return {"success": False, "method": "delete_model", "detail": detail}

    except requests.RequestException as exc:
        detail = f"DELETE /v1/models/ failed: {exc}"
        logger.error(detail)
        return {"success": False, "method": "delete_model", "detail": detail}


def load_llm(
    model_id: str = None,
    base_url: str = None,
) -> dict:
    """
    Reload the GLM model into VRAM via LM Studio's REST API.

    Tries POST /api/v1/models/load first, then falls back to
    POST /v1/models with a body payload.

    Args:
        model_id: The LM Studio model identifier. Defaults to GLM_MODEL_ID.
        base_url: LM Studio base URL. Defaults to LMSTUDIO_BASE_URL.

    Returns:
        dict with keys: success (bool), method (str), detail (str).
    """
    model_id = model_id or GLM_MODEL_ID
    base_url = (base_url or LMSTUDIO_BASE_URL).rstrip("/")

    # ── Attempt 1: POST /api/v1/models/load ──────────────────────────
    try:
        url = f"{base_url}/api/v1/models/load"
        payload = {"model": model_id}
        logger.info(f"Loading LLM via POST {url}  model={model_id}")

        resp = requests.post(url, json=payload, timeout=120)

        if resp.status_code in (200, 204):
            logger.info("LLM loaded successfully (POST /api/v1/models/load)")
            return {"success": True, "method": "post_load", "detail": resp.text}

        logger.warning(
            f"POST /api/v1/models/load returned {resp.status_code}: {resp.text}. "
            "Trying fallback..."
        )
    except requests.ConnectionError:
        logger.error(f"Cannot reach LM Studio at {base_url}. Is it running?")
        return {
            "success": False,
            "method": "post_load",
            "detail": f"Connection refused: {base_url}",
        }
    except requests.RequestException as exc:
        logger.warning(f"POST /api/v1/models/load failed: {exc}. Trying fallback...")

    # ── Attempt 2: POST /v1/models ───────────────────────────────────
    try:
        url = f"{base_url}/v1/models"
        payload = {"model": model_id}
        logger.info(f"Loading LLM via POST {url}")

        resp = requests.post(url, json=payload, timeout=120)

        if resp.status_code in (200, 204):
            logger.info("LLM loaded successfully (POST /v1/models)")
            return {"success": True, "method": "post_models", "detail": resp.text}

        detail = f"POST /v1/models returned {resp.status_code}: {resp.text}"
        logger.error(detail)
        return {"success": False, "method": "post_models", "detail": detail}

    except requests.RequestException as exc:
        detail = f"POST /v1/models failed: {exc}"
        logger.error(detail)
        return {"success": False, "method": "post_models", "detail": detail}


def verify_vram_cleared(
    base_url: str = None,
    max_retries: int = 10,
    retry_delay: float = 4.0,
) -> bool:
    """
    Verify that no models are loaded in LM Studio (VRAM is free).

    Polls GET /v1/models and checks that the returned list is empty.
    Uses generous retries since an 18GB model can take 30+ seconds to
    fully release from GPU memory.

    Args:
        base_url: LM Studio base URL.
        max_retries: How many times to poll before giving up.
        retry_delay: Seconds between polls.

    Returns:
        True if VRAM appears cleared, False otherwise.
    """
    base_url = (base_url or LMSTUDIO_BASE_URL).rstrip("/")
    url = f"{base_url}/v1/models"

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=API_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                models = data.get("data", [])
                if not models:
                    logger.info("VRAM verification passed: no models loaded.")
                    return True
                loaded = [m.get("id", "unknown") for m in models]
                logger.warning(
                    f"VRAM check attempt {attempt}/{max_retries}: "
                    f"models still loaded: {loaded}"
                )
            else:
                logger.warning(
                    f"VRAM check attempt {attempt}/{max_retries}: "
                    f"GET /v1/models returned {resp.status_code}"
                )
        except requests.RequestException as exc:
            logger.warning(
                f"VRAM check attempt {attempt}/{max_retries}: request failed: {exc}"
            )

        if attempt < max_retries:
            time.sleep(retry_delay)

    logger.error("VRAM verification FAILED: model still loaded after all retries.")
    return False


# ── SwarmUI: Image Generation ────────────────────────────────────────────────


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


async def _send_to_telegram(file_path: str, caption: str) -> bool:
    """
    Send a generated image to Telegram via python-telegram-bot.

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
            await bot.send_photo(
                chat_id=int(TELEGRAM_CHAT_ID),
                photo=f,
                caption=truncated_caption,
            )

        logger.info(f"Sent image to Telegram chat {TELEGRAM_CHAT_ID}")
        return True

    except ImportError:
        logger.warning("python-telegram-bot not installed. Skipping Telegram delivery.")
        return False
    except Exception as e:
        logger.error(f"Telegram delivery failed: {e}")
        return False


# ── Main Orchestrator ────────────────────────────────────────────────────────


def generate_image(
    prompt: str,
    negative_prompt: str = "",
    images: int = 1,
    steps: int = 30,
    width: int = 1024,
    height: int = 1024,
    cfg_scale: float = 1.0,
    seed: int = -1,
    swarm_model: str = "",
    send_telegram: bool = False,
    # Video generation params
    media_type: str = "image",
    video_frames: int = 25,
    video_steps: int = 20,
    video_fps: int = 24,
) -> dict:
    """
    Full VRAM orchestration pipeline: unload LLM -> generate image -> reload LLM.

    This is the primary entry point for Kestrel. When the agent decides to
    generate an image, call this function. It will:

      1. Unload GLM 4.7 from VRAM via LM Studio
      2. Verify the VRAM is actually freed
      3. Wait for the Windows GPU driver to flush memory
      4. Send the prompt to SwarmUI for image generation
      5. Save the resulting image(s) to disk
      6. Reload GLM 4.7 so Kestrel can resume chatting

    If the LLM fails to unload, image generation is ABORTED to prevent an
    out-of-memory crash.

    Args:
        prompt: Text description of the image to generate.
        negative_prompt: Things to exclude from the image.
        images: Number of images to generate.
        steps: Diffusion sampling steps.
        width: Output width in pixels.
        height: Output height in pixels.
        cfg_scale: Classifier-free guidance scale.
        seed: RNG seed (-1 for random).
        swarm_model: Override the SwarmUI model (empty = SwarmUI default).
        send_telegram: If True, deliver the generated image(s) to Telegram.

    Returns:
        dict with keys:
          - success (bool)
          - file_paths (list[str]): Saved image paths on disk
          - llm_unloaded (bool): Whether LLM was successfully unloaded
          - llm_reloaded (bool): Whether LLM was successfully reloaded
          - image_result (dict): Raw SwarmUI response details
          - telegram_sent (bool): Whether Telegram delivery succeeded
          - error (str): Error message if failed
          - phase (str): Which phase failed (unload / verify / generate / save / reload)
    """
    result = {
        "success": False,
        "file_paths": [],
        "llm_unloaded": False,
        "llm_reloaded": False,
        "image_result": {},
        "telegram_sent": False,
        "error": "",
        "phase": "",
    }

    # ── Phase 1: Unload LLM ──────────────────────────────────────────
    logger.info("=" * 60)
    is_video = media_type == "video"
    pipeline_type = "video" if is_video else "image"
    logger.info(f"VRAM ORCHESTRATOR: Starting {pipeline_type} generation pipeline")
    logger.info(f"  Prompt: {prompt[:100]!r}")
    if is_video:
        logger.info(f"  Video: frames={video_frames}  steps={video_steps}  fps={video_fps}")
    logger.info("=" * 60)

    logger.info("Phase 1/6: Unloading LLM from VRAM...")
    unload_result = unload_llm()
    if not unload_result["success"]:
        result["error"] = (
            f"ABORTING image generation — LLM failed to unload. "
            f"Reason: {unload_result['detail']}. "
            f"Generating without freeing VRAM would cause an OOM crash."
        )
        result["phase"] = "unload"
        logger.error(result["error"])
        return result

    result["llm_unloaded"] = True
    logger.info("Phase 1/6: LLM unload command accepted.")

    # ── Phase 2: Verify VRAM cleared ─────────────────────────────────
    logger.info("Phase 2/6: Verifying VRAM is cleared...")
    if not verify_vram_cleared():
        # LLM is supposedly unloaded but verification failed.
        # Attempt to reload the LLM and abort safely.
        logger.error("VRAM verification failed. Attempting to reload LLM and abort.")
        reload_result = load_llm()
        result["llm_reloaded"] = reload_result["success"]
        result["error"] = (
            "ABORTING image generation — VRAM verification failed. "
            "The model may still be partially loaded. "
            "LLM reload attempted to restore safe state."
        )
        result["phase"] = "verify"
        logger.error(result["error"])
        return result

    # ── Phase 3: Flush delay ─────────────────────────────────────────
    logger.info(
        f"Phase 3/6: Waiting {VRAM_FLUSH_DELAY}s for Windows GPU driver "
        f"to release VRAM..."
    )
    time.sleep(VRAM_FLUSH_DELAY)

    # ── Phase 4: Generate image via SwarmUI ──────────────────────────
    logger.info("Phase 4/6: Sending generation request to SwarmUI...")
    try:
        gen_kwargs = dict(
            prompt=prompt,
            negative_prompt=negative_prompt,
            images=images,
            steps=steps,
            width=width,
            height=height,
            cfg_scale=cfg_scale,
            seed=seed,
            model=swarm_model,
        )
        if is_video:
            # Wan 2.2 14B is too heavy at 1024x1024 for RTX 4070.
            # Override to 480p (848x480) which is the model's native resolution.
            gen_kwargs["width"] = 848
            gen_kwargs["height"] = 480
            gen_kwargs["steps"] = video_steps  # Use video-specific steps, not image steps
            gen_kwargs.update(
                video_model=SWARM_DEFAULT_VIDEO_MODEL,
                video_frames=video_frames,
                video_steps=video_steps,
                video_fps=video_fps,
            )
        img_result = generate_image_swarmui(**gen_kwargs)
        result["image_result"] = img_result

        if img_result["success"]:
            # Save images to disk
            saved = _save_images(img_result["images"], prompt, base_url=img_result.get("base_url"))
            result["file_paths"] = [s["path"] for s in saved]
            result["image_urls"] = [s["url"] for s in saved]
            if saved:
                result["success"] = True
                logger.info(f"Phase 4/6: Image generation complete. Saved {len(saved)} file(s).")
            else:
                result["error"] = "SwarmUI returned images but all failed to decode/save."
                result["phase"] = "save"
                logger.error(result["error"])
        else:
            result["error"] = f"Image generation failed: {img_result['detail']}"
            result["phase"] = "generate"
            logger.error(result["error"])

    except Exception as exc:
        result["error"] = f"Unexpected error during image generation: {exc}"
        result["phase"] = "generate"
        logger.error(result["error"], exc_info=True)

    # NOTE: Telegram delivery (when send_telegram=True) is handled by the
    # async tool handler in __init__.py, which can properly await the Bot API.
    # The result dict includes a "telegram_sent" field for the handler to set.

    # ── Phase 5: Free diffusion model from VRAM ──────────────────────
    logger.info("Phase 5/6: Freeing diffusion model from VRAM...")
    swarm_base = (SWARMUI_BASE_URL or "").rstrip("/")
    try:
        # Loop FreeBackendMemory until SwarmUI reports 0 backends freed.
        # SwarmUI can take multiple calls to fully release all cached models.
        total_freed = 0
        for attempt in range(5):
            sess_resp = requests.post(
                f"{swarm_base}/API/GetNewSession", json={}, timeout=10
            )
            if sess_resp.status_code != 200:
                logger.warning(
                    f"Phase 5/6: Could not get SwarmUI session (attempt {attempt+1}): "
                    f"{sess_resp.status_code}"
                )
                break

            free_sid = sess_resp.json().get("session_id", "")
            free_resp = requests.post(
                f"{swarm_base}/API/FreeBackendMemory",
                json={"session_id": free_sid},
                timeout=30,
            )
            if free_resp.status_code == 200:
                count = free_resp.json().get("count", 0)
                total_freed += count
                logger.info(
                    f"Phase 5/6: Attempt {attempt+1} freed {count} backend(s) "
                    f"(total freed: {total_freed})"
                )
                if count == 0:
                    break  # Nothing left to free
                time.sleep(4)  # Give GPU time to release memory
            else:
                logger.warning(
                    f"Phase 5/6: FreeBackendMemory returned "
                    f"{free_resp.status_code}: {free_resp.text[:200]}"
                )
                break

        # Extra wait after all frees for the GPU driver to fully release VRAM
        if total_freed > 0:
            logger.info("Phase 5/6: Waiting 8s for GPU VRAM to fully clear...")
            time.sleep(8)

        logger.info(f"Phase 5/6: Complete. Total backends freed: {total_freed}")
    except requests.RequestException as exc:
        logger.warning(f"Phase 5/6: Failed to free backend memory: {exc}")

    # ── Phase 6: Reload LLM (always attempted) ───────────────────────
    logger.info("Phase 6/6: Reloading LLM into VRAM...")
    reload_result = load_llm()
    result["llm_reloaded"] = reload_result["success"]

    if not reload_result["success"]:
        reload_warning = (
            f"WARNING: LLM failed to reload! Reason: {reload_result['detail']}. "
            f"Kestrel will not be able to chat until the model is manually loaded "
            f"in LM Studio."
        )
        logger.error(reload_warning)
        # Append to error without overwriting generation result
        if result["error"]:
            result["error"] += f" | {reload_warning}"
        else:
            result["error"] = reload_warning
    else:
        logger.info("Phase 6/6: LLM reloaded successfully. Kestrel is ready to chat.")

    logger.info("=" * 60)
    logger.info(
        f"VRAM ORCHESTRATOR: Pipeline complete. "
        f"success={result['success']}  files={len(result['file_paths'])}"
    )
    logger.info("=" * 60)

    return result


# ── Convenience: Quick Status Check ──────────────────────────────────────────


def get_vram_status() -> dict:
    """
    Quick status check: which models are loaded in LM Studio and whether
    SwarmUI is reachable.

    Returns:
        dict with loaded_models (list), lmstudio_reachable (bool),
        swarmui_reachable (bool).
    """
    status = {
        "loaded_models": [],
        "lmstudio_reachable": False,
        "swarmui_reachable": False,
    }

    # Check LM Studio
    try:
        resp = requests.get(
            f"{LMSTUDIO_BASE_URL.rstrip('/')}/v1/models",
            timeout=5,
        )
        if resp.status_code == 200:
            status["lmstudio_reachable"] = True
            data = resp.json()
            status["loaded_models"] = [
                m.get("id", "unknown") for m in data.get("data", [])
            ]
    except requests.RequestException:
        pass

    # Check SwarmUI
    try:
        resp = requests.get(
            f"{SWARMUI_BASE_URL.rstrip('/')}/API/GetCurrentStatus",
            timeout=5,
        )
        status["swarmui_reachable"] = resp.status_code == 200
    except requests.RequestException:
        pass

    return status


# ── CLI Entry Point (for standalone testing) ─────────────────────────────────


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage: python vram_orchestrator.py <prompt>")
        print('Example: python vram_orchestrator.py "a sunset over a mountain lake"')
        print()
        print("Checking current status...")
        status = get_vram_status()
        print(f"  LM Studio reachable:  {status['lmstudio_reachable']}")
        print(f"  Loaded models:        {status['loaded_models'] or '(none)'}")
        print(f"  SwarmUI reachable:    {status['swarmui_reachable']}")
        sys.exit(0)

    prompt = " ".join(sys.argv[1:])
    print(f"Generating image for prompt: {prompt!r}")
    result = generate_image(prompt)

    if result["success"]:
        print(f"\nSUCCESS! Saved {len(result['file_paths'])} image(s):")
        for p in result["file_paths"]:
            print(f"  {p}")
    else:
        print(f"\nFAILED at phase '{result['phase']}': {result['error']}")

    print(f"\nLLM reloaded: {result['llm_reloaded']}")
