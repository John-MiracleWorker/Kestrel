from __future__ import annotations

import json
import sys

from scripts import run_memory_system_evals


def test_memory_system_eval_passes_with_stable_integrity_enabled(tmp_path) -> None:
    payload = run_memory_system_evals._run(tmp_path, backend="memory", provider="mock")

    assert payload["passed"] is True
    assert payload["summary"]["case_count"] == 9
    assert payload["summary"]["fail_count"] == 0
    assert payload["summary"]["policy_write_count"] == 0


def test_memory_system_eval_main_returns_nonzero_for_failed_payload(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        run_memory_system_evals,
        "_run",
        lambda root, *, backend, provider: {
            "passed": False,
            "backend": backend,
            "provider": provider,
            "root": str(root),
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_memory_system_evals.py", "--memory-dir", str(tmp_path)],
    )

    assert run_memory_system_evals.main() == 1


def test_memory_system_eval_main_refuses_nonempty_evidence_root(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / "stale-result.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        run_memory_system_evals,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stale root must fail before eval execution")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_memory_system_evals.py", "--memory-dir", str(tmp_path)],
    )

    assert run_memory_system_evals.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["stage"] == "preflight"
    assert payload["error"] == "memory_dir_must_be_empty_to_prevent_stale_evidence_reuse"
