from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from types import ModuleType


def _load_soak_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "run-soak.py"
    spec = importlib.util.spec_from_file_location("kestrel_run_soak", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_completed_run_requires_nonempty_ok_response(monkeypatch) -> None:
    soak = _load_soak_module()
    responses = iter(
        (
            {"run_id": "run-1"},
            {"run_id": "run-1", "status": "completed", "assistant_message": ""},
        )
    )
    monkeypatch.setattr(soak, "_json_request", lambda *args, **kwargs: next(responses))

    result = soak._one_run("http://127.0.0.1:9999", {}, 1, 1.0)

    assert result["ok"] is False
    assert result["error"] == "response_contract_failed"
    assert result["response_contract"] == "exact-ok"
    assert result["response_contract_passed"] is False


def test_completed_run_accepts_explicit_ok_response(monkeypatch) -> None:
    soak = _load_soak_module()
    responses = iter(
        (
            {"run_id": "run-2"},
            {
                "run_id": "run-2",
                "status": "completed",
                "assistant_message": "OK",
            },
            {"timeline": [{"type": "capsule.completed"}]},
        )
    )
    monkeypatch.setattr(soak, "_json_request", lambda *args, **kwargs: next(responses))

    result = soak._one_run("http://127.0.0.1:9999", {}, 2, 1.0)

    assert result["ok"] is True
    assert result["response_contract"] == "exact-ok"
    assert result["response_contract_passed"] is True


def test_completed_run_rejects_negated_or_explanatory_ok(monkeypatch) -> None:
    soak = _load_soak_module()
    for assistant_message in ("NOT OK", "OK, but the run was degraded", "The answer is OK"):
        responses = iter(
            (
                {"run_id": "run-negative"},
                {
                    "run_id": "run-negative",
                    "status": "completed",
                    "assistant_message": assistant_message,
                },
            )
        )
        monkeypatch.setattr(
            soak,
            "_json_request",
            lambda *args, _responses=responses, **kwargs: next(_responses),
        )

        result = soak._one_run("http://127.0.0.1:9999", {}, 3, 1.0)

        assert result["ok"] is False
        assert result["error"] == "response_contract_failed"


def test_completed_run_accepts_only_exact_per_request_mock_echo(monkeypatch) -> None:
    soak = _load_soak_module()
    prompt = "soak probe 7: reply with OK"
    responses = iter(
        (
            {"run_id": "run-mock"},
            {
                "run_id": "run-mock",
                "status": "completed",
                "assistant_message": f"Mock response: {prompt}",
            },
            {"timeline": [{"type": "capsule.completed"}]},
        )
    )
    monkeypatch.setattr(soak, "_json_request", lambda *args, **kwargs: next(responses))

    result = soak._one_run(
        "http://127.0.0.1:9999",
        {},
        7,
        1.0,
        "mock-echo",
    )

    assert result["ok"] is True
    assert result["response_contract"] == "mock-echo"
    assert result["response_contract_passed"] is True
    assert result["capsule_completion"]["passed"] is True


def test_completed_run_requires_exact_capsule_completion_evidence(monkeypatch) -> None:
    soak = _load_soak_module()
    responses = iter(
        (
            {"run_id": "run-no-capsule"},
            {
                "run_id": "run-no-capsule",
                "status": "completed",
                "assistant_message": "OK",
            },
            {"timeline": [{"type": "run.completed"}]},
        )
    )
    monkeypatch.setattr(soak, "_json_request", lambda *args, **kwargs: next(responses))

    result = soak._one_run("http://127.0.0.1:9999", {}, 4, 1.0)

    assert result["ok"] is False
    assert result["error"] == "capsule_completion_failed"
    assert result["capsule_completion"] == {
        "asserted": True,
        "passed": False,
        "completed_event_count": 0,
        "failed_event_count": 0,
        "error": "capsule_completion_event_invalid",
    }


def test_mock_echo_contract_rejects_echo_from_different_request(monkeypatch) -> None:
    soak = _load_soak_module()
    responses = iter(
        (
            {"run_id": "run-mixed"},
            {
                "run_id": "run-mixed",
                "status": "completed",
                "assistant_message": "Mock response: soak probe 6: reply with OK",
            },
        )
    )
    monkeypatch.setattr(soak, "_json_request", lambda *args, **kwargs: next(responses))

    result = soak._one_run(
        "http://127.0.0.1:9999",
        {},
        7,
        1.0,
        "mock-echo",
    )

    assert result["ok"] is False
    assert result["response_contract_passed"] is False


def test_main_rejects_zero_minimum_completions_before_preflight(monkeypatch) -> None:
    soak = _load_soak_module()
    monkeypatch.setattr(
        soak,
        "_runtime_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid arguments must fail before preflight")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run-soak.py", "--allow-overload", "--min-completed", "0"],
    )

    try:
        soak.main()
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("invalid --min-completed must raise SystemExit")


def _http_error(code: int, payload: dict[str, str]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://127.0.0.1:9999/api/runs",
        code=code,
        msg="test",
        hdrs=None,
        fp=io.BytesIO(json.dumps(payload).encode()),
    )


def test_json_request_accepts_only_structured_capacity_overload(monkeypatch) -> None:
    soak = _load_soak_module()

    def raise_capacity(*args, **kwargs):  # noqa: ANN002, ANN003
        raise _http_error(429, {"detail": "run_capacity_exhausted"})

    monkeypatch.setattr(soak.urllib.request, "urlopen", raise_capacity)

    result = soak._json_request(
        "http://127.0.0.1:9999/api/runs",
        headers={},
        method="POST",
        payload={"message": "probe"},
        timeout=1.0,
    )

    assert result == {
        "overloaded": True,
        "status_code": 429,
        "reason": "run_capacity_exhausted",
    }


def test_json_request_rejects_rate_limit_as_capacity_overload(monkeypatch) -> None:
    soak = _load_soak_module()

    def raise_rate_limit(*args, **kwargs):  # noqa: ANN002, ANN003
        raise _http_error(429, {"detail": "rate_limit_exceeded"})

    monkeypatch.setattr(soak.urllib.request, "urlopen", raise_rate_limit)

    try:
        soak._json_request(
            "http://127.0.0.1:9999/api/runs",
            headers={},
            method="POST",
            payload={"message": "probe"},
            timeout=1.0,
        )
    except RuntimeError as exc:
        assert str(exc) == "http_429:rate_limit_exceeded"
    else:
        raise AssertionError("rate limiting must not masquerade as queue overload")


def test_json_request_rejects_generic_service_unavailable_as_overload(
    monkeypatch,
) -> None:
    soak = _load_soak_module()

    def raise_unavailable(*args, **kwargs):  # noqa: ANN002, ANN003
        raise _http_error(503, {"detail": "runtime_unavailable"})

    monkeypatch.setattr(soak.urllib.request, "urlopen", raise_unavailable)

    try:
        soak._json_request(
            "http://127.0.0.1:9999/api/runs",
            headers={},
            method="POST",
            payload={"message": "probe"},
            timeout=1.0,
        )
    except RuntimeError as exc:
        assert str(exc) == "http_503:runtime_unavailable"
    else:
        raise AssertionError("generic 503 must fail the soak")


def test_runtime_snapshot_requires_ready_and_all_memory_layers(monkeypatch) -> None:
    soak = _load_soak_module()
    responses = iter(({"ok": True}, {"working": True, "episodic": False}))
    monkeypatch.setattr(soak, "_json_request", lambda *args, **kwargs: next(responses))

    snapshot = soak._runtime_snapshot("http://127.0.0.1:9999", {}, 1.0)

    assert snapshot["passed"] is False
    assert snapshot["readiness_ok"] is True
    assert snapshot["memory_layers"]["working"] is True
    assert snapshot["memory_layers"]["episodic"] is False
    assert set(snapshot["memory_layers"]) == soak.EXPECTED_MEMORY_LAYERS


def test_runtime_snapshot_rejects_generic_ok_as_memory_verification(monkeypatch) -> None:
    soak = _load_soak_module()
    responses = iter(({"ok": True}, {"ok": True}))
    monkeypatch.setattr(soak, "_json_request", lambda *args, **kwargs: next(responses))

    snapshot = soak._runtime_snapshot("http://127.0.0.1:9999", {}, 1.0)

    assert snapshot["passed"] is False
    assert not any(snapshot["memory_layers"].values())


def test_runtime_snapshot_accepts_all_six_verified_layers(monkeypatch) -> None:
    soak = _load_soak_module()
    responses = iter(
        (
            {"ok": True},
            {layer: True for layer in soak.EXPECTED_MEMORY_LAYERS},
        )
    )
    monkeypatch.setattr(soak, "_json_request", lambda *args, **kwargs: next(responses))

    snapshot = soak._runtime_snapshot("http://127.0.0.1:9999", {}, 1.0)

    assert snapshot["passed"] is True
    assert all(snapshot["memory_layers"].values())


def test_main_refuses_to_load_an_unready_runtime(monkeypatch, capsys) -> None:
    soak = _load_soak_module()
    monkeypatch.setattr(
        soak,
        "_runtime_snapshot",
        lambda *args, **kwargs: {
            "passed": False,
            "readiness_ok": False,
            "memory_layers": {"working": True},
        },
    )
    monkeypatch.setattr(
        soak,
        "_one_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("load must not start after a failed preflight")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["run-soak.py", "--runs", "1"])

    assert soak.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"] == "preflight"
    assert payload["runtime_integrity"]["preflight"]["passed"] is False


def _accepted_probe(index: int, *, run_id: str | None = None) -> dict[str, object]:
    return {
        "ok": True,
        "probe_index": index,
        "run_id": run_id or f"run-{index}",
        "latency_seconds": 0.25,
        "capsule_completion": {"passed": True},
    }


def _capacity_rejection(index: int) -> dict[str, object]:
    return {
        "ok": False,
        "probe_index": index,
        "overloaded": True,
        "status_code": 429,
        "reason": "run_capacity_exhausted",
    }


def _evaluate(soak, results, **overrides):  # noqa: ANN001, ANN003
    options = {
        "requested": len(results),
        "load_elapsed_seconds": 1.0,
        "allow_overload": False,
        "require_overload": False,
        "min_completed": len(results),
        "max_overload_ratio": 0.9,
        "min_throughput": None,
        "max_p95": None,
        "preflight_passed": True,
        "postflight_passed": True,
        "postflight_error": None,
    }
    options.update(overrides)
    return soak._evaluate_results(results, **options)


def test_load_acceptance_is_separate_from_saturation_acceptance() -> None:
    soak = _load_soak_module()

    result = _evaluate(soak, [_accepted_probe(0), _accepted_probe(1)])

    assert result["passed"] is True
    assert result["acceptance"]["mode"] == "load"
    assert result["acceptance"]["load"]["passed"] is True
    assert result["acceptance"]["saturation"]["applicable"] is False
    assert result["acceptance"]["saturation"]["passed"] is None


def test_saturation_requires_observed_capacity_overload() -> None:
    soak = _load_soak_module()

    missing = _evaluate(
        soak,
        [_accepted_probe(0), _accepted_probe(1)],
        require_overload=True,
        min_completed=1,
    )
    observed = _evaluate(
        soak,
        [_accepted_probe(0), _capacity_rejection(1)],
        require_overload=True,
        min_completed=1,
    )

    assert missing["passed"] is False
    assert missing["acceptance"]["saturation"]["observed_capacity_overload"] is False
    assert observed["passed"] is True
    assert observed["acceptance"]["mode"] == "saturation"
    assert observed["acceptance"]["load"]["passed"] is None
    assert observed["acceptance"]["saturation"]["passed"] is True


def test_saturation_enforces_overload_ratio_and_throughput_bounds() -> None:
    soak = _load_soak_module()
    results = [_accepted_probe(0)] + [_capacity_rejection(index) for index in range(1, 10)]

    excessive_overload = _evaluate(
        soak,
        results,
        require_overload=True,
        min_completed=1,
        max_overload_ratio=0.8,
    )
    low_throughput = _evaluate(
        soak,
        [_accepted_probe(0), _capacity_rejection(1)],
        require_overload=True,
        min_completed=1,
        min_throughput=2.0,
    )

    assert excessive_overload["overload_ratio"] == 0.9
    assert excessive_overload["passed"] is False
    assert (
        excessive_overload["acceptance"]["saturation"]["overload_ratio_passed"]
        is False
    )
    assert low_throughput["passed"] is False
    assert (
        low_throughput["acceptance"]["shared"]["checks"]["minimum_throughput"]
        is False
    )


def test_exact_accounting_rejects_duplicate_probe_or_run_identity() -> None:
    soak = _load_soak_module()
    results = [
        _accepted_probe(0, run_id="same-run"),
        _accepted_probe(0, run_id="same-run"),
    ]

    result = _evaluate(soak, results)

    assert result["passed"] is False
    assert result["request_accounting"]["probe_indexes_exact"] is False
    assert result["request_accounting"]["accepted_run_ids_unique"] is False
    assert result["request_accounting"]["passed"] is False
