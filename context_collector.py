"""
Libre Bird — macOS Screen Context Collector.
Uses the Accessibility API to read active window info.
Requires accessibility permissions in System Settings.
"""

import asyncio
import threading
import time
import logging
from typing import Optional, Callable
from datetime import datetime

logger = logging.getLogger("libre_bird.context")

# Try to import macOS-specific modules
try:
    from AppKit import NSWorkspace, NSRunningApplication
    from ApplicationServices import (
        AXUIElementCreateSystemWide,
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
    )
    from CoreFoundation import CFEqual
    import Quartz
    MACOS_AVAILABLE = True
except ImportError:
    MACOS_AVAILABLE = False
    logger.warning("macOS frameworks not available — context collection disabled")


def _ax_get_attribute(element, attribute: str):
    """Safely get an accessibility attribute from a UI element."""
    try:
        err, value = AXUIElementCopyAttributeValue(element, attribute, None)
        if err == 0 and value is not None:
            return value
    except Exception:
        pass
    return None


def _get_focused_text(app_element) -> str:
    """Try to extract text from the focused UI element."""
    try:
        focused = _ax_get_attribute(app_element, "AXFocusedUIElement")
        if focused is None:
            return ""

        # Try AXValue first (text fields, editors)
        value = _ax_get_attribute(focused, "AXValue")
        if value and isinstance(value, str) and len(value) > 0:
            return value[:3000]  # Limit size

        # Try AXSelectedText
        selected = _ax_get_attribute(focused, "AXSelectedText")
        if selected and isinstance(selected, str) and len(selected) > 0:
            return selected[:3000]

        # Try AXTitle
        title = _ax_get_attribute(focused, "AXTitle")
        if title and isinstance(title, str):
            return title[:3000]

        # Try AXDescription
        desc = _ax_get_attribute(focused, "AXDescription")
        if desc and isinstance(desc, str):
            return desc[:3000]

    except Exception as e:
        logger.debug(f"Could not extract focused text: {e}")

    return ""


def get_screen_context() -> Optional[dict]:
    """Capture the current screen context (active app, window, focused text)."""
    if not MACOS_AVAILABLE:
        return None

    try:
        workspace = NSWorkspace.sharedWorkspace()
        active_app = workspace.activeApplication()

        if not active_app:
            return None

        app_name = active_app.get("NSApplicationName", "Unknown")
        bundle_id = active_app.get("NSApplicationBundleIdentifier", "")
        pid = active_app.get("NSApplicationProcessIdentifier", 0)

        # Skip ourselves and system apps
        skip_bundles = {
            "com.apple.loginwindow",
            "com.apple.SecurityAgent",
        }
        if bundle_id in skip_bundles:
            return None

        # Get window title
        app_element = AXUIElementCreateApplication(pid)
        window_title = ""
        focused_text = ""

        focused_window = _ax_get_attribute(app_element, "AXFocusedWindow")
        if focused_window:
            title = _ax_get_attribute(focused_window, "AXTitle")
            if title:
                window_title = str(title)

        # Get focused text (best-effort)
        focused_text = _get_focused_text(app_element)

        return {
            "app_name": app_name,
            "window_title": window_title,
            "focused_text": focused_text,
            "bundle_id": bundle_id,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"Context capture error: {e}")
        return None


class ContextCollector:
    """Background service that periodically captures screen context."""

    def __init__(self, interval: int = 30, on_context: Callable = None):
        self.interval = interval  # seconds between captures
        self.on_context = on_context  # callback(context_dict)
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._last_context: Optional[dict] = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def last_context(self) -> Optional[dict]:
        return self._last_context

    def start(self):
        """Start the background context collection."""
        if not MACOS_AVAILABLE:
            logger.warning("Context collection not available on this platform")
            return

        if self._running:
            return

        self._running = True
        self._paused = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"Context collector started (interval: {self.interval}s)")

    def stop(self):
        """Stop the background context collection."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Context collector stopped")

    def pause(self):
        """Pause context collection without stopping the thread."""
        self._paused = True
        logger.info("Context collector paused")

    def resume(self):
        """Resume context collection."""
        self._paused = False
        logger.info("Context collector resumed")

    def _run_loop(self):
        """Main collection loop (runs in background thread)."""
        while self._running:
            if not self._paused:
                ctx = get_screen_context()
                if ctx:
                    # De-duplicate: skip if same as last capture
                    if (self._last_context and
                        ctx["app_name"] == self._last_context.get("app_name") and
                        ctx["window_title"] == self._last_context.get("window_title") and
                        ctx.get("focused_text", "")[:100] == self._last_context.get("focused_text", "")[:100]):
                        pass  # Skip duplicate
                    else:
                        self._last_context = ctx
                        if self.on_context:
                            try:
                                self.on_context(ctx)
                            except Exception as e:
                                logger.error(f"Context callback error: {e}")

            time.sleep(self.interval)
