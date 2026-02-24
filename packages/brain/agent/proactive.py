"""
Proactive Interrupt Engine — anomaly detection, hypothesis generation,
and intelligent interruption routing.

Receives signals from daemons, monitors, and predictors, then decides
when and how to interrupt the user with actionable recommendations.
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("brain.agent.proactive")


# ── Signal Types ─────────────────────────────────────────────────────


class SignalSource(str, Enum):
    DAEMON = "daemon"
    MONITOR = "monitor"
    PREDICTOR = "predictor"
    SYSTEM = "system"
    USER_PATTERN = "user_pattern"


class InterruptChannel(str, Enum):
    """How to deliver the interrupt."""
    NOTIFICATION = "notification"    # Web push notification
    CHAT_MESSAGE = "chat_message"    # Inject into active chat
    TASK_CREATION = "task_creation"  # Auto-create a task


# ── Data Models ──────────────────────────────────────────────────────


@dataclass
class Signal:
    """A raw signal from any source."""
    source: SignalSource
    source_id: str = ""            # e.g. daemon ID or monitor name
    title: str = ""
    body: str = ""
    severity: str = "info"         # info, warning, critical
    metadata: dict = field(default_factory=dict)
    timestamp: str = ""
    fingerprint: str = ""          # For deduplication

    def compute_fingerprint(self) -> str:
        """Generate a dedup fingerprint from source + title."""
        raw = f"{self.source.value}:{self.source_id}:{self.title}"
        self.fingerprint = hashlib.md5(raw.encode()).hexdigest()[:12]
        return self.fingerprint


@dataclass
class Hypothesis:
    """A hypothesis formed from one or more signals."""
    id: str = ""
    title: str = ""
    explanation: str = ""
    confidence: float = 0.0        # 0.0 to 1.0
    signals: list[str] = field(default_factory=list)  # Signal fingerprints
    recommendation: str = ""
    auto_actionable: bool = False
    goal_template: str = ""        # If auto_actionable, task goal

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "explanation": self.explanation,
            "confidence": self.confidence,
            "recommendation": self.recommendation,
            "auto_actionable": self.auto_actionable,
        }


@dataclass
class Interrupt:
    """A decision to interrupt the user."""
    hypothesis: Hypothesis
    channel: InterruptChannel
    priority: int = 0              # Higher = more urgent
    workspace_id: str = ""
    user_id: str = ""
    created_at: str = ""
    delivered: bool = False


# ── Anomaly Detection ────────────────────────────────────────────────


class AnomalyDetector:
    """
    Pattern-based anomaly detection from signal streams.

    Detects:
      - Frequency spikes (same signal type appearing too often)
      - Novel signals (never seen before)
      - Severity escalation (info → warning → critical pattern)
    """

    def __init__(self):
        self._history: dict[str, list[float]] = {}  # fingerprint → timestamps
        self._seen_types: set[str] = set()

    def check(self, signal: Signal) -> bool:
        """Returns True if the signal is anomalous."""
        fp = signal.fingerprint or signal.compute_fingerprint()
        now = time.time()

        # Track history
        if fp not in self._history:
            self._history[fp] = []
        self._history[fp].append(now)

        # Prune old entries (keep last hour)
        cutoff = now - 3600
        self._history[fp] = [t for t in self._history[fp] if t > cutoff]

        # Check: novel signal
        type_key = f"{signal.source.value}:{signal.source_id}"
        if type_key not in self._seen_types:
            self._seen_types.add(type_key)
            return True  # First time seeing this source

        # Check: frequency spike (> 10 in the last hour)
        if len(self._history[fp]) > 10:
            return True

        # Check: critical severity is always anomalous
        if signal.severity == "critical":
            return True

        return False


# ── Proactive Engine ─────────────────────────────────────────────────


class ProactiveEngine:
    """
    Central engine for processing signals into actionable interrupts.

    Flow:
      Signals → AnomalyDetector → HypothesisGenerator → InterruptRouter

    Usage:
        engine = ProactiveEngine(notification_router, task_launcher)
        await engine.process_signal(signal)
    """

    def __init__(
        self,
        notification_router=None,
        task_launcher: Callable = None,
        llm_provider=None,
        model: str = "",
    ):
        self._router = notification_router
        self._task_launcher = task_launcher
        self._provider = llm_provider
        self._model = model
        self._detector = AnomalyDetector()
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._cooldown: dict[str, float] = {}  # fingerprint → last interrupt time
        self._cooldown_seconds = 300           # 5 min dedup window
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the interrupt processing loop."""
        self._running = True
        self._processor_task = asyncio.create_task(self._process_loop())
        logger.info("Proactive engine started")

    async def stop(self) -> None:
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

    async def process_signal(self, signal: Signal) -> None:
        """
        Ingest a signal. If anomalous, generate hypothesis and queue interrupt.
        """
        signal.compute_fingerprint()

        # Check cooldown
        now = time.time()
        last = self._cooldown.get(signal.fingerprint, 0)
        if now - last < self._cooldown_seconds:
            logger.debug(f"Signal {signal.fingerprint} in cooldown, skipping")
            return

        # Anomaly detection
        is_anomaly = self._detector.check(signal)

        if not is_anomaly and signal.severity == "info":
            return  # Normal info signal, no interrupt needed

        # Generate hypothesis
        hypothesis = await self._generate_hypothesis(signal)

        # Determine channel and priority
        channel, priority = self._route_interrupt(signal, hypothesis)

        interrupt = Interrupt(
            hypothesis=hypothesis,
            channel=channel,
            priority=priority,
            workspace_id=signal.metadata.get("workspace_id", ""),
            user_id=signal.metadata.get("user_id", ""),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Queue for delivery
        await self._queue.put((-priority, interrupt))  # Negative for max-priority first
        self._cooldown[signal.fingerprint] = now

    async def _generate_hypothesis(self, signal: Signal) -> Hypothesis:
        """Form a hypothesis from a signal (fast, rule-based)."""
        # Simple rule-based hypothesis generation
        return Hypothesis(
            id=signal.fingerprint,
            title=signal.title,
            explanation=signal.body,
            confidence=0.7 if signal.severity == "critical" else 0.5,
            signals=[signal.fingerprint],
            recommendation=f"Investigate: {signal.title}",
            auto_actionable=signal.severity == "critical",
            goal_template=f"Investigate and resolve: {signal.title}. Details: {signal.body[:200]}",
        )

    def _route_interrupt(
        self, signal: Signal, hypothesis: Hypothesis
    ) -> tuple[InterruptChannel, int]:
        """Decide how to deliver an interrupt."""
        if signal.severity == "critical":
            return InterruptChannel.NOTIFICATION, 100
        elif signal.severity == "warning":
            return InterruptChannel.NOTIFICATION, 50
        elif hypothesis.auto_actionable:
            return InterruptChannel.TASK_CREATION, 30
        else:
            return InterruptChannel.NOTIFICATION, 10

    async def _process_loop(self) -> None:
        """Process queued interrupts."""
        while self._running:
            try:
                _, interrupt = await asyncio.wait_for(
                    self._queue.get(), timeout=30
                )
                await self._deliver(interrupt)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Interrupt processing error: {e}")

    async def _deliver(self, interrupt: Interrupt) -> None:
        """Deliver an interrupt via the appropriate channel."""
        h = interrupt.hypothesis

        if interrupt.channel == InterruptChannel.NOTIFICATION and self._router:
            await self._router.send(
                user_id=interrupt.user_id,
                workspace_id=interrupt.workspace_id,
                type=("warning" if h.confidence > 0.6 else "info"),
                title=h.title,
                body=f"{h.explanation}\n\n💡 {h.recommendation}",
                source="proactive_engine",
            )

        elif interrupt.channel == InterruptChannel.TASK_CREATION and self._task_launcher:
            if h.auto_actionable and h.goal_template:
                await self._task_launcher(
                    workspace_id=interrupt.workspace_id,
                    user_id=interrupt.user_id,
                    goal=h.goal_template,
                    source="proactive_engine",
                )

        interrupt.delivered = True
        logger.info(
            f"Delivered {interrupt.channel.value} interrupt: {h.title} "
            f"(priority={interrupt.priority})"
        )
