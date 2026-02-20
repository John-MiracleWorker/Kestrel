"""
Apple Calendar Skill — List, create, and search calendar events via AppleScript.

Requires macOS — will raise a clear error on Linux/Docker.
"""

import json
import logging
import platform
import shutil
import subprocess
from datetime import datetime, timedelta

logger = logging.getLogger("libre_bird.skills.calendar")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_macos():
    """Raise a clear error if not running on macOS."""
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        raise RuntimeError(
            "This skill requires macOS with osascript. "
            "It cannot run in a Linux/Docker environment. "
            "Use OAuth-based calendar APIs (e.g., Google Calendar) instead."
        )


def _run_applescript(script: str) -> str:
    """Run an AppleScript and return stdout."""
    _check_macos()
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript failed")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_list_calendars(args: dict) -> dict:
    """List all available calendars."""
    script = '''
    tell application "Calendar"
        set calList to {}
        repeat with c in calendars
            set end of calList to (name of c)
        end repeat
        return calList
    end tell
    '''
    try:
        raw = _run_applescript(script)
        calendars = [c.strip() for c in raw.split(",") if c.strip()]
        return {"calendars": calendars, "count": len(calendars)}
    except Exception as e:
        return {"error": str(e)}


def tool_get_events(args: dict) -> dict:
    """Get calendar events for a date range."""
    days_ahead = int(args.get("days_ahead", 7))
    calendar_name = args.get("calendar", "")

    cal_filter = ""
    if calendar_name:
        cal_filter = f'of calendar "{calendar_name}"'

    script = f'''
    set today to current date
    set endDate to today + ({days_ahead} * days)

    tell application "Calendar"
        set eventList to {{}}
        set matchingEvents to (every event {cal_filter} whose start date >= today and start date <= endDate)
        repeat with evt in matchingEvents
            set evtStart to start date of evt
            set evtEnd to end date of evt
            set evtSummary to summary of evt
            set evtLoc to ""
            try
                set evtLoc to location of evt
            end try
            set evtInfo to evtSummary & " | " & (evtStart as string) & " | " & (evtEnd as string) & " | " & evtLoc
            set end of eventList to evtInfo
        end repeat
        return eventList
    end tell
    '''
    try:
        raw = _run_applescript(script)
        if not raw:
            return {"events": [], "count": 0, "range": f"Next {days_ahead} days"}

        events = []
        for line in raw.split(","):
            parts = [p.strip() for p in line.split(" | ")]
            if len(parts) >= 3:
                events.append({
                    "summary": parts[0],
                    "start": parts[1],
                    "end": parts[2],
                    "location": parts[3] if len(parts) > 3 else ""
                })
        return {"events": events, "count": len(events), "range": f"Next {days_ahead} days"}
    except Exception as e:
        return {"error": str(e)}


def tool_create_event(args: dict) -> dict:
    """Create a new calendar event."""
    title = args.get("title", "New Event")
    start_date = args.get("start_date", "")
    duration_minutes = int(args.get("duration_minutes", 60))
    location = args.get("location", "")
    notes = args.get("notes", "")
    calendar_name = args.get("calendar", "")

    if not start_date:
        return {"error": "start_date is required (e.g. 'February 20, 2026 2:00 PM')"}

    cal_target = 'calendar 1'
    if calendar_name:
        cal_target = f'calendar "{calendar_name}"'

    loc_line = ""
    if location:
        loc_line = f'set location of newEvent to "{location}"'

    notes_line = ""
    if notes:
        notes_line = f'set description of newEvent to "{notes}"'

    script = f'''
    tell application "Calendar"
        set startDate to date "{start_date}"
        set endDate to startDate + ({duration_minutes} * minutes)
        tell {cal_target}
            set newEvent to make new event with properties {{summary:"{title}", start date:startDate, end date:endDate}}
            {loc_line}
            {notes_line}
        end tell
        return "Created: {title}"
    end tell
    '''
    try:
        result = _run_applescript(script)
        return {"success": True, "message": result, "title": title, "start": start_date, "duration_minutes": duration_minutes}
    except Exception as e:
        return {"error": str(e)}


def tool_todays_agenda(args: dict) -> dict:
    """Get today's agenda — all events for today."""
    script = '''
    set today to current date
    set tomorrow to today + (1 * days)
    set time of today to 0
    set time of tomorrow to 0

    tell application "Calendar"
        set eventList to {}
        set matchingEvents to (every event of every calendar whose start date >= today and start date < tomorrow)
        repeat with evtGroup in matchingEvents
            repeat with evt in evtGroup
                set evtStart to start date of evt
                set evtSummary to summary of evt
                set evtLoc to ""
                try
                    set evtLoc to location of evt
                end try
                set evtInfo to evtSummary & " @ " & time string of evtStart
                if evtLoc is not "" then
                    set evtInfo to evtInfo & " (" & evtLoc & ")"
                end if
                set end of eventList to evtInfo
            end repeat
        end repeat
        return eventList
    end tell
    '''
    try:
        raw = _run_applescript(script)
        events = [e.strip() for e in raw.split(",") if e.strip()] if raw else []
        return {"date": datetime.now().strftime("%A, %B %d"), "events": events, "count": len(events)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_calendars",
            "description": "List all available calendars on this Mac.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": "Get upcoming calendar events for the next N days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {"type": "integer", "description": "Number of days to look ahead (default 7)"},
                    "calendar": {"type": "string", "description": "Filter to a specific calendar name (optional)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": "Create a new calendar event. Requires a title and start date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title"},
                    "start_date": {"type": "string", "description": "Start date/time (e.g. 'February 20, 2026 2:00 PM')"},
                    "duration_minutes": {"type": "integer", "description": "Duration in minutes (default 60)"},
                    "location": {"type": "string", "description": "Event location (optional)"},
                    "notes": {"type": "string", "description": "Event notes (optional)"},
                    "calendar": {"type": "string", "description": "Which calendar to add to (optional)"}
                },
                "required": ["title", "start_date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todays_agenda",
            "description": "Get today's complete agenda — all events for today across all calendars.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
]

TOOL_HANDLERS = {
    "list_calendars": tool_list_calendars,
    "get_events": tool_get_events,
    "create_event": tool_create_event,
    "todays_agenda": tool_todays_agenda,
}
