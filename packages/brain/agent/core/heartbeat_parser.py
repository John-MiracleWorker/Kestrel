import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("brain.agent.core.heartbeat_parser")

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
                    if re.search(r"\bevery heartbeat\b|\balways\b", header):
                        current_frequency = "every"
                    elif re.search(r"\bhourly\b", header):
                        current_frequency = "hourly"
                    elif re.search(r"\bdaily\b", header):
                        current_frequency = "daily"
                    else:
                        current_frequency = "every"  # default fallback
                elif re.match(r"[-*]\s+", line):
                    task_desc = re.sub(r"^[-*]\s+", "", line).strip()
                    if task_desc:
                        tasks.append(HeartbeatTask(description=task_desc, frequency=current_frequency))

        except Exception as e:
            logger.error(f"Failed to parse HEARTBEAT.md: {e}")

        return tasks
