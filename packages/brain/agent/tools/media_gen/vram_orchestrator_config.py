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
OUTPUT_DIR = os.getenv(
    "KESTREL_IMAGE_OUTPUT_DIR",
    str((Path(os.getenv("KESTREL_HOME", "~/.kestrel")).expanduser() / "artifacts" / "media")),
)

# HTTP timeouts for API calls (seconds)
API_TIMEOUT = 30
