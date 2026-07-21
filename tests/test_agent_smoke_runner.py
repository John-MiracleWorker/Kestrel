from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution
from scripts import run_agent_smoke


def test_agent_smoke_passes_with_isolated_exact_evidence(tmp_path: Path) -> None:
    root = tmp_path / "smoke-run"
    marker = "fresh_smoke_marker_7f16d9"

    report = run_agent_smoke.run_agent_smoke(root=root, marker=marker)

    assert report["passed"] is True
    assert all(report["assertions"].values())
    assert report["marker"] == marker
    assert report["evidence"]["search_tool_count"] == 1
    assert report["evidence"]["ordinary_policy_write_ids"] == []
    assert report["evidence"]["policy_record_count_after_ordinary"] == 0
    assert report["evidence"]["write_session"] != report["evidence"]["search_session"]
    for configured_path in report["isolation"]["configured_paths"]:
        Path(configured_path).resolve().relative_to(root.resolve())


class _FakeMemory:
    def __init__(self, *, policy_write: bool) -> None:
        self.policy_write = policy_write
        self.marker = ""

    def get_record(
        self,
        _layer: Any,
        record_id: str,
        *,
        include_inactive: bool,
    ) -> Any:
        del include_inactive
        return SimpleNamespace(
            id=record_id,
            layer=MemoryLayer.POLICY if self.policy_write else MemoryLayer.WORKING,
            content=self.marker,
        )

    def iter_records(self, *, layer: MemoryLayer, include_inactive: bool) -> list[Any]:
        del include_inactive
        if layer == MemoryLayer.POLICY and self.policy_write:
            return [
                SimpleNamespace(
                    id="ordinary-record",
                    layer=MemoryLayer.POLICY,
                    content=self.marker,
                )
            ]
        return []


class _FakeAgent:
    def __init__(self, *, policy_write: bool = False, search_tool: bool = True) -> None:
        self.memory = _FakeMemory(policy_write=policy_write)
        self.search_tool = search_tool
        self.turn_count = 0
        self.closed = False

    def chat(self, prompt: str, session_id: str) -> Any:
        self.turn_count += 1
        if self.turn_count == 1:
            self.memory.marker = prompt.removeprefix("Remember this fresh smoke marker exactly: ")
            return SimpleNamespace(
                session_id=session_id,
                user_message=prompt,
                assistant_message=f"Mock response: {prompt}",
                tool_executions=(),
                memory_writes=("ordinary-record",),
                stop_reason="complete",
            )

        marker = prompt.removeprefix("/search ")
        executions: tuple[ToolExecution, ...] = ()
        assistant_message = f"Canned search answer containing {marker}"
        if self.search_tool:
            content = json.dumps(
                [
                    {
                        "layer": "working",
                        "title": "Fresh marker",
                        "snippet": marker,
                    }
                ]
            )
            execution = ToolExecution(
                call=ToolCall(
                    name="memory.search",
                    arguments={"query": marker, "k": 5},
                ),
                success=True,
                content=content,
                data={
                    "hits": [
                        {
                            "layer": "working",
                            "title": "Fresh marker",
                            "snippet": marker,
                        }
                    ]
                },
            )
            executions = (execution,)
            assistant_message = content
        return SimpleNamespace(
            session_id=session_id,
            user_message=prompt,
            assistant_message=assistant_message,
            tool_executions=executions,
            memory_writes=(),
            stop_reason="complete",
        )

    def close(self) -> None:
        self.closed = True


def test_agent_smoke_rejects_canned_search_answer_without_tool_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAgent(search_tool=False)
    monkeypatch.setattr(run_agent_smoke, "build_agent", lambda _config: fake)

    report = run_agent_smoke.run_agent_smoke(
        root=tmp_path / "canned-run",
        marker="fresh_canned_marker",
    )

    assert report["passed"] is False
    assert report["assertions"]["exact_memory_search_succeeded"] is False
    assert report["assertions"]["memory_search_returned_fresh_marker"] is False
    assert report["assertions"]["search_answer_is_exact_tool_evidence"] is False
    assert fake.closed is True


def test_agent_smoke_rejects_ordinary_policy_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAgent(policy_write=True)
    monkeypatch.setattr(run_agent_smoke, "build_agent", lambda _config: fake)

    report = run_agent_smoke.run_agent_smoke(
        root=tmp_path / "policy-run",
        marker="fresh_policy_marker",
    )

    assert report["passed"] is False
    assert report["assertions"]["ordinary_turn_did_not_write_policy"] is False
    assert report["assertions"]["policy_layer_empty_after_ordinary_turn"] is False
    assert report["assertions"]["exact_memory_search_succeeded"] is True
    assert fake.closed is True


def test_agent_smoke_main_returns_nonzero_and_cleans_unique_temp_root_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    roots: list[Path] = []

    def failed_smoke(*, root: Path, marker: str) -> dict[str, Any]:
        roots.append(root)
        assert marker.startswith("kestrel_smoke_")
        return {
            "schema": "kestrel.agent_smoke.v1",
            "assertions": {"injected_failure": False},
            "passed": False,
        }

    monkeypatch.setattr(run_agent_smoke, "run_agent_smoke", failed_smoke)

    assert run_agent_smoke.main() == 1
    first_output = json.loads(capsys.readouterr().out)
    assert run_agent_smoke.main() == 1
    second_output = json.loads(capsys.readouterr().out)

    assert first_output["passed"] is False
    assert second_output["passed"] is False
    assert len(roots) == 2
    assert roots[0] != roots[1]
    assert all(not root.parent.exists() for root in roots)
