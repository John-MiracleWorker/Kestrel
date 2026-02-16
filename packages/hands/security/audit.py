"""
Audit logger — records all skill executions for accountability.
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger("hands.security.audit")


class AuditLogger:
    """
    Logs every skill execution with full context.
    Stores to file (Phase 1) and will integrate with
    the PostgreSQL audit_log table in Phase 2.
    """

    def __init__(self):
        self._log_dir = os.getenv("AUDIT_LOG_DIR", "./logs/audit")
        os.makedirs(self._log_dir, exist_ok=True)
        self._entries: dict[str, dict] = {}

    def log_start(self, exec_id: str, user_id: str, workspace_id: str,
                  skill_name: str, function_name: str, arguments: str):
        """Log the start of a skill execution."""
        entry = {
            "exec_id": exec_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "skill_name": skill_name,
            "function_name": function_name,
            "arguments_hash": hash(arguments),  # Don't log raw args for security
            "started_at": datetime.utcnow().isoformat(),
            "status": "running",
        }
        self._entries[exec_id] = entry
        logger.info(f"Audit: START {skill_name}.{function_name} [{exec_id[:8]}]")

    def log_complete(self, exec_id: str, status: str = "success",
                     execution_time_ms: int = 0, memory_used_mb: int = 0,
                     audit_log: dict = None, error: str = None):
        """Log the completion of a skill execution."""
        entry = self._entries.get(exec_id, {})
        entry.update({
            "status": status,
            "completed_at": datetime.utcnow().isoformat(),
            "execution_time_ms": execution_time_ms,
            "memory_used_mb": memory_used_mb,
        })
        if error:
            entry["error"] = error
        if audit_log:
            entry["sandbox_audit"] = {
                "network_requests": len(audit_log.get("network_requests", [])),
                "file_accesses": len(audit_log.get("file_accesses", [])),
                "system_calls": len(audit_log.get("system_calls", [])),
            }

        # Persist to file
        self._write_entry(entry)

        level = "INFO" if status == "success" else "WARNING"
        logger.log(
            logging.getLevelName(level),
            f"Audit: {status.upper()} {entry.get('skill_name', '?')}."
            f"{entry.get('function_name', '?')} [{exec_id[:8]}] "
            f"({execution_time_ms}ms, {memory_used_mb}MB)"
        )

        # Clean up in-memory
        self._entries.pop(exec_id, None)

    def _write_entry(self, entry: dict):
        """Append audit entry to daily log file."""
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        log_file = os.path.join(self._log_dir, f"audit-{date_str}.jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
