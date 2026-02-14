"""
Libre Bird — macOS Native Notifications & Reminder Scheduler.
Sends native macOS notifications via osascript.
All reminders run locally — nothing leaves the device.
"""

import logging
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("libre_bird.notifications")


def send_notification(title: str, message: str, sound: bool = True) -> bool:
    """Send a native macOS notification via osascript."""
    try:
        sound_str = 'sound name "default"' if sound else ""
        script = (
            f'display notification "{_escape_applescript(message)}" '
            f'with title "{_escape_applescript(title)}" {sound_str}'
        )
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
        logger.info(f"Notification sent: {title} — {message}")
        return True
    except Exception as e:
        logger.error(f"Notification failed: {e}")
        return False


def _escape_applescript(text: str) -> str:
    """Escape special characters for AppleScript strings."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


class Reminder:
    """A single scheduled reminder."""

    def __init__(self, message: str, fire_at: datetime, reminder_id: str = None):
        self.id = reminder_id or str(uuid.uuid4())[:8]
        self.message = message
        self.fire_at = fire_at
        self.fired = False
        self.cancelled = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "message": self.message,
            "fire_at": self.fire_at.isoformat(),
            "remaining_seconds": max(
                0, int((self.fire_at - datetime.now()).total_seconds())
            ),
            "fired": self.fired,
            "cancelled": self.cancelled,
        }


class ReminderScheduler:
    """Background scheduler for timed reminders."""

    def __init__(self):
        self._reminders: list[Reminder] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background scheduler loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Reminder scheduler started")

    def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Reminder scheduler stopped")

    def schedule_reminder(self, message: str, delay_minutes: float) -> dict:
        """Schedule a reminder N minutes from now."""
        fire_at = datetime.now() + timedelta(minutes=delay_minutes)
        reminder = Reminder(message=message, fire_at=fire_at)
        with self._lock:
            self._reminders.append(reminder)
        logger.info(
            f"Reminder scheduled: '{message}' in {delay_minutes} min (id={reminder.id})"
        )
        return reminder.to_dict()

    def list_reminders(self) -> list[dict]:
        """Return all active (pending) reminders."""
        with self._lock:
            return [
                r.to_dict()
                for r in self._reminders
                if not r.fired and not r.cancelled
            ]

    def cancel_reminder(self, reminder_id: str) -> bool:
        """Cancel a pending reminder by ID."""
        with self._lock:
            for r in self._reminders:
                if r.id == reminder_id and not r.fired:
                    r.cancelled = True
                    logger.info(f"Reminder cancelled: {reminder_id}")
                    return True
        return False

    def _run_loop(self):
        """Check for due reminders every second."""
        while self._running:
            now = datetime.now()
            with self._lock:
                for r in self._reminders:
                    if not r.fired and not r.cancelled and now >= r.fire_at:
                        r.fired = True
                        send_notification("🕊️ Libre Bird Reminder", r.message)
                # Clean up old reminders (fired or cancelled > 1 hour ago)
                self._reminders = [
                    r
                    for r in self._reminders
                    if not (
                        (r.fired or r.cancelled)
                        and (now - r.fire_at).total_seconds() > 3600
                    )
                ]
            time.sleep(1)


# Singleton instance
reminder_scheduler = ReminderScheduler()
