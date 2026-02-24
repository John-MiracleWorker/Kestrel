from __future__ import annotations
"""
Daemon Agents — always-on background agents that continuously monitor,
reason about, and proactively act on data sources.

Unlike cron jobs (which fire on a schedule), daemons hold state between
observations and decide when to interrupt the user with actionable findings.

Architecture:
  DaemonAgent → ObservationBuffer → InterruptDecider → NotificationRouter
                    ↕
               Persistent state (DB)
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("brain.agent.daemon")


# ── Daemon State Machine ─────────────────────────────────────────────


class DaemonState(str, Enum):
    """Lifecycle states for a daemon agent."""
    IDLE = "idle"
    OBSERVING = "observing"
    ANALYZING = "analyzing"
    ACTING = "acting"
    PAUSED = "paused"
    STOPPED = "stopped"


class DaemonType(str, Enum):
    """Categories of daemon agents."""
    REPO_WATCHER = "repo_watcher"
    CI_MONITOR = "ci_monitor"
    INBOX_MONITOR = "inbox_monitor"
    DATA_MONITOR = "data_monitor"
    SYSTEM_MONITOR = "system_monitor"
    CUSTOM = "custom"


# ── Data Models ──────────────────────────────────────────────────────


@dataclass
class Observation:
    """A single observation captured by a daemon."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    timestamp: str = ""
    source: str = ""
    content: str = ""
    metadata: dict = field(default_factory=dict)
    is_anomaly: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "source": self.source,
            "content": self.content[:500],
            "is_anomaly": self.is_anomaly,
        }


@dataclass
class DaemonConfig:
    """Configuration for a daemon agent."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workspace_id: str = ""
    user_id: str = ""
    name: str = ""
    description: str = ""
    daemon_type: DaemonType = DaemonType.CUSTOM
    watch_target: str = ""         # What to monitor (repo URL, inbox, path, etc.)
    poll_interval_seconds: int = 300  # Default: 5 minutes
    sensitivity: str = "medium"    # low, medium, high — affects interrupt threshold
    escalation_rules: dict = field(default_factory=dict)
    state: DaemonState = DaemonState.IDLE
    created_at: str = ""
    last_observation_at: str = ""
    observation_count: int = 0
    interrupt_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "daemon_type": self.daemon_type.value,
            "watch_target": self.watch_target,
            "poll_interval_seconds": self.poll_interval_seconds,
            "sensitivity": self.sensitivity,
            "state": self.state.value,
            "last_observation_at": self.last_observation_at,
            "observation_count": self.observation_count,
            "interrupt_count": self.interrupt_count,
        }


@dataclass
class InterruptSignal:
    """A signal that should be surfaced to the user."""
    daemon_id: str
    title: str
    body: str
    severity: str = "info"         # info, warning, critical
    hypothesis: str = ""           # What the daemon thinks is happening
    recommendation: str = ""       # What the daemon suggests doing
    auto_actionable: bool = False  # Can the daemon fix it autonomously?
    goal_template: str = ""        # If auto_actionable, the goal to execute

    def to_dict(self) -> dict:
        return {
            "daemon_id": self.daemon_id,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "hypothesis": self.hypothesis,
            "recommendation": self.recommendation,
            "auto_actionable": self.auto_actionable,
        }


# ── Observation Buffer ───────────────────────────────────────────────


class ObservationBuffer:
    """
    Ring buffer of recent observations with change detection.

    Tracks the last N observations and detects meaningful changes
    between consecutive observations.
    """

    def __init__(self, max_size: int = 100):
        self._buffer: list[Observation] = []
        self._max_size = max_size
        self._baseline: Optional[str] = None  # Hash/fingerprint of normal state

    def add(self, observation: Observation) -> bool:
        """
        Add an observation. Returns True if it differs from the last one.
        """
        changed = False
        if self._buffer:
            last = self._buffer[-1]
            changed = last.content != observation.content
        else:
            changed = True  # First observation is always "changed"

        self._buffer.append(observation)
        if len(self._buffer) > self._max_size:
            self._buffer.pop(0)

        return changed

    def get_recent(self, n: int = 10) -> list[Observation]:
        return self._buffer[-n:]

    def get_anomalies(self) -> list[Observation]:
        return [o for o in self._buffer if o.is_anomaly]

    def set_baseline(self, fingerprint: str) -> None:
        self._baseline = fingerprint

    @property
    def size(self) -> int:
        return len(self._buffer)


# ── Daemon Agent ─────────────────────────────────────────────────────


class DaemonAgent:
    """
    An always-on background agent that monitors a target and generates
    interrupt signals when something actionable is detected.

    Usage:
        daemon = DaemonAgent(config, observer_fn, analyzer_fn)
        await daemon.start()  # Runs in background
    """

    def __init__(
        self,
        config: DaemonConfig,
        observer: Callable,
        analyzer: Callable = None,
        interrupt_callback: Callable = None,
    ):
        self.config = config
        self._observer = observer        # async fn(config) -> Observation
        self._analyzer = analyzer        # async fn(observations) -> list[InterruptSignal]
        self._on_interrupt = interrupt_callback
        self._buffer = ObservationBuffer()
        self._task: Optional[asyncio.Task] = None
        self._consecutive_no_change = 0

    async def start(self) -> None:
        """Start the daemon's observation loop."""
        if self._task and not self._task.done():
            logger.warning(f"Daemon {self.config.name} already running")
            return

        self.config.state = DaemonState.OBSERVING
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"Daemon '{self.config.name}' started "
            f"(poll every {self.config.poll_interval_seconds}s)"
        )

    async def stop(self) -> None:
        """Stop the daemon."""
        self.config.state = DaemonState.STOPPED
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"Daemon '{self.config.name}' stopped")

    async def pause(self) -> None:
        """Pause the daemon (keeps state, stops polling)."""
        self.config.state = DaemonState.PAUSED
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"Daemon '{self.config.name}' paused")

    async def _run_loop(self) -> None:
        """Main observation loop."""
        while self.config.state in (DaemonState.OBSERVING, DaemonState.ANALYZING):
            try:
                # 1. Observe
                self.config.state = DaemonState.OBSERVING
                observation = await self._observer(self.config)
                observation.timestamp = datetime.now(timezone.utc).isoformat()

                changed = self._buffer.add(observation)
                self.config.observation_count += 1
                self.config.last_observation_at = observation.timestamp

                if not changed:
                    self._consecutive_no_change += 1
                else:
                    self._consecutive_no_change = 0

                # 2. Analyze (if something changed or periodic deep check)
                should_analyze = (
                    changed
                    or observation.is_anomaly
                    or self._consecutive_no_change % 12 == 0  # Deep check every ~hour
                )

                if should_analyze and self._analyzer:
                    self.config.state = DaemonState.ANALYZING
                    signals = await self._analyzer(self._buffer.get_recent(20))

                    # 3. Interrupt if actionable
                    for signal in signals:
                        if self._should_interrupt(signal):
                            self.config.state = DaemonState.ACTING
                            self.config.interrupt_count += 1
                            if self._on_interrupt:
                                await self._on_interrupt(signal)
                            logger.info(
                                f"Daemon '{self.config.name}' interrupt: {signal.title}"
                            )

                self.config.state = DaemonState.OBSERVING

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Daemon '{self.config.name}' error: {e}", exc_info=True)

            # Adaptive polling: slow down if nothing is changing
            interval = self.config.poll_interval_seconds
            if self._consecutive_no_change > 10:
                interval = min(interval * 2, 3600)  # Max 1 hour

            await asyncio.sleep(interval)

    def _should_interrupt(self, signal: InterruptSignal) -> bool:
        """Decide if a signal warrants interrupting the user."""
        sensitivity_thresholds = {
            "low": {"info": False, "warning": False, "critical": True},
            "medium": {"info": False, "warning": True, "critical": True},
            "high": {"info": True, "warning": True, "critical": True},
        }
        thresholds = sensitivity_thresholds.get(
            self.config.sensitivity,
            sensitivity_thresholds["medium"],
        )
        return thresholds.get(signal.severity, True)


# ── Daemon Manager ───────────────────────────────────────────────────


class DaemonManager:
    """
    Manages the lifecycle of all daemon agents.

    Handles creation, starting, stopping, and persistence of daemons.
    """

    def __init__(self, pool=None, notification_router=None, task_launcher=None):
        self._pool = pool
        self._router = notification_router
        self._task_launcher = task_launcher
        self._daemons: dict[str, DaemonAgent] = {}

    async def create_daemon(
        self,
        workspace_id: str,
        user_id: str,
        name: str,
        description: str,
        daemon_type: str = "custom",
        watch_target: str = "",
        poll_interval: int = 300,
        sensitivity: str = "medium",
    ) -> DaemonConfig:
        """Create and start a new daemon agent."""
        config = DaemonConfig(
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            description=description,
            daemon_type=DaemonType(daemon_type),
            watch_target=watch_target,
            poll_interval_seconds=poll_interval,
            sensitivity=sensitivity,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Create observer and analyzer based on type
        observer = self._get_observer(config)
        analyzer = self._get_analyzer(config)

        daemon = DaemonAgent(
            config=config,
            observer=observer,
            analyzer=analyzer,
            interrupt_callback=self._handle_interrupt,
        )

        self._daemons[config.id] = daemon

        # Persist config
        await self._persist_daemon(config)

        # Start
        await daemon.start()

        return config

    async def stop_daemon(self, daemon_id: str) -> bool:
        """Stop a daemon."""
        daemon = self._daemons.get(daemon_id)
        if not daemon:
            return False
        await daemon.stop()
        return True

    async def pause_daemon(self, daemon_id: str) -> bool:
        """Pause a daemon."""
        daemon = self._daemons.get(daemon_id)
        if not daemon:
            return False
        await daemon.pause()
        return True

    def list_daemons(self, workspace_id: str = "") -> list[dict]:
        """List all active daemons."""
        daemons = []
        for daemon in self._daemons.values():
            if workspace_id and daemon.config.workspace_id != workspace_id:
                continue
            daemons.append(daemon.config.to_dict())
        return daemons

    async def _handle_interrupt(self, signal: InterruptSignal) -> None:
        """Handle an interrupt signal from a daemon."""
        # Route to notification system
        if self._router:
            daemon = self._daemons.get(signal.daemon_id)
            if daemon:
                await self._router.send(
                    user_id=daemon.config.user_id,
                    workspace_id=daemon.config.workspace_id,
                    type=signal.severity,
                    title=signal.title,
                    body=signal.body,
                    source=f"daemon:{daemon.config.name}",
                )

        # If auto-actionable and has a goal template, launch a task
        if signal.auto_actionable and signal.goal_template and self._task_launcher:
            daemon = self._daemons.get(signal.daemon_id)
            if daemon:
                await self._task_launcher(
                    workspace_id=daemon.config.workspace_id,
                    user_id=daemon.config.user_id,
                    goal=signal.goal_template,
                    source=f"daemon:{daemon.config.name}",
                )

    def _get_observer(self, config: DaemonConfig) -> Callable:
        """Get the appropriate observer function for a daemon type."""

        async def generic_observer(cfg: DaemonConfig) -> Observation:
            """Generic observer that just records a timestamp."""
            return Observation(
                source=cfg.daemon_type.value,
                content=f"Observation from {cfg.name} watching {cfg.watch_target}",
            )

        # Can be extended with type-specific observers:
        # REPO_WATCHER → poll GitHub API
        # CI_MONITOR → poll GitHub Actions
        # SYSTEM_MONITOR → psutil checks
        return generic_observer

    def _get_analyzer(self, config: DaemonConfig) -> Callable:
        """Get the appropriate analyzer function for a daemon type."""

        async def generic_analyzer(observations: list[Observation]) -> list[InterruptSignal]:
            """Generic analyzer that checks for anomalies."""
            signals = []
            for obs in observations:
                if obs.is_anomaly:
                    signals.append(InterruptSignal(
                        daemon_id=config.id,
                        title=f"Anomaly detected by {config.name}",
                        body=obs.content[:200],
                        severity="warning",
                    ))
            return signals

        return generic_analyzer

    async def _persist_daemon(self, config: DaemonConfig) -> None:
        """Save daemon config to the database."""
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO daemon_agents (id, workspace_id, user_id, name, description,
                        daemon_type, watch_target, poll_interval_seconds, sensitivity,
                        state, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state
                    """,
                    config.id, config.workspace_id, config.user_id,
                    config.name, config.description, config.daemon_type.value,
                    config.watch_target, config.poll_interval_seconds,
                    config.sensitivity, config.state.value, config.created_at,
                )
        except Exception as e:
            logger.debug(f"Daemon persistence failed (table may not exist): {e}")

    async def load_daemons(self, workspace_id: str = "") -> None:
        """Load persisted daemons from the database and restart them."""
        if not self._pool:
            return
        try:
            query = "SELECT * FROM daemon_agents WHERE state != 'stopped'"
            args = []
            if workspace_id:
                query += " AND workspace_id = $1"
                args.append(workspace_id)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, *args)

            for row in rows:
                config = DaemonConfig(
                    id=row["id"],
                    workspace_id=row["workspace_id"],
                    user_id=row["user_id"],
                    name=row["name"],
                    description=row["description"],
                    daemon_type=DaemonType(row["daemon_type"]),
                    watch_target=row.get("watch_target", ""),
                    poll_interval_seconds=row.get("poll_interval_seconds", 300),
                    sensitivity=row.get("sensitivity", "medium"),
                    created_at=row.get("created_at", ""),
                )
                observer = self._get_observer(config)
                analyzer = self._get_analyzer(config)
                daemon = DaemonAgent(
                    config=config,
                    observer=observer,
                    analyzer=analyzer,
                    interrupt_callback=self._handle_interrupt,
                )
                self._daemons[config.id] = daemon
                await daemon.start()

            if rows:
                logger.info(f"Loaded and started {len(rows)} persisted daemons")
        except Exception as e:
            logger.debug(f"No persisted daemons loaded: {e}")
