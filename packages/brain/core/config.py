import os
import logging
import sys
from pathlib import Path
import json
from dotenv import load_dotenv

load_dotenv()

# Logging — structured JSON in production, readable in dev
_log_format = os.getenv("LOG_FORMAT", "text")
_log_level = os.getenv("LOG_LEVEL", "INFO").upper()

if _log_format == "json":
    logging.basicConfig(
        level=_log_level,
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    )
else:
    logging.basicConfig(level=_log_level)

logger = logging.getLogger("brain")

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


# ── Startup Configuration Validation ─────────────────────────────────

def validate_config() -> None:
    """Validate critical configuration at startup.

    Checks that required env vars are set and reasonable.
    Raises SystemExit on fatal errors, logs warnings for non-fatal issues.
    """
    errors: list[str] = []
    warnings: list[str] = []

    is_production = os.getenv("NODE_ENV", "development") == "production"

    # Database connectivity
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        pg_host = os.getenv("POSTGRES_HOST", "")
        pg_password = os.getenv("POSTGRES_PASSWORD", "")
        if not pg_host:
            warnings.append("POSTGRES_HOST not set — defaulting to 'localhost'")
        if pg_password in ("", "changeme") and is_production:
            errors.append(
                "POSTGRES_PASSWORD is unset or still 'changeme' in production"
            )

    # Encryption key
    if not os.getenv("ENCRYPTION_KEY"):
        if is_production:
            errors.append(
                "ENCRYPTION_KEY must be set in production. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        else:
            warnings.append(
                "ENCRYPTION_KEY not set — using ephemeral key; "
                "encrypted data will not survive restarts"
            )

    # Redis
    redis_host = os.getenv("REDIS_HOST", "")
    if not redis_host and not os.getenv("REDIS_URL", ""):
        warnings.append("REDIS_HOST not set — defaulting to 'localhost'")

    # gRPC port sanity
    try:
        port = int(os.getenv("BRAIN_GRPC_PORT", "50051"))
        if port < 1 or port > 65535:
            errors.append(f"BRAIN_GRPC_PORT={port} is out of valid range (1-65535)")
    except ValueError:
        errors.append("BRAIN_GRPC_PORT is not a valid integer")

    # Report findings
    for w in warnings:
        logger.warning("Config warning: %s", w)
    for e in errors:
        logger.error("Config error: %s", e)

    if errors:
        logger.error(
            "Startup aborted due to %d configuration error(s). "
            "Fix the above issues and restart.",
            len(errors),
        )
        sys.exit(1)
