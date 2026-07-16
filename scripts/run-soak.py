#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Kestrel API load/soak verifier")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--token-env", default="NEST_AGENT_API_TOKEN")
    parser.add_argument("--allow-overload", action="store_true")
    parser.add_argument("--min-completed", type=int)
    parser.add_argument("--max-p95", type=float)
    args = parser.parse_args()
    token = os.environ.get(args.token_env, "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [
            pool.submit(_one_run, args.base_url.rstrip("/"), headers, index, args.timeout)
            for index in range(max(1, args.runs))
        ]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - soak boundary reports failures
                results.append({"ok": False, "error": f"{type(exc).__name__}:{exc}"})

    latencies = [float(item["latency_seconds"]) for item in results if item.get("ok")]
    overloaded = [item for item in results if item.get("overloaded")]
    failures = [item for item in results if not item.get("ok") and not item.get("overloaded")]
    requested = max(1, args.runs)
    minimum_completed = args.min_completed
    if minimum_completed is None:
        minimum_completed = 1 if args.allow_overload else requested
    p95 = _percentile(latencies, 0.95) if latencies else None
    passed = (
        not failures
        and len(latencies) >= max(0, minimum_completed)
        and len(latencies) + len(overloaded) == requested
        and (args.allow_overload or not overloaded)
        and (args.max_p95 is None or (p95 is not None and p95 <= args.max_p95))
    )
    payload = {
        "schema": "kestrel.soak_result.v1",
        "requested": requested,
        "completed": len(latencies),
        "overloaded": len(overloaded),
        "failed": len(failures),
        "passed": passed,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "latency_seconds": {
            "min": round(min(latencies), 3) if latencies else None,
            "median": round(statistics.median(latencies), 3) if latencies else None,
            "p95": round(p95, 3) if p95 is not None else None,
            "max": round(max(latencies), 3) if latencies else None,
        },
        "failure_types": sorted({str(item.get("error", "unknown")).split(":", 1)[0] for item in failures}),
        "acceptance": {
            "allow_overload": args.allow_overload,
            "min_completed": max(0, minimum_completed),
            "max_p95": args.max_p95,
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if passed else 1


def _one_run(base_url: str, headers: dict[str, str], index: int, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    created = _json_request(
        f"{base_url}/api/runs",
        headers=headers,
        method="POST",
        payload={"message": f"soak probe {index}: reply with OK", "autonomy_mode": "manual"},
        timeout=min(timeout, 30.0),
    )
    if created.get("overloaded"):
        return {"ok": False, **created}
    run_id = str(created["run_id"])
    deadline = started + timeout
    while time.monotonic() < deadline:
        run = _json_request(
            f"{base_url}/api/runs/{run_id}",
            headers=headers,
            method="GET",
            payload=None,
            timeout=min(10.0, timeout),
        )
        record = run.get("run", run)
        status = str(record["status"])
        if status in {"completed", "failed", "cancelled", "blocked"}:
            return {
                "ok": status == "completed",
                "run_id": run_id,
                "status": status,
                "latency_seconds": time.monotonic() - started,
                "error": None if status == "completed" else status,
            }
        time.sleep(0.1)
    return {"ok": False, "run_id": run_id, "error": "timeout"}


def _json_request(
    url: str,
    *,
    headers: dict[str, str],
    method: str,
    payload: dict[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - operator supplies local URL
            parsed = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        if exc.code in {429, 503}:
            return {"overloaded": True, "status_code": exc.code, "detail": detail}
        raise RuntimeError(f"http_{exc.code}:{detail}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid_json_response")
    return parsed


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())
