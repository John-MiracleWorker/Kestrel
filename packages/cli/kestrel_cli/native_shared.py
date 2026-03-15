from __future__ import annotations

import abc
import asyncio
import fnmatch
import hashlib
import json
import logging
import math
import mimetypes
import os
import platform
import re
import sqlite3
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - packaging guard
    httpx = None

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - packaging guard
    yaml = None


LOGGER = logging.getLogger("kestrel.native")
DEFAULT_CONTROL_HOST = os.getenv("KESTREL_CONTROL_HOST", "127.0.0.1")
DEFAULT_CONTROL_PORT = int(os.getenv("KESTREL_CONTROL_PORT", "8749"))


DEFAULT_CONFIG = {
    "runtime": {
        "mode": "native",
        "allow_loopback_http": False,
        "single_user": True,
    },
    "heartbeat": {
        "interval_seconds": 300,
        "quiet_hours": {
            "enabled": False,
            "start": "23:00",
            "end": "07:00",
        },
    },
    "permissions": {
        "broad_local_control": True,
        "require_approval_for_mutations": True,
    },
    "models": {
        "preferred_provider": "auto",
        "preferred_model": "",
        "ollama_url": "http://127.0.0.1:11434",
        "lmstudio_url": "http://127.0.0.1:1234",
    },
    "watch": {
        "poll_interval_seconds": 5,
    },
    "agent": {
        "max_plan_steps": 8,
        "max_step_iterations": 6,
        "max_total_tool_calls": 24,
        "max_execution_seconds": 180,
        "allow_custom_tool_scaffolding": True,
    },
    "tools": {
        "enabled_categories": ["file", "system", "web", "memory", "media", "desktop", "custom"],
    },
}


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class KestrelPaths:
    home: Path
    run_dir: Path
    logs_dir: Path
    audit_dir: Path
    state_dir: Path
    memory_dir: Path
    watchlist_dir: Path
    artifacts_dir: Path
    cache_dir: Path
    models_dir: Path
    tools_dir: Path
    control_socket: Path
    control_host: str
    control_port: int
    sqlite_db: Path
    config_yml: Path
    heartbeat_md: Path
    workspace_md: Path
    watchlist_yml: Path
    heartbeat_state_json: Path
    runtime_profile_json: Path


def resolve_paths(home_override: str | None = None) -> KestrelPaths:
    home = Path(home_override or os.getenv("KESTREL_HOME") or "~/.kestrel").expanduser()
    run_dir = home / "run"
    logs_dir = home / "logs"
    audit_dir = home / "audit"
    state_dir = home / "state"
    memory_dir = home / "memory"
    watchlist_dir = home / "watchlist"
    artifacts_dir = home / "artifacts"
    cache_dir = home / "cache"
    models_dir = home / "models"
    tools_dir = home / "tools"
    return KestrelPaths(
        home=home,
        run_dir=run_dir,
        logs_dir=logs_dir,
        audit_dir=audit_dir,
        state_dir=state_dir,
        memory_dir=memory_dir,
        watchlist_dir=watchlist_dir,
        artifacts_dir=artifacts_dir,
        cache_dir=cache_dir,
        models_dir=models_dir,
        tools_dir=tools_dir,
        control_socket=run_dir / "control.sock",
        control_host=os.getenv("KESTREL_CONTROL_HOST", DEFAULT_CONTROL_HOST),
        control_port=int(os.getenv("KESTREL_CONTROL_PORT", str(DEFAULT_CONTROL_PORT))),
        sqlite_db=state_dir / "kestrel.db",
        config_yml=home / "config.yml",
        heartbeat_md=home / "HEARTBEAT.md",
        workspace_md=home / "WORKSPACE.md",
        watchlist_yml=watchlist_dir / "paths.yml",
        heartbeat_state_json=state_dir / "heartbeat.json",
        runtime_profile_json=state_dir / "runtime_profile.json",
    )


def _write_text_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def ensure_home_layout(home_override: str | None = None) -> KestrelPaths:
    paths = resolve_paths(home_override=home_override)
    for directory in (
        paths.home,
        paths.run_dir,
        paths.logs_dir,
        paths.audit_dir,
        paths.state_dir,
        paths.memory_dir,
        paths.watchlist_dir,
        paths.artifacts_dir,
        paths.cache_dir,
        paths.models_dir,
        paths.tools_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    config_text = """runtime:
  mode: native
  allow_loopback_http: false
  single_user: true
heartbeat:
  interval_seconds: 300
  quiet_hours:
    enabled: false
    start: "23:00"
    end: "07:00"
permissions:
  broad_local_control: true
  require_approval_for_mutations: true
models:
  preferred_provider: auto
  preferred_model: ""
  ollama_url: http://127.0.0.1:11434
  lmstudio_url: http://127.0.0.1:1234
watch:
  poll_interval_seconds: 5
agent:
  max_plan_steps: 8
  max_step_iterations: 6
  max_total_tool_calls: 24
  max_execution_seconds: 180
  allow_custom_tool_scaffolding: true
tools:
  enabled_categories:
    - file
    - system
    - web
    - memory
    - media
    - desktop
    - custom
"""
    heartbeat_text = """# Kestrel Heartbeat Tasks

## Every heartbeat
- Refresh runtime profile
- Sync markdown memory
- Reindex watched files
"""
    workspace_text = """# Active Workspace Context

## Current Projects
- Describe what you are working on here.
"""
    watchlist_text = """paths:
  - ~/Downloads
  - ~/Desktop
"""
    _write_text_if_missing(paths.config_yml, config_text)
    _write_text_if_missing(paths.heartbeat_md, heartbeat_text)
    _write_text_if_missing(paths.workspace_md, workspace_text)
    _write_text_if_missing(paths.watchlist_yml, watchlist_text)
    return paths


def load_native_config(paths: KestrelPaths | None = None) -> dict[str, Any]:
    paths = paths or ensure_home_layout()
    if not paths.config_yml.exists():
        return dict(DEFAULT_CONFIG)
    if yaml is None:
        LOGGER.warning("PyYAML unavailable; using native fallback parser for %s", paths.config_yml)
        try:
            raw = _parse_simple_yaml_config(paths.config_yml.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Fallback YAML parser failed for %s: %s", paths.config_yml, exc)
            raw = {}
        return _deep_merge(DEFAULT_CONFIG, raw)
    try:
        raw = yaml.safe_load(paths.config_yml.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("Failed to parse %s: %s", paths.config_yml, exc)
        raw = {}
    return _deep_merge(DEFAULT_CONFIG, raw)


def _parse_simple_yaml_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _parse_simple_yaml_block(lines: list[str], start_index: int, indent: int) -> tuple[Any, int]:
    index = start_index
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            break
        index += 1
    if index >= len(lines):
        return {}, index

    container_is_list = lines[index].startswith(" " * indent + "- ")
    if container_is_list:
        items: list[Any] = []
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                index += 1
                continue
            current_indent = len(line) - len(line.lstrip(" "))
            if current_indent < indent:
                break
            if current_indent != indent or not stripped.startswith("- "):
                break
            item_content = stripped[2:].strip()
            index += 1
            if not item_content:
                nested, index = _parse_simple_yaml_block(lines, index, indent + 2)
                items.append(nested)
                continue
            items.append(_parse_simple_yaml_scalar(item_content))
        return items, index

    mapping: dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        current_indent = len(line) - len(line.lstrip(" "))
        if current_indent < indent:
            break
        if current_indent != indent:
            break
        key, _, remainder = stripped.partition(":")
        if not _:
            index += 1
            continue
        key = key.strip()
        remainder = remainder.strip()
        index += 1
        if not remainder:
            nested, index = _parse_simple_yaml_block(lines, index, indent + 2)
            mapping[key] = nested
        else:
            mapping[key] = _parse_simple_yaml_scalar(remainder)
    return mapping, index


def _parse_simple_yaml_config(text: str) -> dict[str, Any]:
    parsed, _ = _parse_simple_yaml_block(text.splitlines(), 0, 0)
    return parsed if isinstance(parsed, dict) else {}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, path)


def configure_daemon_logging(paths: KestrelPaths) -> None:
    if any(isinstance(handler, RotatingFileHandler) for handler in logging.getLogger().handlers):
        return
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    daemon_log = paths.logs_dir / "daemon.log"
    handler = RotatingFileHandler(
        daemon_log,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s")
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(handler)


def control_socket_available(paths: KestrelPaths | None = None) -> bool:
    paths = paths or ensure_home_layout()
    if os.name == "nt":
        try:
            with socket.create_connection((paths.control_host, paths.control_port), timeout=1):
                return True
        except OSError:
            return False
    return paths.control_socket.exists()


class ControlClientError(RuntimeError):
    pass


async def send_control_stream(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    paths: KestrelPaths | None = None,
    timeout_seconds: float = 30,
) -> Any:
    paths = paths or ensure_home_layout()
    if os.name != "nt" and not paths.control_socket.exists():
        raise ControlClientError(f"Control socket not found at {paths.control_socket}")

    request_id = str(uuid.uuid4())
    if os.name == "nt":
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(paths.control_host, paths.control_port),
            timeout=timeout_seconds,
        )
    else:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(paths.control_socket)),
            timeout=timeout_seconds,
        )
    payload = {
        "request_id": request_id,
        "method": method,
        "params": params or {},
    }
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()

    try:
        while True:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
            if not raw:
                break
            response = json.loads(raw.decode("utf-8"))
            if response.get("request_id") != request_id:
                continue
            if not response.get("ok", False):
                error = response.get("error") or {}
                raise ControlClientError(error.get("message") or "Unknown control API failure")
            yield response
            if response.get("done"):
                break
    finally:
        writer.close()
        await writer.wait_closed()


async def send_control_request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    paths: KestrelPaths | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    async for response in send_control_stream(
        method,
        params=params,
        paths=paths,
        timeout_seconds=timeout_seconds,
    ):
        if "result" in response:
            return response["result"]
    raise ControlClientError(f"No result received for {method}")


