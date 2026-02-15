"""
Libre Bird — Proactive AI Analysis Engine.

Runs in the background, periodically analyzing the user's screen context
via the local LLM and sending macOS notifications when it detects a genuine
optimization opportunity.

Only fires when something is truly actionable — no spam.
"""

import asyncio
import logging
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional, List

logger = logging.getLogger("libre_bird.proactive")

# ── Analysis Prompt ──────────────────────────────────────────────────
# This prompt is critical — it must be strict about only suggesting
# when there's a genuine optimization.

ANALYSIS_PROMPT = """You are Libre Bird's proactive assistant. You are analyzing the user's current screen context to see if there's something genuinely helpful you can suggest.

CURRENT CONTEXT:
- App: {app_name}
- Window: {window_title}
- Activity type: {activity}
- Time on this activity: {activity_minutes} minutes
- Current time: {current_time}
- Recent activity pattern: {activity_summary}
{music_status}

RULES — BE EXTREMELY SELECTIVE:
1. ONLY suggest something if it provides GENUINE value. If nothing is truly useful, respond with exactly: NONE
2. Good suggestions: music for the current task, a break after 90+ min, switching to dark mode at night, relevant workflow tips
3. Bad suggestions: obvious things the user already knows, generic tips, anything annoying
4. Keep suggestions to ONE SHORT sentence (under 100 characters)
5. Never suggest the same type of thing twice in a row
6. If the user has been working less than 15 minutes on something, say NONE — let them settle in
7. Be conversational and friendly, like a thoughtful friend dropping a quick note

Previous suggestions (do NOT repeat these): {previous_suggestions}

Respond with ONLY the suggestion text (one sentence) or NONE. No explanation, no preamble."""


class ProactiveEngine:
    """Background engine that analyzes context and sends smart notifications."""

    def __init__(self, llm_engine, context_collector):
        self.llm = llm_engine
        self.ctx_collector = context_collector
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Analysis settings
        self.interval = 300  # 5 minutes between analyses
        self.min_activity_minutes = 15  # Don't bother if < 15 min on task
        self.cooldown = 1800  # 30 min cooldown between notifications

        # State tracking
        self._last_analysis_time = 0
        self._last_notification_time = 0
        self._last_context_hash = ""
        self._previous_suggestions: List[str] = []  # rolling window
        self._suggestions: List[dict] = []  # stored suggestions
        self._enabled = True

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def suggestions(self) -> List[dict]:
        return list(self._suggestions)

    def start(self, loop: asyncio.AbstractEventLoop):
        """Start the proactive analysis engine."""
        if self._running:
            return
        self._loop = loop
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"Proactive engine started (interval: {self.interval}s)")

    def stop(self):
        """Stop the proactive analysis engine."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Proactive engine stopped")

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    def dismiss_suggestion(self, index: int = -1):
        """Dismiss a suggestion by index (or the most recent one)."""
        if self._suggestions:
            if index == -1:
                self._suggestions.pop()
            elif 0 <= index < len(self._suggestions):
                self._suggestions.pop(index)

    def _context_hash(self, ctx: dict) -> str:
        """Create a rough hash of context to detect changes."""
        return f"{ctx.get('app_name', '')}|{ctx.get('window_title', '')}"

    def _should_analyze(self, ctx: dict) -> bool:
        """Determine if we should run an analysis cycle."""
        now = time.time()

        # Engine disabled
        if not self._enabled:
            return False

        # LLM not loaded
        if not self.llm.is_loaded:
            return False

        # Too soon since last analysis
        if now - self._last_analysis_time < self.interval:
            return False

        # Context hasn't changed and we analyzed recently
        ctx_hash = self._context_hash(ctx)
        if ctx_hash == self._last_context_hash and now - self._last_analysis_time < self.interval * 2:
            return False

        # User hasn't been doing this long enough
        activity_min = ctx.get("activity_minutes", 0)
        if activity_min < self.min_activity_minutes:
            return False

        return True

    def _build_prompt(self, ctx: dict) -> str:
        """Build the analysis prompt with current context."""
        # Get music status
        music_status = ""
        try:
            from tools import tool_music_control
            now_playing = tool_music_control("now_playing")
            if now_playing.get("state") == "playing":
                music_status = f"- Currently playing: {now_playing.get('track', '?')} by {now_playing.get('artist', '?')} ({now_playing.get('genre', '?')})"
            else:
                music_status = "- No music playing"
        except Exception:
            music_status = "- Music status unavailable"

        # Activity summary
        activity_summary = "No activity data"
        if self.ctx_collector and hasattr(self.ctx_collector, 'activity_summary'):
            summary = self.ctx_collector.activity_summary
            if summary:
                activity_summary = ", ".join(
                    f"{cat}: {mins}m" for cat, mins in summary.items()
                )

        # Previous suggestions (last 5)
        prev = ", ".join(self._previous_suggestions[-5:]) if self._previous_suggestions else "none yet"

        now = datetime.now()

        return ANALYSIS_PROMPT.format(
            app_name=ctx.get("app_name", "Unknown"),
            window_title=ctx.get("window_title", ""),
            activity=ctx.get("activity", "other"),
            activity_minutes=ctx.get("activity_minutes", 0),
            current_time=now.strftime("%I:%M %p"),
            activity_summary=activity_summary,
            music_status=music_status,
            previous_suggestions=prev,
        )

    def _analyze(self, ctx: dict) -> Optional[str]:
        """Run the LLM analysis and return a suggestion (or None)."""
        try:
            prompt = self._build_prompt(ctx)

            # Use the model directly for a quick, low-token inference
            response = self.llm._model.create_chat_completion(
                messages=[
                    {"role": "system", "content": "You are a proactive assistant that gives brief, useful suggestions. Respond with NONE if nothing is worth suggesting."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=100,
                temperature=0.7,
                stop=["NONE\n", "\n\n"],
            )

            text = response["choices"][0]["message"]["content"].strip()

            # Clean thinking blocks
            import re
            text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

            # Check if LLM said nothing is worth suggesting
            if not text or text.upper() == "NONE" or "NONE" in text.upper():
                logger.debug("Proactive analysis: nothing to suggest")
                return None

            # Trim to one sentence
            text = text.split("\n")[0].strip()
            if len(text) > 200:
                text = text[:197] + "..."

            return text

        except Exception as e:
            logger.error(f"Proactive analysis failed: {e}")
            return None

    def _send_notification(self, suggestion: str):
        """Send a macOS notification."""
        now = time.time()

        # Cooldown check — don't spam
        if now - self._last_notification_time < self.cooldown:
            logger.debug(f"Notification cooldown active, skipping: {suggestion}")
            return

        try:
            # Escape quotes for AppleScript
            safe_text = suggestion.replace('"', '\\"').replace("'", "\\'")
            script = f'display notification "{safe_text}" with title "🐦 Libre Bird" sound name "Glass"'
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5
            )
            self._last_notification_time = now
            logger.info(f"Proactive notification sent: {suggestion}")
        except Exception as e:
            logger.error(f"Notification failed: {e}")

    def _run_loop(self):
        """Main proactive analysis loop (runs in background thread)."""
        # Wait for the app to fully start and model to load
        time.sleep(30)

        while self._running:
            try:
                if self.ctx_collector and self.ctx_collector.last_context:
                    ctx = self.ctx_collector.last_context

                    if self._should_analyze(ctx):
                        logger.debug(f"Running proactive analysis for: {ctx.get('app_name')}")
                        self._last_analysis_time = time.time()
                        self._last_context_hash = self._context_hash(ctx)

                        suggestion = self._analyze(ctx)

                        if suggestion:
                            # Store suggestion
                            self._suggestions.append({
                                "text": suggestion,
                                "timestamp": datetime.now().isoformat(),
                                "context": ctx.get("app_name", ""),
                                "dismissed": False,
                            })

                            # Keep max 20 suggestions
                            if len(self._suggestions) > 20:
                                self._suggestions = self._suggestions[-20:]

                            # Track for dedup
                            self._previous_suggestions.append(suggestion)
                            if len(self._previous_suggestions) > 10:
                                self._previous_suggestions = self._previous_suggestions[-10:]

                            # Send macOS notification
                            self._send_notification(suggestion)

            except Exception as e:
                logger.error(f"Proactive loop error: {e}")

            # Sleep in small increments so we can stop quickly
            for _ in range(int(self.interval / 5)):
                if not self._running:
                    break
                time.sleep(5)
