from __future__ import annotations

import os
from pathlib import Path

import pytest

from nested_memvid_agent.learning_eval import (
    LearningEvalOptions,
    load_learning_eval_scenario,
    run_learning_eval_suite,
)


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_LEARNING_EVALS") != "1" or not os.getenv("OPENAI_API_KEY"),
    reason="live learning architecture evals require RUN_LIVE_LEARNING_EVALS=1 and OPENAI_API_KEY",
)
def test_live_openai_learning_architecture_eval_smoke(tmp_path: Path) -> None:
    scenario = load_learning_eval_scenario("live_provider_smoke_learning_loop")
    report_path = tmp_path / "live-learning-report.md"

    report = run_learning_eval_suite(
        [scenario],
        LearningEvalOptions(
            provider="openai",
            model=os.getenv("NEST_AGENT_EVAL_MODEL", "gpt-5-mini"),
            backend="memory",
            workspace=tmp_path,
            max_llm_calls=3,
            max_cost_usd=0.5,
            max_tool_calls=1,
            timeout_seconds=90,
            report_path=report_path,
        ),
    )

    assert report.status in {"pass", "skip"}
    assert report_path.exists()
    payload = report.to_payload()
    assert payload["summary"]["llm_calls"] <= 3
    assert "OPENAI_API_KEY" not in report_path.read_text(encoding="utf-8")
