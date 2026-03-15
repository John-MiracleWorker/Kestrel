from __future__ import annotations

import time

import requests

from .vram_orchestrator_config import API_TIMEOUT, GLM_MODEL_ID, LMSTUDIO_BASE_URL, logger

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
