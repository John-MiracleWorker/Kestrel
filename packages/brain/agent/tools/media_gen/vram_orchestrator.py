from __future__ import annotations

import logging
import time

import requests

from .vram_orchestrator_config import (
    GLM_MODEL_ID,
    LMSTUDIO_BASE_URL,
    SWARMUI_BASE_URL,
    SWARM_DEFAULT_VIDEO_MODEL,
    VRAM_FLUSH_DELAY,
    logger,
)
from .vram_orchestrator_lmstudio import load_llm, unload_llm, verify_vram_cleared
from .vram_orchestrator_swarm import (
    _infer_media_type,
    _save_images,
    generate_image_swarmui,
)

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
        send_telegram: Deprecated compatibility flag. Media delivery now flows
            through shared channel events handled by Gateway.

    Returns:
        dict with keys:
          - success (bool)
          - file_paths (list[str]): Saved image paths on disk
          - llm_unloaded (bool): Whether LLM was successfully unloaded
          - llm_reloaded (bool): Whether LLM was successfully reloaded
          - image_result (dict): Raw SwarmUI response details
          - artifacts (list[dict]): Shared artifact descriptors for channel delivery
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
        "artifacts": [],
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
            result["artifacts"] = [
                {
                    "path": item["path"],
                    "url": item["url"],
                    "type": _infer_media_type(item["path"]),
                }
                for item in saved
            ]
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

    # NOTE: Channel delivery is handled by Gateway from the shared media
    # artifact metadata returned by the tool handlers.

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

    if reload_result["success"]:
        # Verify the model is truly loaded and ready, not just accepted.
        # LM Studio returns 200 on the load endpoint immediately, but
        # a 19GB model takes 30-60s to fully load into VRAM.
        model_id = GLM_MODEL_ID
        lm_base = (LMSTUDIO_BASE_URL or "").rstrip("/")
        verify_timeout = 120  # max seconds to wait for the model to appear
        poll_interval = 5
        elapsed = 0
        model_ready = False
        logger.info(
            f"Phase 6/6: Load accepted. Verifying model is ready "
            f"(polling every {poll_interval}s, timeout {verify_timeout}s)..."
        )
        while elapsed < verify_timeout:
            try:
                resp = requests.get(f"{lm_base}/v1/models", timeout=10)
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    loaded_ids = [m.get("id", "") for m in data]
                    if any(model_id in mid for mid in loaded_ids):
                        model_ready = True
                        logger.info(
                            f"Phase 6/6: Model confirmed loaded after {elapsed}s. "
                            f"Loaded models: {loaded_ids}"
                        )
                        break
            except requests.RequestException:
                pass
            time.sleep(poll_interval)
            elapsed += poll_interval

        if not model_ready:
            reload_warning = (
                f"WARNING: LLM load was accepted but model failed to appear "
                f"in /v1/models after {verify_timeout}s. The model may still be "
                f"loading — check LM Studio manually."
            )
            logger.warning(reload_warning)
            if result["error"]:
                result["error"] += f" | {reload_warning}"
            else:
                result["error"] = reload_warning
        else:
            logger.info("Phase 6/6: LLM reloaded and verified. Kestrel is ready to chat.")
    else:
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
