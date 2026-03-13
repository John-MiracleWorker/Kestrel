"""
Media Generation Config — environment-based configuration for SwarmUI/ComfyUI remote host.

Manages connection details for the Windows host running SwarmUI (ComfyUI backend)
on the local LAN. All values are sourced from environment variables with sensible defaults.
"""

import os
import uuid
from pathlib import Path

# ── Remote Host ──────────────────────────────────────────────────────

SWARM_HOST_IP = os.getenv("SWARM_HOST_IP", "192.168.1.19")
SWARM_PORT = int(os.getenv("SWARM_PORT", "7801"))

# ── Derived URLs ─────────────────────────────────────────────────────

SWARM_BASE_URL = f"http://{SWARM_HOST_IP}:{SWARM_PORT}"
SWARM_WS_URL = f"ws://{SWARM_HOST_IP}:{SWARM_PORT}"

# ── Client Identity ──────────────────────────────────────────────────

CLIENT_ID = os.getenv("SWARM_CLIENT_ID", str(uuid.uuid4()))

# ── Timeouts (seconds) ───────────────────────────────────────────────

# Image generation typically completes within 2 minutes
IMAGE_TIMEOUT = int(os.getenv("SWARM_IMAGE_TIMEOUT", "300"))

# Video generation can take up to 15 minutes on an RTX 4070
VIDEO_TIMEOUT = int(os.getenv("SWARM_VIDEO_TIMEOUT", "900"))

# HTTP request timeout for individual REST calls
HTTP_TIMEOUT = int(os.getenv("SWARM_HTTP_TIMEOUT", "30"))

# WebSocket connect timeout
WS_CONNECT_TIMEOUT = int(os.getenv("SWARM_WS_CONNECT_TIMEOUT", "15"))

# ── Output ────────────────────────────────────────────────────────────

def _default_output_dir() -> str:
    kestrel_home = os.getenv("KESTREL_HOME", "~/.kestrel")
    return str((Path(kestrel_home).expanduser() / "artifacts" / "media"))


OUTPUT_DIR = os.getenv("SWARM_OUTPUT_DIR", _default_output_dir())

# ── Telegram (optional delivery) ─────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
