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

logger = logging.getLogger("kestrel.vram_orchestrator")

# ── Configuration ────────────────────────────────────────────────────────────

# LM Studio endpoint
LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234")

# SwarmUI endpoint
SWARMUI_BASE_URL = os.getenv("SWARMUI_BASE_URL", "http://localhost:7801")

# Drop in your exact LM Studio model identifier here, e.g.:
#   "glm-4-9b-chat-q4_k_m"  or  "THUDM/glm-4-flash-30b-gguf"
GLM_MODEL_ID = os.getenv(
    "KESTREL_GLM_MODEL_ID",
    "YOUR_GLM_MODEL_ID_HERE",  # <-- PLACEHOLDER: replace with your LM Studio model ID
)

# Seconds to wait after unload before hitting SwarmUI.
# Windows needs time to actually release 19GB from the GPU driver.
VRAM_FLUSH_DELAY = float(os.getenv("KESTREL_VRAM_FLUSH_DELAY", "6.0"))

# How long to wait for a single image generation (seconds).
IMAGE_GEN_TIMEOUT = int(os.getenv("KESTREL_IMAGE_GEN_TIMEOUT", "300"))

# Directory to save generated images
OUTPUT_DIR = os.getenv("KESTREL_IMAGE_OUTPUT_DIR", os.path.join(os.path.expanduser("~"), "Kestrel", "generated_images"))

# HTTP timeouts for API calls (seconds)
API_TIMEOUT = 30


# ── LM Studio: Model Lifecycle ──────────────────────────────────────────────


def unload_llm(
    model_id: str = None,
    base_url: str = None,
) -> dict:
    """
    Unload the GLM model from VRAM via LM Studio's REST API.

    Tries the documented /api/v1/models/unload endpoint first, then falls back
    to the DELETE /v1/models/{model_id} convention if the first one isn't
    available.

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

    # ── Attempt 1: POST /api/v1/models/unload ────────────────────────
    try:
        url = f"{base_url}/api/v1/models/unload"
        payload = {"model": model_id}
        logger.info(f"Unloading LLM via POST {url}  model={model_id}")

        resp = requests.post(url, json=payload, timeout=API_TIMEOUT)

        if resp.status_code in (200, 204):
            logger.info("LLM unloaded successfully (POST /api/v1/models/unload)")
            return {"success": True, "method": "post_unload", "detail": resp.text}

        logger.warning(
            f"POST /api/v1/models/unload returned {resp.status_code}: {resp.text}. "
            "Trying fallback..."
        )
    except requests.ConnectionError:
        logger.error(f"Cannot reach LM Studio at {base_url}. Is it running?")
        return {
            "success": False,
            "method": "post_unload",
            "detail": f"Connection refused: {base_url}",
        }
    except requests.RequestException as exc:
        logger.warning(f"POST /api/v1/models/unload failed: {exc}. Trying fallback...")

    # ── Attempt 2: DELETE /v1/models/{model_id} ──────────────────────
    try:
        url = f"{base_url}/v1/models/{model_id}"
        logger.info(f"Unloading LLM via DELETE {url}")

        resp = requests.delete(url, timeout=API_TIMEOUT)

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

        resp = requests.post(url, json=payload, timeout=API_TIMEOUT)

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

        resp = requests.post(url, json=payload, timeout=API_TIMEOUT)

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
    max_retries: int = 5,
    retry_delay: float = 2.0,
) -> bool:
    """
    Verify that no models are loaded in LM Studio (VRAM is free).

    Polls GET /v1/models and checks that the returned list is empty.

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
    steps: int = 20,
    width: int = 1024,
    height: int = 1024,
    cfg_scale: float = 7.0,
    seed: int = -1,
    model: str = "",
    base_url: str = None,
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
    url = f"{base_url}/API/GenerateText2Image"

    payload = {
        "prompt": prompt,
        "negativeprompt": negative_prompt,
        "images": images,
        "steps": steps,
        "width": width,
        "height": height,
        "cfgscale": cfg_scale,
        "seed": seed,
    }
    if model:
        payload["model"] = model

    logger.info(
        f"Sending image generation request to SwarmUI: "
        f"prompt={prompt[:80]!r}  steps={steps}  size={width}x{height}"
    )

    try:
        resp = requests.post(url, json=payload, timeout=IMAGE_GEN_TIMEOUT)

        if resp.status_code != 200:
            detail = f"SwarmUI returned {resp.status_code}: {resp.text[:500]}"
            logger.error(detail)
            return {"success": False, "images": [], "detail": detail}

        data = resp.json()

        # SwarmUI returns images as base64 strings in the "images" field
        result_images = data.get("images", [])
        if not result_images:
            detail = f"SwarmUI returned 200 but no images in response: {list(data.keys())}"
            logger.warning(detail)
            return {"success": False, "images": [], "detail": detail}

        logger.info(f"SwarmUI returned {len(result_images)} image(s).")
        return {
            "success": True,
            "images": result_images,
            "detail": f"Generated {len(result_images)} image(s)",
        }

    except requests.ConnectionError:
        detail = (
            f"Cannot reach SwarmUI at {base_url}. "
            "Is SwarmUI running on localhost:7801?"
        )
        logger.error(detail)
        return {"success": False, "images": [], "detail": detail}
    except requests.Timeout:
        detail = f"SwarmUI request timed out after {IMAGE_GEN_TIMEOUT}s."
        logger.error(detail)
        return {"success": False, "images": [], "detail": detail}
    except requests.RequestException as exc:
        detail = f"SwarmUI request failed: {exc}"
        logger.error(detail)
        return {"success": False, "images": [], "detail": detail}


def _save_images(images_data: list, prompt: str) -> list[str]:
    """
    Save base64-encoded images to disk.

    Args:
        images_data: List of base64-encoded image strings from SwarmUI.
        prompt: The original prompt (used to build a readable filename).

    Returns:
        List of absolute file paths for saved images.
    """
    import base64

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Build a filesystem-safe prefix from the prompt
    safe_prefix = "".join(c if c.isalnum() or c in " _-" else "" for c in prompt[:50]).strip()
    safe_prefix = safe_prefix.replace(" ", "_") or "image"
    timestamp = int(time.time())

    saved_paths = []
    for idx, img_b64 in enumerate(images_data):
        # Strip data URI prefix if present (e.g. "data:image/png;base64,...")
        if "," in img_b64 and img_b64.startswith("data:"):
            img_b64 = img_b64.split(",", 1)[1]

        try:
            img_bytes = base64.b64decode(img_b64)
        except Exception as exc:
            logger.error(f"Failed to decode image {idx}: {exc}")
            continue

        suffix = idx if len(images_data) > 1 else ""
        filename = f"{safe_prefix}_{timestamp}{f'_{suffix}' if suffix != '' else ''}.png"
        filepath = os.path.join(OUTPUT_DIR, filename)

        with open(filepath, "wb") as f:
            f.write(img_bytes)

        abs_path = os.path.abspath(filepath)
        saved_paths.append(abs_path)
        logger.info(f"Saved image: {abs_path} ({len(img_bytes):,} bytes)")

    return saved_paths


# ── Main Orchestrator ────────────────────────────────────────────────────────


def generate_image(
    prompt: str,
    negative_prompt: str = "",
    images: int = 1,
    steps: int = 20,
    width: int = 1024,
    height: int = 1024,
    cfg_scale: float = 7.0,
    seed: int = -1,
    swarm_model: str = "",
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

    Returns:
        dict with keys:
          - success (bool)
          - file_paths (list[str]): Saved image paths on disk
          - llm_unloaded (bool): Whether LLM was successfully unloaded
          - llm_reloaded (bool): Whether LLM was successfully reloaded
          - image_result (dict): Raw SwarmUI response details
          - error (str): Error message if failed
          - phase (str): Which phase failed (unload / verify / generate / save / reload)
    """
    result = {
        "success": False,
        "file_paths": [],
        "llm_unloaded": False,
        "llm_reloaded": False,
        "image_result": {},
        "error": "",
        "phase": "",
    }

    # ── Phase 1: Unload LLM ──────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("VRAM ORCHESTRATOR: Starting image generation pipeline")
    logger.info(f"  Prompt: {prompt[:100]!r}")
    logger.info("=" * 60)

    logger.info("Phase 1/5: Unloading LLM from VRAM...")
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
    logger.info("Phase 1/5: LLM unload command accepted.")

    # ── Phase 2: Verify VRAM cleared ─────────────────────────────────
    logger.info("Phase 2/5: Verifying VRAM is cleared...")
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
        f"Phase 3/5: Waiting {VRAM_FLUSH_DELAY}s for Windows GPU driver "
        f"to release VRAM..."
    )
    time.sleep(VRAM_FLUSH_DELAY)

    # ── Phase 4: Generate image via SwarmUI ──────────────────────────
    logger.info("Phase 4/5: Sending generation request to SwarmUI...")
    try:
        img_result = generate_image_swarmui(
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
        result["image_result"] = img_result

        if img_result["success"]:
            # Save images to disk
            saved = _save_images(img_result["images"], prompt)
            result["file_paths"] = saved
            if saved:
                result["success"] = True
                logger.info(f"Phase 4/5: Image generation complete. Saved {len(saved)} file(s).")
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

    # ── Phase 5: Reload LLM (always attempted) ───────────────────────
    logger.info("Phase 5/5: Reloading LLM into VRAM...")
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
        logger.info("Phase 5/5: LLM reloaded successfully. Kestrel is ready to chat.")

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
