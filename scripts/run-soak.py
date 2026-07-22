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
from typing import Any, Literal, cast

from nested_memvid_agent.security_boundary import redact_secrets

EXPECTED_MEMORY_LAYERS = frozenset(
    {"working", "episodic", "semantic", "procedural", "self", "policy"}
)
ResponseContract = Literal["exact-ok", "mock-echo"]
DEFAULT_MAX_OVERLOAD_RATIO = 0.90


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Kestrel API load/soak verifier")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--token-env", default="NEST_AGENT_API_TOKEN")
    parser.add_argument("--allow-overload", action="store_true")
    parser.add_argument(
        "--require-overload",
        action="store_true",
        help=(
            "Run a saturation gate: accept only structured capacity overloads and "
            "require at least one such rejection. This implies --allow-overload."
        ),
    )
    parser.add_argument("--min-completed", type=int)
    parser.add_argument("--max-p95", type=float)
    parser.add_argument(
        "--max-overload-ratio",
        type=float,
        default=DEFAULT_MAX_OVERLOAD_RATIO,
        help=(
            "Maximum structured capacity rejections divided by requested runs "
            f"(default: {DEFAULT_MAX_OVERLOAD_RATIO:.2f})."
        ),
    )
    parser.add_argument(
        "--min-throughput",
        type=float,
        help="Minimum accepted completed runs per load-window second.",
    )
    parser.add_argument(
        "--response-contract",
        choices=("exact-ok", "mock-echo"),
        default="exact-ok",
        help=(
            "exact-ok requires a normalized exact OK response; mock-echo is only "
            "for deterministic mock server/queue load tests"
        ),
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.max_p95 is not None and args.max_p95 <= 0:
        parser.error("--max-p95 must be greater than 0")
    if not 0.0 <= args.max_overload_ratio <= 1.0:
        parser.error("--max-overload-ratio must be between 0 and 1")
    if args.min_throughput is not None and args.min_throughput <= 0:
        parser.error("--min-throughput must be greater than 0")
    if args.min_completed is not None and args.min_completed < 1:
        parser.error("--min-completed must be at least 1")
    if args.min_completed is not None and args.min_completed > args.runs:
        parser.error("--min-completed cannot exceed --runs")
    if args.require_overload and args.runs < 2:
        parser.error("--require-overload requires at least 2 runs")
    if (
        args.require_overload
        and args.min_completed is not None
        and args.min_completed >= args.runs
    ):
        parser.error("--min-completed must be less than --runs when overload is required")
    token = os.environ.get(args.token_env, "").strip()
    response_contract = cast(ResponseContract, args.response_contract)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        preflight = _runtime_snapshot(args.base_url.rstrip("/"), headers, args.timeout)
    except Exception as exc:  # noqa: BLE001 - emit a machine-readable failed gate
        print(
            json.dumps(
                redact_secrets(
                    {
                        "schema": "kestrel.soak_result.v3",
                        "passed": False,
                        "stage": "preflight",
                        "error": f"{type(exc).__name__}:{exc}",
                    }
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    if not preflight["passed"]:
        print(
            json.dumps(
                redact_secrets(
                    {
                        "schema": "kestrel.soak_result.v3",
                        "passed": False,
                        "stage": "preflight",
                        "runtime_integrity": {"preflight": preflight},
                    }
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    started = time.monotonic()
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                _one_run,
                args.base_url.rstrip("/"),
                headers,
                index,
                args.timeout,
                response_contract,
            ): index
            for index in range(args.runs)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - soak boundary reports failures
                results.append(
                    {
                        "ok": False,
                        "probe_index": index,
                        "error": f"{type(exc).__name__}:{exc}",
                    }
                )
    load_elapsed = time.monotonic() - started

    requested = args.runs
    minimum_completed = args.min_completed
    if minimum_completed is None:
        minimum_completed = 1 if args.allow_overload or args.require_overload else requested
    try:
        postflight = _runtime_snapshot(args.base_url.rstrip("/"), headers, args.timeout)
        postflight_error = None
    except Exception as exc:  # noqa: BLE001 - soak boundary reports diagnostics
        postflight = None
        postflight_error = f"{type(exc).__name__}:{exc}"
    evaluation = _evaluate_results(
        results,
        requested=requested,
        load_elapsed_seconds=load_elapsed,
        allow_overload=args.allow_overload,
        require_overload=args.require_overload,
        min_completed=minimum_completed,
        max_overload_ratio=args.max_overload_ratio,
        min_throughput=args.min_throughput,
        max_p95=args.max_p95,
        preflight_passed=bool(preflight["passed"]),
        postflight_passed=bool(postflight and postflight["passed"]),
        postflight_error=postflight_error,
    )
    passed = bool(evaluation["passed"])
    payload = {
        "schema": "kestrel.soak_result.v3",
        "requested": requested,
        "completed": evaluation["completed"],
        "overloaded": evaluation["overloaded"],
        "failed": evaluation["failed"],
        "passed": passed,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "load_elapsed_seconds": round(load_elapsed, 3),
        "throughput_completed_per_second": evaluation[
            "throughput_completed_per_second"
        ],
        "overload_ratio": evaluation["overload_ratio"],
        "latency_seconds": evaluation["latency_seconds"],
        "failure_types": evaluation["failure_types"],
        "request_accounting": evaluation["request_accounting"],
        "capsule_completion": evaluation["capsule_completion"],
        "runtime_integrity": {
            "preflight": preflight,
            "postflight": postflight,
            "postflight_error": postflight_error,
        },
        "acceptance": {
            **dict(evaluation["acceptance"]),
            "response_contract": response_contract,
        },
    }
    print(json.dumps(redact_secrets(payload), indent=2, sort_keys=True))
    return 0 if passed else 1


def _one_run(
    base_url: str,
    headers: dict[str, str],
    index: int,
    timeout: float,
    response_contract: ResponseContract = "exact-ok",
) -> dict[str, Any]:
    started = time.monotonic()
    prompt = f"soak probe {index}: reply with OK"
    created = _json_request(
        f"{base_url}/api/runs",
        headers=headers,
        method="POST",
        payload={"message": prompt, "autonomy_mode": "manual"},
        timeout=min(timeout, 30.0),
    )
    if created.get("overloaded"):
        return {"ok": False, "probe_index": index, **created}
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
            assistant = str(record.get("assistant_message") or "").strip()
            response_contract_passed = _response_contract_passed(
                assistant,
                prompt=prompt,
                contract=response_contract,
            )
            capsule_evidence = {
                "asserted": False,
                "passed": False,
                "completed_event_count": 0,
                "failed_event_count": 0,
                "error": "run_not_eligible",
            }
            if status == "completed" and response_contract_passed:
                try:
                    trace = _json_request(
                        f"{base_url}/api/runs/{run_id}/trace?limit=5000",
                        headers=headers,
                        method="GET",
                        payload=None,
                        timeout=min(10.0, timeout),
                    )
                    capsule_evidence = _capsule_completion_evidence(trace)
                except Exception as exc:  # noqa: BLE001 - evidence failure is a failed probe
                    capsule_evidence = {
                        "asserted": True,
                        "passed": False,
                        "completed_event_count": 0,
                        "failed_event_count": 0,
                        "error": f"{type(exc).__name__}:{exc}",
                    }
            accepted = (
                status == "completed"
                and response_contract_passed
                and capsule_evidence["passed"] is True
            )
            return {
                "ok": accepted,
                "probe_index": index,
                "run_id": run_id,
                "status": status,
                "latency_seconds": time.monotonic() - started,
                "assistant_chars": len(assistant),
                "response_contract": response_contract,
                "response_contract_passed": response_contract_passed,
                "capsule_completion": capsule_evidence,
                "error": (
                    None
                    if accepted
                    else status
                    if status != "completed"
                    else "response_contract_failed"
                    if not response_contract_passed
                    else "capsule_completion_failed"
                ),
            }
        time.sleep(0.1)
    return {"ok": False, "probe_index": index, "run_id": run_id, "error": "timeout"}


def _capsule_completion_evidence(trace: dict[str, Any]) -> dict[str, Any]:
    timeline = trace.get("timeline")
    if not isinstance(timeline, list):
        return {
            "asserted": True,
            "passed": False,
            "completed_event_count": 0,
            "failed_event_count": 0,
            "error": "trace_timeline_missing",
        }
    event_types = [
        str(event.get("type"))
        for event in timeline
        if isinstance(event, dict) and isinstance(event.get("type"), str)
    ]
    completed_count = event_types.count("capsule.completed")
    failed_count = event_types.count("capsule.failed")
    passed = completed_count == 1 and failed_count == 0
    return {
        "asserted": True,
        "passed": passed,
        "completed_event_count": completed_count,
        "failed_event_count": failed_count,
        "error": None if passed else "capsule_completion_event_invalid",
    }


def _evaluate_results(
    results: list[dict[str, Any]],
    *,
    requested: int,
    load_elapsed_seconds: float,
    allow_overload: bool,
    require_overload: bool,
    min_completed: int,
    max_overload_ratio: float,
    min_throughput: float | None,
    max_p95: float | None,
    preflight_passed: bool,
    postflight_passed: bool,
    postflight_error: str | None,
) -> dict[str, Any]:
    successful_responses = [
        item
        for item in results
        if item.get("ok") is True and item.get("overloaded") is not True
    ]
    completed = [
        item
        for item in successful_responses
        if isinstance(item.get("capsule_completion"), dict)
        and item["capsule_completion"].get("passed") is True
    ]
    overloaded = [
        item
        for item in results
        if item.get("ok") is False
        and item.get("overloaded") is True
        and item.get("status_code") == 429
        and item.get("reason") == "run_capacity_exhausted"
        and not item.get("run_id")
    ]
    classified_ids = {id(item) for item in completed + overloaded}
    failures = [item for item in results if id(item) not in classified_ids]
    latencies = [float(item["latency_seconds"]) for item in completed]
    p95 = _percentile(latencies, 0.95) if latencies else None
    overload_ratio = len(overloaded) / requested
    throughput = len(completed) / max(load_elapsed_seconds, 1e-9)
    probe_indexes: list[int] = []
    for item in results:
        probe_index = item.get("probe_index")
        if isinstance(probe_index, int) and not isinstance(probe_index, bool):
            probe_indexes.append(probe_index)
    completed_run_ids = [
        str(item.get("run_id"))
        for item in completed
        if isinstance(item.get("run_id"), str) and str(item.get("run_id"))
    ]
    exact_accounting = (
        len(results) == requested
        and len(completed) + len(overloaded) + len(failures) == requested
        and sorted(probe_indexes) == list(range(requested))
        and len(completed_run_ids) == len(completed)
        and len(set(completed_run_ids)) == len(completed_run_ids)
    )
    capsule_passed_count = len(completed)
    capsule_gate_passed = capsule_passed_count == len(successful_responses)
    shared_checks = {
        "preflight_runtime_integrity": preflight_passed,
        "postflight_runtime_integrity": postflight_passed and postflight_error is None,
        "no_unexpected_failures": not failures,
        "exact_request_accounting": exact_accounting,
        "minimum_completed": len(completed) >= min_completed,
        "maximum_p95": max_p95 is None or (p95 is not None and p95 <= max_p95),
        "minimum_throughput": min_throughput is None or throughput >= min_throughput,
        "capsule_completion_for_every_accepted_run": capsule_gate_passed,
    }
    shared_passed = all(shared_checks.values())
    effective_allow_overload = allow_overload or require_overload
    overload_ratio_passed = overload_ratio <= max_overload_ratio
    load_overload_passed = effective_allow_overload or not overloaded
    load_passed = shared_passed and load_overload_passed and overload_ratio_passed
    saturation_passed = (
        shared_passed and bool(overloaded) and overload_ratio_passed
    )
    mode = "saturation" if require_overload else "load"
    passed = saturation_passed if require_overload else load_passed
    return {
        "passed": passed,
        "completed": len(completed),
        "overloaded": len(overloaded),
        "failed": len(failures),
        "throughput_completed_per_second": round(throughput, 3),
        "overload_ratio": round(overload_ratio, 6),
        "latency_seconds": {
            "min": round(min(latencies), 3) if latencies else None,
            "median": round(statistics.median(latencies), 3) if latencies else None,
            "p95": round(p95, 3) if p95 is not None else None,
            "max": round(max(latencies), 3) if latencies else None,
        },
        "failure_types": sorted(
            {str(item.get("error", "unknown")).split(":", 1)[0] for item in failures}
        ),
        "request_accounting": {
            "requested": requested,
            "result_count": len(results),
            "classified_count": len(completed) + len(overloaded) + len(failures),
            "probe_indexes_exact": sorted(probe_indexes) == list(range(requested)),
            "accepted_run_ids_unique": len(completed_run_ids) == len(completed)
            and len(set(completed_run_ids)) == len(completed_run_ids),
            "passed": exact_accounting,
        },
        "capsule_completion": {
            "asserted_via": "GET /api/runs/{run_id}/trace?limit=5000",
            "eligible_completed_response_count": len(successful_responses),
            "accepted_run_count": len(completed),
            "passed_count": capsule_passed_count,
            "passed": capsule_gate_passed,
            "residual": None,
        },
        "acceptance": {
            "mode": mode,
            "shared": {
                "passed": shared_passed,
                "checks": shared_checks,
                "min_completed": min_completed,
                "max_p95": max_p95,
                "min_throughput": min_throughput,
            },
            "load": {
                "applicable": not require_overload,
                "allow_overload": effective_allow_overload,
                "overload_policy_passed": load_overload_passed,
                "max_overload_ratio": max_overload_ratio,
                "overload_ratio_passed": overload_ratio_passed,
                "passed": load_passed if not require_overload else None,
            },
            "saturation": {
                "applicable": require_overload,
                "require_overload": require_overload,
                "observed_capacity_overload": bool(overloaded),
                "max_overload_ratio": max_overload_ratio,
                "overload_ratio_passed": overload_ratio_passed,
                "passed": saturation_passed if require_overload else None,
            },
        },
    }


def _response_contract_passed(
    assistant: str,
    *,
    prompt: str,
    contract: ResponseContract,
) -> bool:
    if contract == "exact-ok":
        return assistant.casefold() == "ok"
    return assistant == f"Mock response: {prompt}"


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
        try:
            error_payload = json.loads(detail)
        except json.JSONDecodeError:
            error_payload = {}
        reason = error_payload.get("detail") if isinstance(error_payload, dict) else None
        if exc.code == 429 and reason == "run_capacity_exhausted":
            return {
                "overloaded": True,
                "status_code": exc.code,
                "reason": reason,
            }
        safe_detail = redact_secrets(reason if isinstance(reason, str) else detail)
        raise RuntimeError(f"http_{exc.code}:{safe_detail}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid_json_response")
    return parsed


def _runtime_snapshot(
    base_url: str,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    readiness = _json_request(
        f"{base_url}/api/health/ready",
        headers=headers,
        method="GET",
        payload=None,
        timeout=min(10.0, timeout),
    )
    memory = _json_request(
        f"{base_url}/api/memory/verify",
        headers=headers,
        method="GET",
        payload=None,
        timeout=min(30.0, timeout),
    )
    memory_layers = {layer: memory.get(layer) is True for layer in sorted(EXPECTED_MEMORY_LAYERS)}
    passed = readiness.get("ok") is True and bool(memory_layers) and all(memory_layers.values())
    return {
        "passed": passed,
        "readiness_ok": readiness.get("ok") is True,
        "memory_layers": memory_layers,
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


if __name__ == "__main__":
    raise SystemExit(main())
