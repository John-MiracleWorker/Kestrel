import os
import re
from pathlib import Path

class HeartbeatTask:
    def __init__(self, description: str, frequency: str = "every"):
        self.description = description
        self.frequency = frequency

class HeartbeatParser:
    """
    Parses ~/.kestrel/HEARTBEAT.md to extract user-defined autonomous tasks.
    """
    def __init__(self):
        self.file_path = Path(os.path.expanduser("~/.kestrel/HEARTBEAT.md"))

    def parse(self) -> list[HeartbeatTask]:
        tasks = []
        if not self.file_path.exists():
            return tasks

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                content = f.read()

            current_frequency = "every"
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue

                if line.startswith("## "):
                    header = line[3:].strip().lower()
                    if "every heartbeat" in header or "always" in header:
                        current_frequency = "every"
                    elif "hourly" in header:
                        current_frequency = "hourly"
                    elif "daily" in header:
                        current_frequency = "daily"
                    else:
                        current_frequency = "every" # default fallback
                elif line.startswith("- ") or line.startswith("* "):
                    task_desc = line[2:].strip()
                    if task_desc:
                        tasks.append(HeartbeatTask(description=task_desc, frequency=current_frequency))

        except Exception as e:
            import logging
            logging.getLogger("brain.agent.core.heartbeat_parser").error(f"Failed to parse HEARTBEAT.md: {e}")

        return tasks
