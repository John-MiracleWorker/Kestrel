#!/usr/bin/env python3
"""
Kestrel OS Daemon
Runs as a persistent launchd background process.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

# Paths
KESTREL_HOME = Path(os.path.expanduser("~/.kestrel"))
STATE_DIR = KESTREL_HOME / "state"
HEARTBEAT_STATE_FILE = STATE_DIR / "heartbeat.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kestrel-daemon")

START_TIME = time.time()

def setup_directories():
    directories = [
        KESTREL_HOME,
        KESTREL_HOME / "memory",
        KESTREL_HOME / "memory" / "custom",
        KESTREL_HOME / "tasks" / "active",
        KESTREL_HOME / "tasks" / "history",
        KESTREL_HOME / "skills",
        KESTREL_HOME / "watchlist",
        KESTREL_HOME / "audit",
        KESTREL_HOME / "state",
    ]
    for d in directories:
        d.mkdir(parents=True, exist_ok=True)

    # Create default configuration if missing
    config_file = KESTREL_HOME / "config.yml"
    if not config_file.exists():
        config_file.write_text(
            "heartbeat:\n  interval: 1800\n  quiet_hours:\n    start: '23:00'\n    end: '07:00'\n", 
            encoding="utf-8"
        )

    heartbeat_file = KESTREL_HOME / "HEARTBEAT.md"
    if not heartbeat_file.exists():
        heartbeat_file.write_text(
            "# Kestrel Heartbeat Tasks\n\n## Every heartbeat\n- Check system status\n", 
            encoding="utf-8"
        )

    workspace_file = KESTREL_HOME / "WORKSPACE.md"
    if not workspace_file.exists():
        workspace_file.write_text(
            "# Active Workspace Context\n\n## Current Projects\n- None specified yet.\n", 
            encoding="utf-8"
        )
        
    paths_file = KESTREL_HOME / "watchlist" / "paths.yml"
    if not paths_file.exists():
        paths_file.write_text("paths:\n  - ~/Downloads\n", encoding="utf-8")

def update_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    state = {
        "last_heartbeat": now,
        "status": "active",
        "uptime": now - START_TIME,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "next_heartbeat": now + 1800,
    }
    # Write atomically: write to a temp file then rename so a crash mid-write
    # cannot leave a corrupted state file.
    tmp_path = None
    try:
        with Path(str(HEARTBEAT_STATE_FILE) + ".tmp").open("w") as f:
            json.dump(state, f)
            tmp_path = f.name
        os.replace(tmp_path, HEARTBEAT_STATE_FILE)
    except Exception as e:
        logger.error(f"Failed to write heartbeat state: {e}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

async def heartbeat_loop():
    logger.info("Daemon started, heartbeat loop active.")
    while True:
        try:
            update_state()
            logger.info("Heartbeat tick.")
            # TODO: Phase 2 and 3 integration will go here
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
        
        await asyncio.sleep(60)  # fast polling for dev, will use interval in complete implementation

async def main():
    setup_directories()
    await heartbeat_loop()

if __name__ == "__main__":
    asyncio.run(main())
