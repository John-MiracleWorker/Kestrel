"""
Libre Bird — Global Hotkey Listener.
Registers ⌘+Shift+Space to bring the Libre Bird window to front.
Uses pynput for cross-platform keyboard monitoring.
"""

import logging
import subprocess
import threading
from typing import Optional

logger = logging.getLogger("libre_bird.hotkey")

# Try to import pynput — it's optional
try:
    from pynput import keyboard
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False
    logger.warning("pynput not installed — global hotkey disabled")


def _bring_window_to_front():
    """Bring the Libre Bird window to front using AppleScript."""
    try:
        script = '''
        tell application "System Events"
            set frontApp to name of first application process whose frontmost is true
        end tell
        tell application "Libre Bird"
            activate
        end tell
        '''
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=3,
        )
        logger.info("Brought Libre Bird to front via hotkey")
    except Exception as e:
        logger.error(f"Failed to bring window to front: {e}")
        # Fallback: try open command
        try:
            subprocess.run(
                ["open", "-a", "Libre Bird"],
                capture_output=True,
                timeout=3,
            )
        except Exception:
            pass


class GlobalHotkey:
    """Manages the global hotkey listener."""

    def __init__(self, on_activate=None):
        self._listener: Optional[keyboard.GlobalHotKeys] = None
        self._running = False
        self.on_activate = on_activate or _bring_window_to_front

    def start(self):
        """Start listening for the global hotkey."""
        if not PYNPUT_AVAILABLE:
            logger.warning("Cannot start hotkey: pynput not available")
            return

        if self._running:
            return

        try:
            self._listener = keyboard.GlobalHotKeys({
                "<cmd>+<shift>+<space>": self._on_hotkey,
            })
            self._listener.daemon = True
            self._listener.start()
            self._running = True
            logger.info("Global hotkey registered: ⌘+Shift+Space")
        except Exception as e:
            logger.error(f"Failed to register global hotkey: {e}")

    def stop(self):
        """Stop the hotkey listener."""
        if self._listener:
            self._listener.stop()
            self._running = False
            logger.info("Global hotkey listener stopped")

    def _on_hotkey(self):
        """Called when the hotkey is pressed."""
        logger.info("Global hotkey triggered!")
        try:
            self.on_activate()
        except Exception as e:
            logger.error(f"Hotkey callback error: {e}")

    @property
    def is_running(self) -> bool:
        return self._running


# Singleton instance
global_hotkey = GlobalHotkey()
