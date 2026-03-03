"""
Daemon Observers — type-specific observation functions for daemon agents.

Each observer is an async function that takes a DaemonConfig and returns
an Observation capturing the current state of the monitored resource.

Observers:
  - system_monitor: CPU, memory, disk via psutil
  - repo_watcher: GitHub API polling for new commits/PRs/issues
  - ci_monitor: GitHub Actions workflow status
"""

import logging
from typing import Callable

from agent.daemon import DaemonConfig, DaemonType, Observation

logger = logging.getLogger("brain.agent.daemon_observers")


# ── System Monitor Observer ──────────────────────────────────────────


async def system_monitor_observer(config: DaemonConfig) -> Observation:
    """Monitor system resources using psutil."""
    try:
        import psutil
    except ImportError:
        return Observation(
            source=config.daemon_type.value,
            content="psutil not available — cannot monitor system resources",
            is_anomaly=False,
        )

    cpu_pct = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    content = (
        f"CPU: {cpu_pct}% | "
        f"Memory: {mem.percent}% ({mem.available // (1024**2)} MB free) | "
        f"Disk: {disk.percent}% ({disk.free // (1024**3)} GB free)"
    )

    # Flag anomalies
    is_anomaly = cpu_pct > 90 or mem.percent > 90 or disk.percent > 90

    return Observation(
        source=config.daemon_type.value,
        content=content,
        metadata={
            "cpu_percent": cpu_pct,
            "memory_percent": mem.percent,
            "disk_percent": disk.percent,
        },
        is_anomaly=is_anomaly,
    )


# ── Repo Watcher Observer ───────────────────────────────────────────


async def repo_watcher_observer(config: DaemonConfig) -> Observation:
    """Poll GitHub API for recent repository activity."""
    import aiohttp

    repo = config.watch_target  # e.g., "owner/repo"
    if not repo:
        return Observation(
            source=config.daemon_type.value,
            content="No repository configured in watch_target",
        )

    headers = {"Accept": "application/vnd.github.v3+json"}
    events = []

    try:
        async with aiohttp.ClientSession() as session:
            # Check recent commits on default branch
            url = f"https://api.github.com/repos/{repo}/commits?per_page=3"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for commit in data[:3]:
                        sha = commit.get("sha", "")[:7]
                        msg = commit.get("commit", {}).get("message", "")[:80]
                        events.append(f"commit {sha}: {msg}")

            # Check open PRs
            url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=3"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    prs = await resp.json()
                    for pr in prs[:3]:
                        events.append(f"PR #{pr['number']}: {pr['title'][:60]}")

    except Exception as e:
        return Observation(
            source=config.daemon_type.value,
            content=f"GitHub API error: {e}",
            is_anomaly=False,
        )

    content = " | ".join(events) if events else "No recent activity"
    return Observation(
        source=config.daemon_type.value,
        content=content,
        metadata={"event_count": len(events)},
        is_anomaly=False,
    )


# ── CI Monitor Observer ──────────────────────────────────────────────


async def ci_monitor_observer(config: DaemonConfig) -> Observation:
    """Poll GitHub Actions for recent workflow run status."""
    import aiohttp

    repo = config.watch_target
    if not repo:
        return Observation(
            source=config.daemon_type.value,
            content="No repository configured",
        )

    headers = {"Accept": "application/vnd.github.v3+json"}

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=5"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return Observation(
                        source=config.daemon_type.value,
                        content=f"GitHub Actions API returned {resp.status}",
                    )
                data = await resp.json()

        runs = data.get("workflow_runs", [])
        if not runs:
            return Observation(
                source=config.daemon_type.value,
                content="No recent CI runs found",
            )

        summaries = []
        failures = 0
        for run in runs[:5]:
            status = run.get("conclusion", run.get("status", "unknown"))
            name = run.get("name", "unknown")[:40]
            summaries.append(f"{name}: {status}")
            if status == "failure":
                failures += 1

        content = " | ".join(summaries)
        return Observation(
            source=config.daemon_type.value,
            content=content,
            metadata={"total_runs": len(runs), "failures": failures},
            is_anomaly=failures > 0,
        )

    except Exception as e:
        return Observation(
            source=config.daemon_type.value,
            content=f"CI monitor error: {e}",
        )


# ── Generic Observer (fallback) ──────────────────────────────────────


async def generic_observer(config: DaemonConfig) -> Observation:
    """Generic observer that records a timestamp observation."""
    return Observation(
        source=config.daemon_type.value,
        content=f"Observation from {config.name} watching {config.watch_target}",
    )


# ── Observer Registry ────────────────────────────────────────────────


OBSERVER_MAP: dict[str, Callable] = {
    DaemonType.SYSTEM_MONITOR.value: system_monitor_observer,
    DaemonType.REPO_WATCHER.value: repo_watcher_observer,
    DaemonType.CI_MONITOR.value: ci_monitor_observer,
    DaemonType.CUSTOM.value: generic_observer,
    DaemonType.DATA_MONITOR.value: generic_observer,
    DaemonType.INBOX_MONITOR.value: generic_observer,
}
