from __future__ import annotations
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
        """Generate a dedup fingerprint from source + title + 5-minute time bucket.

        The time bucket ensures the same signal received in a different 5-minute
        window gets a fresh fingerprint and is not blocked by the cooldown.
        """
        time_bucket = int(time.time() // 300)
        raw = f"{self.source.value}:{self.source_id}:{self.title}:{time_bucket}"
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
        """Form a hypothesis from a signal. Uses LLM when available, falls back to rules."""
        if not self._provider:
            return self._rule_based_hypothesis(signal)

        try:
            import json as _json
            import re as _re

            prompt = (
                f"Analyze this system signal and generate a hypothesis:\n"
                f"Source: {signal.source.value}\n"
                f"Title: {signal.title}\n"
                f"Body: {signal.body}\n"
                f"Severity: {signal.severity}\n\n"
                f"Respond with JSON only:\n"
                f'{{"title": "...", "explanation": "...", "confidence": 0.X, '
                f'"recommendation": "...", "auto_actionable": true/false, '
                f'"goal_template": "..."}}'
            )

            response = await self._provider.generate(
                messages=[{"role": "user", "content": prompt}],
                model=self._model,
                temperature=0.3,
                max_tokens=512,
            )

            raw = response if isinstance(response, str) else response.get("content", "")
            raw = _re.sub(r"```json\s*", "", raw)
            raw = _re.sub(r"```\s*$", "", raw)
            data = _json.loads(raw.strip())

            return Hypothesis(
                id=signal.fingerprint,
                title=data.get("title", signal.title),
                explanation=data.get("explanation", signal.body),
                confidence=float(data.get("confidence", 0.6)),
                signals=[signal.fingerprint],
                recommendation=data.get("recommendation", f"Investigate: {signal.title}"),
                auto_actionable=bool(data.get("auto_actionable", False)),
                goal_template=data.get("goal_template", ""),
            )
        except Exception as e:
            logger.warning(f"LLM hypothesis generation failed, using rules: {e}")
            return self._rule_based_hypothesis(signal)

    def _rule_based_hypothesis(self, signal: Signal) -> Hypothesis:
        """Fast rule-based fallback for hypothesis generation."""
        # Sanitize signal content before embedding into a task goal to prevent
        # prompt injection from external signal sources (fs events, monitors, etc.)
        safe_title = signal.title[:120].replace("\n", " ").replace("\r", "")
        safe_body = signal.body[:200].replace("\n", " ").replace("\r", "")
        return Hypothesis(
            id=signal.fingerprint,
            title=signal.title,
            explanation=signal.body,
            confidence=0.7 if signal.severity == "critical" else 0.5,
            signals=[signal.fingerprint],
            recommendation=f"Investigate: {safe_title}",
            auto_actionable=signal.severity == "critical",
            goal_template=(
                f"[System-generated] Investigate and resolve the following detected event. "
                f"Event title: {safe_title}. "
                f"Event details: {safe_body}"
            ),
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


class HeartbeatEngine:
    """
    OpenClaw Upgrade: Periodic Heartbeat Scheduler.
    Wakes up the agent periodically to assess state, run sweeps, execute
    routine maintenance, and continue background tasks without user prompting.
    """
    def __init__(self, pool, task_launcher, interval_seconds: int = 3600, opportunity_engine=None, session_manager=None):
        self._pool = pool
        self._task_launcher = task_launcher
        self._interval = interval_seconds
        self._opportunity_engine = opportunity_engine
        self._session_manager = session_manager
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Heartbeat engine started (interval: {self._interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Heartbeat engine stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                await self._run_sweeps()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat loop error: {e}")

    async def _run_sweeps(self) -> None:
        """Query DB for active workspaces and enqueue the best opportunities."""
        if not self._opportunity_engine:
            return

        # Quiet hours and idle detection
        try:
            import yaml
            import os
            from datetime import datetime
            config_path = os.path.expanduser("~/.kestrel/config.yml")
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    config = yaml.safe_load(f) or {}
                    
                q_hours = config.get("heartbeat", {}).get("quiet_hours", {})
                if q_hours:
                    start = q_hours.get("start")
                    end = q_hours.get("end")
                    if start and end:
                        now = datetime.now()
                        current_time = now.strftime("%H:%M")
                        if start <= end:
                            in_quiet_hours = start <= current_time <= end
                        else:
                            in_quiet_hours = current_time >= start or current_time <= end
                            
                        if in_quiet_hours:
                            logger.info(f"Heartbeat skipped: Quiet hours ({start} - {end})")
                            return
        except Exception as e:
            logger.warning(f"Error checking quiet hours: {e}")

        try:
            from agent.core.heartbeat_parser import HeartbeatParser
            parser = HeartbeatParser()
            heartbeat_tasks = parser.parse()
        except Exception as e:
            logger.warning(f"Failed to parse Heartbeat tasks: {e}")
            heartbeat_tasks = []

        try:
            if self._session_manager:
                pruned = await self._session_manager.prune_inactive_sessions(limit=50)
                if pruned.get("pruned_count"):
                    logger.info(f"Heartbeat pruned {pruned['pruned_count']} inactive session(s)")

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT wm.workspace_id, wm.user_id
                    FROM workspace_members wm
                    WHERE wm.role = 'owner'
                    ORDER BY wm.workspace_id
                    LIMIT 50
                    """
                )

            for row in rows:
                ws_id = str(row["workspace_id"])
                user_id = str(row["user_id"])

                # Bi-directional memory sync: ingest manual edits from markdown
                try:
                    from agent.core.markdown_memory import LocalMarkdownMemoryManager
                    from agent.core.memory_graph import MemoryGraph
                    mg = MemoryGraph(self._pool)
                    mm = LocalMarkdownMemoryManager(mg)
                    await mm.ingest_from_disk(ws_id)
                except Exception as e:
                    logger.warning(f"Failed to ingest markdown memory during heartbeat: {e}")

                async with self._pool.acquire() as conn:
                    stale_count = await conn.fetchval(
                        """
                        SELECT COUNT(*)
                        FROM task_queue
                        WHERE workspace_id = $1
                          AND (
                              (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= now())
                              OR (status = 'queued' AND created_at < now() - interval '6 hours')
                          )
                        """,
                        ws_id,
                    )

                if stale_count:
                    stale_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
                    await self._opportunity_engine.record_opportunity(
                        workspace_id=ws_id,
                        source="heartbeat",
                        title="Recover stalled queued work",
                        goal_template=(
                            f"Review {stale_count} stalled queued or running tasks in this workspace, "
                            "recover resumable work, cancel dead runs, and queue any necessary follow-up."
                        ),
                        score=0.95,
                        severity="warning",
                        dedupe_key=f"heartbeat:stalled:{ws_id}:{stale_bucket}",
                        payload_json={"stale_count": int(stale_count), "bucket": stale_bucket},
                    )

                now = datetime.now(timezone.utc)
                for heartbeat_task in heartbeat_tasks:
                    bucket = now.strftime("%Y-%m-%d")
                    score = 0.7
                    if heartbeat_task.frequency == "hourly":
                        bucket = now.strftime("%Y-%m-%dT%H")
                        score = 0.8
                    elif heartbeat_task.frequency == "every":
                        bucket = now.strftime("%Y-%m-%dT%H")
                        score = 0.85

                    fingerprint = hashlib.md5(
                        f"{ws_id}:{heartbeat_task.frequency}:{heartbeat_task.description}:{bucket}".encode()
                    ).hexdigest()[:12]
                    await self._opportunity_engine.record_opportunity(
                        workspace_id=ws_id,
                        source="heartbeat",
                        title=heartbeat_task.description[:120],
                        goal_template=heartbeat_task.description,
                        score=score,
                        severity="info",
                        dedupe_key=f"heartbeat:{heartbeat_task.frequency}:{fingerprint}",
                        payload_json={
                            "frequency": heartbeat_task.frequency,
                            "bucket": bucket,
                        },
                    )

                queued = await self._opportunity_engine.enqueue_best(
                    workspace_id=ws_id,
                    user_id=user_id,
                    limit=1,
                )
                if queued:
                    logger.debug(f"Queued {len(queued)} heartbeat opportunity for workspace {ws_id}")

        except Exception as e:
            logger.error(f"Failed to run heartbeat sweeps: {e}")
