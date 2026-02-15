"""
Task Scheduler Skill
Schedule commands, shell scripts, or prompts to run at specific times or intervals.
Uses stdlib threading.Timer — no external dependencies.
Scheduled tasks persist in a JSON file so they survive restarts.
"""

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict

logger = logging.getLogger("libre_bird.skills.scheduler")

_SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "scheduled_tasks.json")
_active_timers: Dict[str, threading.Timer] = {}
_tasks: Dict[str, dict] = {}


def _ensure_data_dir():
    os.makedirs(os.path.dirname(_SCHEDULE_FILE), exist_ok=True)


def _load_tasks():
    global _tasks
    _ensure_data_dir()
    if os.path.exists(_SCHEDULE_FILE):
        try:
            with open(_SCHEDULE_FILE, "r") as f:
                _tasks = json.load(f)
        except Exception:
            _tasks = {}
    return _tasks


def _save_tasks():
    _ensure_data_dir()
    with open(_SCHEDULE_FILE, "w") as f:
        json.dump(_tasks, f, indent=2)


def _execute_task(task_id: str):
    """Execute a scheduled task."""
    task = _tasks.get(task_id)
    if not task:
        return

    logger.info(f"Executing scheduled task: {task.get('name', task_id)}")

    action = task.get("action", "")
    action_type = task.get("action_type", "shell")

    try:
        if action_type == "shell":
            result = subprocess.run(
                action, shell=True, capture_output=True, text=True, timeout=60
            )
            task["last_result"] = result.stdout[:500] if result.stdout else result.stderr[:500]
        elif action_type == "applescript":
            result = subprocess.run(
                ["osascript", "-e", action],
                capture_output=True, text=True, timeout=15
            )
            task["last_result"] = result.stdout[:500]
        elif action_type == "notification":
            subprocess.run([
                "osascript", "-e",
                f'display notification "{action}" with title "Libre Bird Scheduler"'
            ], timeout=5)
            task["last_result"] = "Notification sent"
        else:
            task["last_result"] = f"Unknown action type: {action_type}"

        task["last_run"] = datetime.now().isoformat()
        task["run_count"] = task.get("run_count", 0) + 1

        # Handle repeating tasks
        interval = task.get("interval_seconds")
        if interval and interval > 0:
            timer = threading.Timer(interval, _execute_task, args=[task_id])
            timer.daemon = True
            timer.start()
            _active_timers[task_id] = timer
        else:
            task["status"] = "completed"
            _active_timers.pop(task_id, None)

        _save_tasks()

    except Exception as e:
        task["last_result"] = f"Error: {str(e)}"
        task["status"] = "error"
        _save_tasks()


def tool_schedule_task(args: dict) -> dict:
    """Schedule a task to run at a specific time or after a delay."""
    _load_tasks()

    name = args.get("name", "Unnamed Task")
    action = args.get("action", "")
    action_type = args.get("action_type", "notification")  # shell, applescript, notification
    delay_minutes = args.get("delay_minutes")
    interval_minutes = args.get("interval_minutes")
    run_at = args.get("run_at")  # ISO time string

    if not action:
        return {"error": "action is required (shell command, applescript, or notification text)"}

    task_id = str(uuid.uuid4())[:8]
    now = datetime.now()

    # Calculate delay
    delay_seconds = 0
    if delay_minutes is not None:
        delay_seconds = float(delay_minutes) * 60
    elif run_at:
        try:
            target = datetime.fromisoformat(run_at)
            delay_seconds = max(0, (target - now).total_seconds())
        except ValueError:
            return {"error": f"Invalid run_at format: {run_at}. Use ISO format like '2026-02-15T14:30:00'"}
    else:
        delay_seconds = 0  # Run immediately

    interval_seconds = None
    if interval_minutes is not None:
        interval_seconds = float(interval_minutes) * 60

    task = {
        "id": task_id,
        "name": name,
        "action": action,
        "action_type": action_type,
        "created": now.isoformat(),
        "scheduled_for": (now + timedelta(seconds=delay_seconds)).isoformat(),
        "interval_seconds": interval_seconds,
        "status": "scheduled",
        "run_count": 0,
    }

    _tasks[task_id] = task

    # Start the timer
    timer = threading.Timer(delay_seconds, _execute_task, args=[task_id])
    timer.daemon = True
    timer.start()
    _active_timers[task_id] = timer

    _save_tasks()

    result = {
        "success": True,
        "task_id": task_id,
        "name": name,
        "scheduled_for": task["scheduled_for"],
        "action_type": action_type,
    }
    if interval_seconds:
        result["repeats_every"] = f"{interval_minutes} minutes"
    return result


def tool_list_scheduled(args: dict) -> dict:
    """List all scheduled tasks."""
    _load_tasks()

    tasks_list = []
    for tid, task in _tasks.items():
        tasks_list.append({
            "id": tid,
            "name": task.get("name", ""),
            "status": task.get("status", "unknown"),
            "action_type": task.get("action_type", ""),
            "scheduled_for": task.get("scheduled_for", ""),
            "last_run": task.get("last_run", "never"),
            "run_count": task.get("run_count", 0),
            "active": tid in _active_timers,
        })

    return {"tasks": tasks_list, "count": len(tasks_list)}


def tool_cancel_scheduled(args: dict) -> dict:
    """Cancel a scheduled task by ID."""
    task_id = args.get("task_id", "").strip()
    if not task_id:
        return {"error": "task_id is required"}

    _load_tasks()

    if task_id not in _tasks:
        return {"error": f"Task '{task_id}' not found"}

    # Cancel the timer
    timer = _active_timers.pop(task_id, None)
    if timer:
        timer.cancel()

    _tasks[task_id]["status"] = "cancelled"
    _save_tasks()

    return {"success": True, "task_id": task_id, "message": f"Task '{_tasks[task_id].get('name', task_id)}' cancelled"}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_task",
            "description": "Schedule a task to run after a delay, at a specific time, or on a repeating interval. Tasks can be shell commands, AppleScript, or notifications.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable name for this task"},
                    "action": {"type": "string", "description": "The command/script/text to execute"},
                    "action_type": {"type": "string", "enum": ["shell", "applescript", "notification"], "description": "Type of action (default: notification)"},
                    "delay_minutes": {"type": "number", "description": "Minutes from now to run (use this OR run_at)"},
                    "run_at": {"type": "string", "description": "ISO datetime to run at (e.g. '2026-02-15T14:30:00')"},
                    "interval_minutes": {"type": "number", "description": "If set, repeat every N minutes"},
                },
                "required": ["name", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scheduled",
            "description": "List all scheduled tasks with their status, next run time, and run history.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_scheduled",
            "description": "Cancel a scheduled task by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task ID to cancel"},
                },
                "required": ["task_id"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "schedule_task": tool_schedule_task,
    "list_scheduled": tool_list_scheduled,
    "cancel_scheduled": tool_cancel_scheduled,
}
