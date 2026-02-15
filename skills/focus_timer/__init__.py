"""
Focus / Pomodoro Timer Skill
Manage timed focus sessions with break reminders, notifications, and productivity tracking.
Inspired by Focusdoro, Forest, and Reclaim.ai.

Uses only stdlib (threading, json, subprocess for notifications) — zero dependencies.
"""

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger("libre_bird.skills.focus_timer")

_DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "focus_history.json")

# Current session state (in-memory)
_current_session = {
    "active": False,
    "task": "",
    "start_time": None,
    "duration_minutes": 25,
    "break_minutes": 5,
    "elapsed": 0,
    "timer": None,
}


def _ensure_data_dir():
    os.makedirs(os.path.dirname(_DATA_FILE), exist_ok=True)


def _notify(title: str, message: str):
    """Send a macOS notification."""
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{message}" with title "{title}" sound name "Glass"'
        ], timeout=5, capture_output=True)
    except Exception:
        pass


def _load_history() -> list:
    _ensure_data_dir()
    if os.path.exists(_DATA_FILE):
        try:
            with open(_DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_session(session: dict):
    history = _load_history()
    history.append(session)
    # Keep last 100 sessions
    history = history[-100:]
    with open(_DATA_FILE, "w") as f:
        json.dump(history, f, indent=2)


def _timer_complete():
    """Called when a focus session completes."""
    _current_session["active"] = False
    task = _current_session.get("task", "Focus session")
    duration = _current_session.get("duration_minutes", 25)
    break_mins = _current_session.get("break_minutes", 5)

    # Save to history
    _save_session({
        "task": task,
        "duration_minutes": duration,
        "started": _current_session.get("start_time", ""),
        "completed": datetime.now().isoformat(),
        "status": "completed",
    })

    _notify("🍅 Focus Complete!", f"Great work on '{task}'! Take a {break_mins}-minute break.")

    # Start break timer
    def _break_done():
        _notify("⏰ Break Over", "Ready for another focus session?")

    break_timer = threading.Timer(break_mins * 60, _break_done)
    break_timer.daemon = True
    break_timer.start()

    logger.info(f"Focus session '{task}' completed ({duration} min)")


def tool_start_focus(args: dict) -> dict:
    """Start a Pomodoro focus session."""
    if _current_session["active"]:
        elapsed = time.time() - _current_session.get("_start_epoch", time.time())
        remaining = _current_session["duration_minutes"] * 60 - elapsed
        return {
            "error": "A session is already running",
            "task": _current_session["task"],
            "remaining_minutes": round(remaining / 60, 1),
        }

    task = args.get("task", "Focus session")
    duration = int(args.get("duration_minutes", 25))
    break_mins = int(args.get("break_minutes", 5))

    # Clamp duration
    duration = max(1, min(duration, 120))
    break_mins = max(1, min(break_mins, 30))

    now = datetime.now()
    _current_session.update({
        "active": True,
        "task": task,
        "start_time": now.isoformat(),
        "_start_epoch": time.time(),
        "duration_minutes": duration,
        "break_minutes": break_mins,
    })

    # Set completion timer
    timer = threading.Timer(duration * 60, _timer_complete)
    timer.daemon = True
    timer.start()
    _current_session["timer"] = timer

    _notify("🍅 Focus Started", f"Working on '{task}' for {duration} minutes. Stay focused!")

    end_time = now + timedelta(minutes=duration)
    return {
        "success": True,
        "task": task,
        "duration_minutes": duration,
        "break_minutes": break_mins,
        "started": now.strftime("%I:%M %p"),
        "ends_at": end_time.strftime("%I:%M %p"),
        "message": f"🍅 Focus session started! {duration} minutes on '{task}'. I'll notify you when it's time for a break.",
    }


def tool_stop_focus(args: dict) -> dict:
    """Stop the current focus session early."""
    if not _current_session["active"]:
        return {"error": "No active focus session"}

    # Cancel the timer
    timer = _current_session.get("timer")
    if timer:
        timer.cancel()

    elapsed = time.time() - _current_session.get("_start_epoch", time.time())
    elapsed_minutes = round(elapsed / 60, 1)

    # Save as partial session
    _save_session({
        "task": _current_session["task"],
        "duration_minutes": elapsed_minutes,
        "started": _current_session.get("start_time", ""),
        "completed": datetime.now().isoformat(),
        "status": "stopped_early",
        "planned_duration": _current_session["duration_minutes"],
    })

    task = _current_session["task"]
    _current_session["active"] = False

    return {
        "success": True,
        "task": task,
        "elapsed_minutes": elapsed_minutes,
        "message": f"Stopped focus on '{task}' after {elapsed_minutes} minutes.",
    }


def tool_focus_status(args: dict) -> dict:
    """Get the status of the current focus session."""
    if not _current_session["active"]:
        return {"active": False, "message": "No active focus session"}

    elapsed = time.time() - _current_session.get("_start_epoch", time.time())
    total_seconds = _current_session["duration_minutes"] * 60
    remaining = total_seconds - elapsed

    return {
        "active": True,
        "task": _current_session["task"],
        "elapsed_minutes": round(elapsed / 60, 1),
        "remaining_minutes": round(max(0, remaining) / 60, 1),
        "duration_minutes": _current_session["duration_minutes"],
        "progress_percent": round(min(100, (elapsed / total_seconds) * 100), 1),
        "started": _current_session.get("start_time", ""),
    }


def tool_focus_history(args: dict) -> dict:
    """Get focus session history and productivity stats."""
    history = _load_history()
    days = int(args.get("days", 7))

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    recent = [s for s in history if s.get("completed", "") >= cutoff]

    completed = [s for s in recent if s.get("status") == "completed"]
    stopped = [s for s in recent if s.get("status") == "stopped_early"]

    total_minutes = sum(s.get("duration_minutes", 0) for s in recent)

    # Group by day
    by_day = {}
    for s in recent:
        day = s.get("completed", "")[:10]
        if day:
            by_day[day] = by_day.get(day, 0) + s.get("duration_minutes", 0)

    return {
        "period": f"Last {days} days",
        "total_sessions": len(recent),
        "completed_sessions": len(completed),
        "stopped_early": len(stopped),
        "total_focus_minutes": round(total_minutes, 1),
        "total_focus_hours": round(total_minutes / 60, 1),
        "avg_minutes_per_day": round(total_minutes / max(1, days), 1),
        "daily_breakdown": by_day,
        "recent_sessions": recent[-5:],  # Last 5
    }


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "start_focus",
            "description": "Start a Pomodoro focus session. You'll get a notification when it's time for a break.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "What you're working on"},
                    "duration_minutes": {"type": "integer", "description": "Focus duration in minutes (default 25, max 120)"},
                    "break_minutes": {"type": "integer", "description": "Break duration after session (default 5, max 30)"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_focus",
            "description": "Stop the current focus session early.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_status",
            "description": "Check the status of the current focus session — elapsed time, remaining time, progress.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_history",
            "description": "Get focus session history and productivity stats — total focus time, sessions per day, completion rate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Number of days to look back (default 7)"},
                },
                "required": [],
            },
        },
    },
]

TOOL_HANDLERS = {
    "start_focus": tool_start_focus,
    "stop_focus": tool_stop_focus,
    "focus_status": tool_focus_status,
    "focus_history": tool_focus_history,
}
