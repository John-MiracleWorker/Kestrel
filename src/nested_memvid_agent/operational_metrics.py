from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from threading import Lock, active_count
from time import monotonic, process_time
from typing import Any, cast

from .event_log import redact_secrets
from .layers import DEFAULT_LAYER_SPECS
from .llm.factory import provider_health_id
from .llm.resilience import global_provider_health_registry

_PROCESS_STARTED = monotonic()
_MEMORY_HEALTH_LOCK = Lock()
_MEMORY_HEALTH_CACHE: dict[str, tuple[tuple[int, int, int, int], float, bool, str | None]] = {}


def operational_snapshot(*, config: Any, state: Any, runs: Any) -> dict[str, Any]:
    statuses = state.run_status_counts() if hasattr(state, "run_status_counts") else {}
    worker_statuses = state.subagent_status_counts() if hasattr(state, "subagent_status_counts") else {}
    capacity = runs.capacity_snapshot() if hasattr(runs, "capacity_snapshot") else {}
    counters = runs.operational_counters() if hasattr(runs, "operational_counters") else {}
    provider_id = provider_health_id(config)
    provider = global_provider_health_registry.snapshot(provider_id)
    poller = _telegram_poller_health()
    memory = _memory_storage(config)
    state_health = (
        state.health_snapshot()
        if hasattr(state, "health_snapshot")
        else {"ok": False, "integrity": "unavailable", "writable": False}
    )
    alerts: list[dict[str, str]] = []
    if provider["state"] in {"open", "half_open", "degraded"}:
        alerts.append({"code": "provider_degraded", "severity": "error", "provider": provider_id})
    if capacity and capacity.get("queued", 0) >= capacity.get("max_queued", 0) > 0:
        alerts.append({"code": "run_queue_saturated", "severity": "error"})
    if int(statuses.get("failed", 0)) > 0:
        alerts.append({"code": "failed_runs_present", "severity": "warning"})
    if int(worker_statuses.get("failed", 0)) > 0:
        alerts.append({"code": "failed_workers_present", "severity": "warning"})
    if poller["status"] in {"error", "stale"}:
        alerts.append({"code": "telegram_poller_unhealthy", "severity": "error"})
    if memory["max_utilization"] >= 0.9:
        alerts.append({"code": "memory_capacity_high", "severity": "warning"})
    payload = {
        "schema": "kestrel.operational_metrics.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "uptime_seconds": round(monotonic() - _PROCESS_STARTED, 3),
        "process": _process_resource_snapshot(),
        "runs": {"by_status": statuses, "capacity": capacity, "counters": counters},
        "workers": {"by_status": worker_statuses},
        "provider": provider,
        "telegram_poller": poller,
        "memory": memory,
        "state": state_health,
        "state_schema_version": state.schema_version() if hasattr(state, "schema_version") else None,
        "alerts": alerts,
    }
    return cast(dict[str, Any], redact_secrets(payload))


def _memory_storage(config: Any) -> dict[str, Any]:
    memory_dir = Path(getattr(config, "memory_dir", Path(".nest/memory")))
    limit = max(1, int(getattr(config, "memory_max_layer_bytes", 1_073_741_824)))
    layers: dict[str, dict[str, Any]] = {}
    max_utilization = 0.0
    total_bytes = 0
    invalid_layers: list[str] = []
    backend = str(getattr(config, "backend", "memory"))
    for layer, spec in DEFAULT_LAYER_SPECS.items():
        path = memory_dir / spec.mv2_file
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        integrity_ok = True
        integrity_error: str | None = None
        valid = backend != "memvid" or (path.is_file() and not path.is_symlink())
        if valid and backend == "memvid":
            integrity_ok, integrity_error = _check_memvid_layer(path, layer)
            valid = integrity_ok
        if not valid:
            invalid_layers.append(layer.value)
        utilization = size / limit
        total_bytes += size
        max_utilization = max(max_utilization, utilization)
        layers[layer.value] = {
            "bytes": size,
            "limit_bytes": limit,
            "utilization": round(utilization, 6),
            "integrity_ok": integrity_ok,
            "integrity_error": integrity_error,
        }
    writable = (
        os.access(memory_dir, os.W_OK)
        if memory_dir.exists()
        else os.access(memory_dir.parent, os.W_OK)
    )
    return {
        "layers": layers,
        "total_bytes": total_bytes,
        "max_utilization": round(max_utilization, 6),
        "available": not invalid_layers,
        "writable": writable,
        "invalid_layers": invalid_layers,
    }


def _check_memvid_layer(path: Path, layer: Any, *, cache_seconds: float = 30.0) -> tuple[bool, str | None]:
    index_path = path.with_suffix(f"{path.suffix}.records.json")
    try:
        file_stat = path.stat()
        index_stat = index_path.stat() if index_path.exists() else None
        signature = (
            file_stat.st_mtime_ns,
            file_stat.st_size,
            index_stat.st_mtime_ns if index_stat else 0,
            index_stat.st_size if index_stat else 0,
        )
    except OSError:
        return False, "stat_failed"
    cache_key = str(path.resolve())
    now = monotonic()
    with _MEMORY_HEALTH_LOCK:
        cached = _MEMORY_HEALTH_CACHE.get(cache_key)
        if cached and cached[0] == signature and now - cached[1] <= cache_seconds:
            return cached[2], cached[3]
    try:
        from .backends.memvid_backend import MemvidBackend, MemvidLockError

        backend = MemvidBackend(path, layer, read_only=True, path_lock_blocking=False)
        try:
            backend.open()
        finally:
            backend.close()
        result: tuple[bool, str | None] = (True, None)
    except MemvidLockError:
        # A live writer already owns the layer. Readiness must not deadlock an
        # active run or open a second SDK handle against the same `.mv2` file.
        result = (True, "busy")
    except Exception as exc:  # noqa: BLE001 - readiness converts backend failures to status
        result = (False, type(exc).__name__)
    with _MEMORY_HEALTH_LOCK:
        _MEMORY_HEALTH_CACHE[cache_key] = (signature, now, result[0], result[1])
    return result


def _telegram_poller_health() -> dict[str, Any]:
    path = Path(
        os.environ.get(
            "KESTREL_TELEGRAM_HEALTH_PATH",
            str(Path.home() / ".kestrel" / "telegram-poller-health.json"),
        )
    )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated = float(raw.get("updated_at_epoch", 0.0))
        age = max(0.0, datetime.now(UTC).timestamp() - updated)
        status = str(raw.get("status", "unknown"))
        if age > 90 and status not in {"stopped", "not_configured"}:
            status = "stale"
        return {
            "status": status,
            "age_seconds": round(age, 3),
            "pid": raw.get("pid"),
            "error_type": raw.get("error_type"),
        }
    except FileNotFoundError:
        return {"status": "not_configured"}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "error", "error_type": "invalid_health_file"}


def readiness_snapshot(*, config: Any, state: Any, runs: Any) -> dict[str, Any]:
    metrics = operational_snapshot(config=config, state=state, runs=runs)
    provider_state = str(metrics["provider"]["state"])
    nonterminal = state.list_nonterminal_runs() if hasattr(state, "list_nonterminal_runs") else []
    orphaned = [
        run.run_id
        for run in nonterminal
        if getattr(run, "status", "") == "running" and not _run_has_fresh_lease(run)
    ]
    reasons: list[str] = []
    provider_name = str(getattr(config, "provider", "unknown"))
    if provider_state == "unknown" and provider_name != "mock":
        reasons.append("provider_not_verified")
    elif provider_state in {"open", "half_open", "degraded"}:
        reasons.append("provider_not_operational")
    state_health = metrics["state"]
    if not state_health.get("ok"):
        reasons.append("state_store_unhealthy")
    capacity = metrics["runs"]["capacity"]
    if capacity and capacity.get("queued", 0) >= capacity.get("max_queued", 0) > 0:
        reasons.append("run_queue_saturated")
    memory = metrics["memory"]
    if not memory.get("available") or not memory.get("writable"):
        reasons.append("memory_store_unhealthy")
    if float(memory.get("max_utilization", 0.0)) >= 1.0:
        reasons.append("memory_capacity_exhausted")
    if _telegram_is_required(config) and metrics["telegram_poller"].get("status") != "healthy":
        reasons.append("telegram_poller_unhealthy")
    if orphaned:
        reasons.append("orphaned_running_runs")
    return {
        "ok": not reasons,
        "schema": "kestrel.readiness.v1",
        "reasons": reasons,
        "orphaned_run_ids": orphaned,
        "provider": metrics["provider"],
        "state": state_health,
        "memory": memory,
        "telegram_poller": metrics["telegram_poller"],
        "state_schema_version": metrics["state_schema_version"],
    }


def prometheus_snapshot(snapshot: dict[str, Any]) -> str:
    lines = ["# TYPE kestrel_up gauge", "kestrel_up 1"]
    process = _metric_mapping(snapshot.get("process"))
    lines.extend(
        [
            f"kestrel_process_uptime_seconds {_metric_number(snapshot.get('uptime_seconds'))}",
            f"kestrel_process_cpu_seconds {_metric_number(process.get('cpu_seconds'))}",
            f"kestrel_process_threads {_metric_number(process.get('thread_count'))}",
            f"kestrel_process_rss_bytes {_metric_number(process.get('max_rss_bytes'))}",
        ]
    )
    state = _metric_mapping(snapshot.get("state"))
    lines.append(f"kestrel_state_writable {1 if state.get('writable') else 0}")
    runs = _metric_mapping(snapshot.get("runs"))
    run_statuses = _metric_mapping(runs.get("by_status"))
    for status in sorted({"blocked", "cancelled", "completed", "failed", "queued", "running"} | set(run_statuses)):
        lines.append(
            f'kestrel_runs{{status="{_metric_label(status)}"}} {_metric_number(run_statuses.get(status, 0))}'
        )
    for name, value in sorted(_metric_mapping(runs.get("counters")).items()):
        lines.append(f'kestrel_run_operations{{operation="{_metric_label(name)}"}} {_metric_number(value)}')
    workers = _metric_mapping(snapshot.get("workers"))
    worker_statuses = _metric_mapping(workers.get("by_status"))
    for status in sorted({"cancelled", "completed", "failed", "queued", "running"} | set(worker_statuses)):
        lines.append(
            f'kestrel_workers{{status="{_metric_label(status)}"}} {_metric_number(worker_statuses.get(status, 0))}'
        )
    capacity = _metric_mapping(runs.get("capacity"))
    for name in ("active", "queued", "reserved", "max_active", "max_queued"):
        lines.append(f'kestrel_run_capacity{{kind="{name}"}} {_metric_number(capacity.get(name))}')
    provider = _metric_mapping(snapshot.get("provider"))
    for name in ("total_successes", "total_failures", "consecutive_failures", "last_latency_seconds"):
        lines.append(f'kestrel_provider_calls{{kind="{name}"}} {_metric_number(provider.get(name))}')
    memory = _metric_mapping(snapshot.get("memory"))
    lines.extend(
        [
            f"kestrel_memory_total_bytes {_metric_number(memory.get('total_bytes'))}",
            f"kestrel_memory_available {1 if memory.get('available') else 0}",
            f"kestrel_memory_writable {1 if memory.get('writable') else 0}",
        ]
    )
    poller = _metric_mapping(snapshot.get("telegram_poller"))
    lines.extend(
        [
            f"kestrel_telegram_poller_healthy {1 if poller.get('status') == 'healthy' else 0}",
            f"kestrel_telegram_poller_age_seconds {_metric_number(poller.get('age_seconds'))}",
        ]
    )
    return "\n".join(lines) + "\n"


def _metric_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _metric_number(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "0"


def _metric_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _process_resource_snapshot() -> dict[str, object]:
    payload: dict[str, object] = {
        "pid": os.getpid(),
        "cpu_seconds": round(process_time(), 3),
        "thread_count": active_count(),
    }
    try:
        resource = import_module("resource")
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = int(usage.ru_maxrss)
        payload["max_rss_bytes"] = rss if sys.platform == "darwin" else rss * 1024
    except (ImportError, AttributeError, OSError, ValueError):
        payload["max_rss_bytes"] = None
    return payload


def _run_has_fresh_lease(run: Any) -> bool:
    owner = getattr(run, "lease_owner", None)
    expires = getattr(run, "lease_expires_at", None)
    if not owner or not expires:
        return False
    parts = str(owner).split("_", 2)
    if len(parts) == 3 and parts[0] == "manager" and parts[1].isdigit():
        try:
            os.kill(int(parts[1]), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
    try:
        expiry = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry > datetime.now(UTC)


def _telegram_is_required(config: Any) -> bool:
    path = Path(getattr(config, "channel_config_path", Path(".nest/config/channels.json")))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    rows = payload.get("channels", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return False
    return any(
        isinstance(row, dict)
        and row.get("provider") == "telegram"
        and bool(row.get("enabled", True))
        for row in rows
    )
