from __future__ import annotations

from scripts.run_golden_evals import _report_exit_code, _run_case, _summary


def test_golden_eval_report_exits_nonzero_when_any_case_fails() -> None:
    failed = _run_case("synthetic_failure", lambda: {"passed": False})
    summary = _summary([failed])
    report = {
        "results": [failed],
        "summary": summary,
        "passed": summary["fail_count"] == 0,
    }

    assert report["passed"] is False
    assert _report_exit_code(report) == 1


def test_golden_eval_report_exits_zero_only_for_explicit_pass() -> None:
    assert _report_exit_code({"passed": True}) == 0
    assert _report_exit_code({"passed": False}) == 1
    assert _report_exit_code({}) == 1
