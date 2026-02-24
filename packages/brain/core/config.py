import os
import logging
from pathlib import Path
import json
from dotenv import load_dotenv

load_dotenv()

# Logging
logger = logging.getLogger("brain")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

# gRPC
GRPC_PORT = int(os.getenv("BRAIN_GRPC_PORT", "50051"))
GRPC_HOST = os.getenv("BRAIN_GRPC_HOST", "0.0.0.0")

# Agent Constants
TASK_EVENT_HISTORY_MAX = int(os.getenv("TASK_EVENT_HISTORY_MAX", "300"))
TASK_EVENT_TTL_SECONDS = int(os.getenv("TASK_EVENT_TTL_SECONDS", "3600"))

# Tool Catalog
TOOL_CATALOG_PATH = Path(__file__).resolve().parents[2] / "shared" / "tool-catalog.json"

def load_tool_catalog() -> list[dict]:
    with TOOL_CATALOG_PATH.open("r", encoding="utf-8") as catalog_file:
        return json.load(catalog_file)
