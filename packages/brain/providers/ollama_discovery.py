"""
Ollama LAN Discovery — scans the local network for Ollama instances.

Probes every host on the machine's local subnet on port 11434 in parallel,
queries each responding host for its installed models, and ranks them so
the most capable instance is used first.

Usage (from anywhere):
    from providers.ollama_discovery import ollama_discovery
    best_url = await ollama_discovery.get_best_host()
    all_hosts = await ollama_discovery.get_all_hosts()
    await ollama_discovery.refresh()   # Force re-scan

Environment variables:
    OLLAMA_HOST            — if set, used directly without scanning
    OLLAMA_SCAN_SUBNET     — CIDR to scan (default: auto-detected from default route)
    OLLAMA_SCAN_TIMEOUT    — per-host probe timeout in seconds (default: 1.5)
    OLLAMA_SCAN_INTERVAL   — seconds between background re-scans (default: 120)
    OLLAMA_DISABLE_SCAN    — set to '1' to disable scanning entirely
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import subprocess
import time
from typing import Optional

import aiohttp

logger = logging.getLogger("brain.providers.ollama_discovery")

_OLLAMA_PORT = 11434
_SCAN_TIMEOUT = float(os.getenv("OLLAMA_SCAN_TIMEOUT", "1.5"))
_SCAN_INTERVAL = int(os.getenv("OLLAMA_SCAN_INTERVAL", "120"))
_DISABLE_SCAN = os.getenv("OLLAMA_DISABLE_SCAN", "0") == "1"


# ── Model capability scoring ──────────────────────────────────────────────────

def _score_model(model_name: str) -> int:
    """Assign a capability score to a model tag for ranking hosts.
    
    Higher score = more capable. We prefer hosts with larger models so the
    most powerful machine on the network is selected first.
    """
    m = model_name.lower()
    # Cloud-relay tags score low (fragile, rate-limited)
    if ":cloud" in m:
        return 1

    # Extract parameter count if present (e.g. qwen3:70b → 70)
    param_match = re.search(r":(\d+)b", m)
    if param_match:
        return int(param_match.group(1))

    # Known large-model families without explicit param tag
    if any(x in m for x in ("70b", "72b", "65b", "34b", "32b")):
        return 60
    if any(x in m for x in ("13b", "14b", "20b")):
        return 14
    if any(x in m for x in ("7b", "8b")):
        return 8
    if any(x in m for x in ("3b", "4b")):
        return 4
    return 5  # Unknown size — moderate score


def _host_score(models: list[dict]) -> int:
    """Total capability score for a host based on its best model."""
    if not models:
        return 0
    return max(_score_model(m.get("name", "")) for m in models)


# ── Subnet detection ──────────────────────────────────────────────────────────

def _detect_local_subnet() -> Optional[str]:
    """Detect the machine's primary local subnet in CIDR notation.
    
    Returns something like '192.168.1.0/24' or None on failure.
    """
    # Try env override first
    env_subnet = os.getenv("OLLAMA_SCAN_SUBNET", "")
    if env_subnet:
        return env_subnet

    # Method 1: Use `ip route` (Linux / Docker containers)
    try:
        out = subprocess.check_output(
            ["ip", "route", "show", "default"], timeout=3, stderr=subprocess.DEVNULL
        ).decode()
        # Try to get the src address
        src_match = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
        if src_match:
            ip = src_match.group(1)
            # Default to /24 subnet
            parts = ip.split(".")
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    except Exception:
        pass

    # Method 2: Use hostname resolution
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if not ip.startswith("127."):
            parts = ip.split(".")
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    except Exception:
        pass

    # Method 3: Try common home network ranges
    for test_subnet in ("192.168.1.0/24", "192.168.0.0/24", "10.0.0.0/24"):
        return test_subnet  # Return the first as a reasonable default

    return None


# ── Discovery core ────────────────────────────────────────────────────────────

async def _probe_host(session: aiohttp.ClientSession, host: str) -> Optional[dict]:
    """Probe a single host for an Ollama instance. Returns host info or None."""
    url = f"http://{host}:{_OLLAMA_PORT}/api/tags"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=_SCAN_TIMEOUT)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            models = [
                {"name": m["name"], "size": m.get("size", 0)}
                for m in data.get("models", [])
            ]
            score = _host_score(models)
            logger.debug(f"Ollama found at {host}: {[m['name'] for m in models]}")
            return {
                "url": f"http://{host}:{_OLLAMA_PORT}",
                "host": host,
                "models": models,
                "score": score,
            }
    except Exception:
        return None


async def _scan_subnet(subnet: str) -> list[dict]:
    """Scan all hosts in a /24 subnet for Ollama. Returns ranked list."""
    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError as e:
        logger.warning(f"Ollama discovery: invalid subnet '{subnet}': {e}")
        return []

    hosts = [str(h) for h in network.hosts()]
    logger.info(f"Ollama discovery: scanning {len(hosts)} hosts on {subnet} for port {_OLLAMA_PORT}...")

    connector = aiohttp.TCPConnector(limit=64)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [_probe_host(session, h) for h in hosts]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    found = [r for r in results if isinstance(r, dict) and r is not None]
    found.sort(key=lambda h: h["score"], reverse=True)
    logger.info(
        f"Ollama discovery: found {len(found)} instance(s) — "
        + ", ".join(f"{h['host']}(score={h['score']})" for h in found[:5])
    )
    return found


# ── Also probe well-known addresses ──────────────────────────────────────────

async def _probe_known_hosts(session: aiohttp.ClientSession) -> list[dict]:
    """Probe always-checked addresses even if outside the scanned subnet."""
    if os.getenv("KESTREL_RUNTIME_MODE", "").lower() in {"native", "local"}:
        known = [
            "localhost",
            "127.0.0.1",
            "host.docker.internal",
            "172.17.0.1",
        ]
    else:
        known = [
            "host.docker.internal",  # Docker host (macOS/Windows)
            "localhost",
            "127.0.0.1",
            "172.17.0.1",            # Docker bridge gateway
        ]
    tasks = [_probe_host(session, h) for h in known]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


# ── OllamaDiscovery singleton ─────────────────────────────────────────────────

class OllamaDiscovery:
    """
    Discovers and tracks Ollama instances on the local network.

    On first call to get_best_host(), performs a full LAN scan.
    A background task re-scans every OLLAMA_SCAN_INTERVAL seconds so
    kestrel automatically picks up new machines that come online.
    """

    def __init__(self):
        self._hosts: list[dict] = []
        self._scanned_at: float = 0.0
        self._lock = asyncio.Lock()
        self._bg_task: Optional[asyncio.Task] = None
        self._static_host = os.getenv("OLLAMA_HOST", "")

    # ── Public API ────────────────────────────────────────────────────

    async def get_best_host(self) -> str:
        """Return the URL of the best available Ollama instance.
        
        Falls back to OLLAMA_HOST env var if scanning is disabled or fails.
        """
        if _DISABLE_SCAN or self._static_host:
            default_host = "http://127.0.0.1" if os.getenv("KESTREL_RUNTIME_MODE", "").lower() in {"native", "local"} else "http://host.docker.internal"
            return self._static_host or f"{default_host}:{_OLLAMA_PORT}"

        await self._ensure_scanned()

        if self._hosts:
            return self._hosts[0]["url"]
        default_host = "http://127.0.0.1" if os.getenv("KESTREL_RUNTIME_MODE", "").lower() in {"native", "local"} else "http://host.docker.internal"
        return f"{default_host}:{_OLLAMA_PORT}"

    async def get_all_hosts(self) -> list[dict]:
        """Return all discovered Ollama instances, ranked best-first."""
        if _DISABLE_SCAN or self._static_host:
            return []
        await self._ensure_scanned()
        return list(self._hosts)

    async def refresh(self) -> list[dict]:
        """Force a fresh network scan regardless of cache TTL."""
        async with self._lock:
            self._scanned_at = 0.0
        return await self.get_all_hosts()

    def start_background_scanning(self) -> None:
        """Start a background task that periodically re-scans the network."""
        if _DISABLE_SCAN or self._static_host:
            return
        if self._bg_task and not self._bg_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._bg_task = loop.create_task(self._bg_scan_loop())
            logger.info("Ollama discovery: background scanning started")
        except RuntimeError:
            pass  # No event loop — will scan on first request

    def get_cached_hosts(self) -> list[dict]:
        """Return cached hosts synchronously (may be empty if not yet scanned)."""
        return list(self._hosts)

    def set_static_host(self, host: str) -> None:
        """Set a static host explicitly and stop any background scanning."""
        self._static_host = host
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            logger.info(f"Ollama discovery: background scan cancelled, using static host {host}")

    # ── Private ───────────────────────────────────────────────────────

    async def _ensure_scanned(self) -> None:
        """Scan if we haven't scanned recently."""
        now = time.time()
        if now - self._scanned_at < _SCAN_INTERVAL and self._hosts:
            return
        async with self._lock:
            now = time.time()
            if now - self._scanned_at < _SCAN_INTERVAL and self._hosts:
                return
            await self._do_scan()

    async def _do_scan(self) -> None:
        """Perform a full scan: known hosts + LAN subnet."""
        results: list[dict] = []

        connector = aiohttp.TCPConnector(limit=64)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Always try known/well-known addresses first (fast)
            known = await _probe_known_hosts(session)
            results.extend(known)

        # Then scan the full subnet in parallel
        subnet = _detect_local_subnet()
        if subnet:
            lan_hosts = await _scan_subnet(subnet)
            # Merge: deduplicate by IP, keeping best info
            known_ips = {h["host"] for h in results}
            for h in lan_hosts:
                if h["host"] not in known_ips:
                    results.append(h)

        # Sort by score (most capable first)
        results.sort(key=lambda h: h["score"], reverse=True)
        self._hosts = results
        self._scanned_at = time.time()

    async def _bg_scan_loop(self) -> None:
        """Background loop that re-scans every OLLAMA_SCAN_INTERVAL seconds."""
        while True:
            await asyncio.sleep(_SCAN_INTERVAL)
            try:
                async with self._lock:
                    await self._do_scan()
                if self._hosts:
                    logger.info(
                        f"Ollama discovery (bg): best host is now {self._hosts[0]['url']} "
                        f"(score={self._hosts[0]['score']})"
                    )
            except Exception as e:
                logger.warning(f"Ollama discovery background scan failed: {e}")


# Singleton
ollama_discovery = OllamaDiscovery()
