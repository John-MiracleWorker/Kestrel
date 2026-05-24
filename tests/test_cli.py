from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pytest import MonkeyPatch, raises

from nested_memvid_agent.cli import _validate_server_bind, main
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.models import (
    EvidenceRef,
    MemoryKind,
    MemoryLayer,
    MemoryRecord,
    RetrievalQuery,
)
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.promotion_ledger import PromotionEntry, PromotionLedger
from nested_memvid_agent.state_store import AgentStateStore


def _clear_nest_agent_env(monkeypatch: MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("NEST_AGENT_") or name.startswith("NESTED_MEMVID_"):
            monkeypatch.delenv(name, raising=False)


def test_memory_verify_subcommand_reports_layers(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "verify",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
        ],
    )

    main()

    output = capsys.readouterr().out
    assert "working: ok" in output
    assert "policy: ok" in output


def test_product_setup_subcommand_reports_first_run_checks(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    _clear_nest_agent_env(monkeypatch)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "product",
            "setup",
            "--provider",
            "mock",
            "--workspace",
            str(tmp_path),
            "--memory-dir",
            str(memory_dir),
            "--state-path",
            str(state_dir / "agent.db"),
            "--json",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "kestrel.setup_readiness.v1"
    assert any(check["check_id"] == "provider_configuration" for check in payload["checks"])


def test_memory_doctor_subcommand_is_dry_run_by_default(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "doctor",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
        ],
    )

    main()

    output = capsys.readouterr().out
    assert '"working"' in output
    assert '"doctor_available": false' in output


def test_memory_correct_subcommand_supersedes_target(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    memory_dir = tmp_path / "memory"
    memory = build_memory_system("memory", memory_dir)
    target_id = memory.put(
        MemoryRecord(
            id="cli-fact",
            title="CLI fact",
            content="CLI fact says beta is enabled.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.86,
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "correct",
            target_id,
            "CLI fact says beta is not enabled.",
            "--backend",
            "memory",
            "--memory-dir",
            str(memory_dir),
            "--allow-memory-import",
        ],
    )

    main()

    output = capsys.readouterr().out
    assert '"corrected": true' in output
    reopened = build_memory_system("memory", memory_dir)
    assert not reopened.get_record(MemoryLayer.SEMANTIC, target_id, include_inactive=False)
    assert reopened.retrieve(RetrievalQuery(query="beta not enabled", layers=(MemoryLayer.SEMANTIC,)))


def test_memory_compact_subcommand_is_dry_run_by_default(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "compact",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
        ],
    )

    main()

    output = capsys.readouterr().out
    assert '"dry_run": true' in output
    assert '"layer": "working"' in output


def test_memory_ledger_subcommand_reports_promotions(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    state_path = tmp_path / "state.db"
    ledger = PromotionLedger(AgentStateStore(state_path))
    ledger.record_promotion(
        PromotionEntry(
            promotion_id="promotion-cli",
            record_id="record-cli",
            source_layer=MemoryLayer.EPISODIC,
            target_layer=MemoryLayer.PROCEDURAL,
            decision_reason="test",
            validation_score=0.9,
            repeat_count=2,
            explicit_instruction=False,
            optimizer_trace={"validation_score": 0.9},
            promoted_at="2026-05-17T00:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "ledger",
            "--state-path",
            str(state_path),
            "--since",
            "all",
        ],
    )

    main()

    output = capsys.readouterr().out
    assert "Promotion ledger" in output
    assert "episodic->procedural" in output
    assert "False-positive rate" in output


def test_learning_dashboard_subcommand_reports_headline_numbers(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    from nested_memvid_agent.behavior_delta import (
        BehaviorDelta,
        BehaviorDeltaKind,
        BehaviorDeltaRisk,
        BehaviorDeltaStatus,
        TriggerSpec,
        ValidationPlan,
    )
    from nested_memvid_agent.behavior_delta_ledger import (
        BehaviorDeltaActivation,
        BehaviorDeltaLedger,
    )

    state_path = tmp_path / "state.db"
    ledger = BehaviorDeltaLedger(AgentStateStore(state_path))
    delta = BehaviorDelta(
        id="delta-cli-auto",
        title="CLI auto",
        kind=BehaviorDeltaKind.PROCEDURE,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.LOW,
        status=BehaviorDeltaStatus.ACTIVE,
        trigger=TriggerSpec(task_types=("debugging",)),
        behavior_change="Use the safer retry procedure.",
        evidence_refs=(EvidenceRef(source="test", locator="fixture"),),
        validation_plan=ValidationPlan(),
        metadata={"draft": True},
    )
    ledger.record_delta(delta)
    ledger.record_activation(
        BehaviorDeltaActivation(
            id="act-cli",
            delta_id=delta.id,
            run_id="run-cli",
            task_id=None,
            objective="debug",
            activated_at="2026-05-21T00:00:00+00:00",
            activation_reason="auto_activated_low_risk_threshold_met",
            compiled_section="ACTIVE PROCEDURES",
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["nest-agent", "learning", "dashboard", "--state-path", str(state_path), "--since", "all"],
    )

    main()

    output = capsys.readouterr().out
    assert "Learning dashboard" in output
    assert "Auto-activations: 1" in output
    assert "procedural" in output


def test_tools_subcommand_lists_risk_levels(monkeypatch: MonkeyPatch, capsys: object) -> None:
    monkeypatch.setattr(sys, "argv", ["nest-agent", "tools"])

    main()

    output = capsys.readouterr().out
    assert "memory.search [low, allowed]" in output
    assert "git.commit [high, approval required]" in output


def test_chat_self_and_web_slash_commands(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object) -> None:
    common = [
        "nest-agent",
        "chat",
        "--backend",
        "memory",
        "--memory-dir",
        str(tmp_path / "memory"),
        "--state-path",
        str(tmp_path / "state.db"),
        "--workspace",
        str(tmp_path),
    ]

    monkeypatch.setattr(sys, "argv", [*common, "--message", "/self"])
    main()
    self_output = capsys.readouterr().out
    assert "Soul" in self_output
    assert "self.mv2" in self_output

    monkeypatch.setattr(sys, "argv", [*common, "--message", "/capabilities"])
    main()
    capabilities_output = capsys.readouterr().out
    assert "self.inspect" in capabilities_output

    monkeypatch.setattr(sys, "argv", [*common, "--allow-web", "--web-backend", "mock", "--message", "/web kestrel soul"])
    main()
    web_output = capsys.readouterr().out
    assert "https://mock.kestrel.local/search/" in web_output


def test_plugins_subcommands_install_list_and_toggle(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    plugin_repo = tmp_path / "plugin-repo"
    plugin_repo.mkdir()
    (plugin_repo / "kestrel.plugin.json").write_text(
        json.dumps(
            {
                "id": "clip",
                "name": "CLI Plugin",
                "description": "CLI plugin fixture.",
                "skills": [
                    {
                        "id": "hello",
                        "description": "Say hello.",
                        "instructions": "Return hello.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_fetch(self: object, source: object, destination: Path, ref: str | None = None) -> str:
        del self, source, ref
        shutil.copytree(plugin_repo, destination)
        return "c" * 40

    monkeypatch.setattr("nested_memvid_agent.plugin_manager.GitPluginFetcher.fetch", fake_fetch)
    common_args = [
        "--state-path",
        str(tmp_path / "state.db"),
        "--plugins-dir",
        str(tmp_path / "plugins"),
        "--memory-dir",
        str(tmp_path / "memory"),
        "--allow-plugin-install",
    ]
    monkeypatch.setattr(sys, "argv", ["nest-agent", "plugins", "review", "owner/repo", *common_args, "--json"])
    main()
    review = json.loads(capsys.readouterr().out)
    assert review["manifest"]["id"] == "clip"
    assert review["enable_blockers"] == []

    monkeypatch.setattr(sys, "argv", ["nest-agent", "plugins", "install", "owner/repo", *common_args])
    main()
    assert "clip [not enabled]" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["nest-agent", "plugins", "list", *common_args])
    main()
    assert "clip [not enabled]" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["nest-agent", "plugins", "enable", "clip", *common_args])
    main()
    assert "clip [enabled]" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["nest-agent", "plugins", "disable", "clip", *common_args])
    main()
    assert "clip [not enabled]" in capsys.readouterr().out


def test_server_non_loopback_requires_api_auth_token(monkeypatch: MonkeyPatch) -> None:
    with raises(SystemExit, match="unsafe_bind"):
        _validate_server_bind("0.0.0.0", AgentConfig(require_api_auth=False))

    with raises(SystemExit, match="unsafe_bind"):
        _validate_server_bind(
            "0.0.0.0",
            AgentConfig(require_api_auth=True, api_auth_token_env="KESTREL_BIND_TEST_TOKEN"),
        )

    monkeypatch.setenv("KESTREL_BIND_TEST_TOKEN", "secret-token")
    _validate_server_bind(
        "0.0.0.0",
        AgentConfig(require_api_auth=True, api_auth_token_env="KESTREL_BIND_TEST_TOKEN"),
    )
    _validate_server_bind("127.0.0.1", AgentConfig(require_api_auth=False))


def test_context_subcommand_compiles_prompt(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "context",
            "hello context",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
        ],
    )

    main()

    output = capsys.readouterr().out
    assert "MV2 PSEUDO-CONTEXT PACK" in output
    assert "hello context" in output


def test_doctor_subcommand_reports_runtime_readiness(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "doctor",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--workspace",
            str(tmp_path),
        ],
    )

    main()

    output = capsys.readouterr().out
    assert '"python"' in output
    assert '"backend": "memory"' in output
    assert '"allow_shell": false' in output
    assert '"default_command": "pytest -q"' in output


def test_doctor_subcommand_uses_env_config(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object) -> None:
    _clear_nest_agent_env(monkeypatch)
    monkeypatch.setenv("NEST_AGENT_BACKEND", "memory")
    monkeypatch.setenv("NEST_AGENT_MEMORY_DIR", str(tmp_path / "env-memory"))
    monkeypatch.setenv("NEST_AGENT_PROVIDER", "openai-compatible")
    monkeypatch.setenv("NEST_AGENT_MODEL", "env-model")
    monkeypatch.setenv("NEST_AGENT_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("NEST_AGENT_ALLOW_SHELL", "true")
    monkeypatch.setenv("NEST_AGENT_CONTEXT_BUDGET_CHARS", "12345")
    monkeypatch.setattr(sys, "argv", ["nest-agent", "doctor"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["memory"]["backend"] == "memory"
    assert payload["memory"]["path"] == str(tmp_path / "env-memory")
    assert payload["provider"]["provider"] == "openai-compatible"
    assert payload["provider"]["model"] == "env-model"
    assert payload["provider"]["base_url_configured"] is True
    assert payload["tool_config"]["allow_shell"] is True
    assert payload["tool_config"]["context_budget_chars"] == 12345


def test_doctor_flags_override_env_config(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object) -> None:
    _clear_nest_agent_env(monkeypatch)
    monkeypatch.setenv("NEST_AGENT_BACKEND", "memvid")
    monkeypatch.setenv("NEST_AGENT_MEMORY_DIR", str(tmp_path / "env-memory"))
    monkeypatch.setenv("NEST_AGENT_PROVIDER", "openai-compatible")
    monkeypatch.setenv("NEST_AGENT_MODEL", "env-model")
    monkeypatch.setenv("NEST_AGENT_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("NEST_AGENT_ALLOW_SHELL", "true")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "doctor",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "flag-memory"),
            "--provider",
            "mock",
            "--model",
            "flag-model",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["memory"]["backend"] == "memory"
    assert payload["memory"]["path"] == str(tmp_path / "flag-memory")
    assert payload["provider"]["provider"] == "mock"
    assert payload["provider"]["model"] == "flag-model"
    assert payload["provider"]["base_url_configured"] is True
    assert payload["tool_config"]["allow_shell"] is True


def test_doctor_default_memory_dir_is_nest_memory(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    _clear_nest_agent_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["nest-agent", "doctor"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["memory"]["backend"] == "memory"
    assert payload["memory"]["path"] == ".nest/memory"


def test_run_subcommand_reports_structured_turn(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object) -> None:
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "run",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(state_path),
            "--json",
            "--events",
            "hello run",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["assistant_message"] == "Mock response: hello run"
    assert payload["stop_reason"] == "complete"
    assert payload["status"] == "completed"
    assert payload["run_id"].startswith("run_")
    assert any(event["type"] == "run.completed" for event in payload["events"])

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "status",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(state_path),
            "--json",
            payload["run_id"],
        ],
    )

    main()

    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["run_id"] == payload["run_id"]
    assert status_payload["status"] == "completed"


def test_approval_subcommands_use_persistent_run_state(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    state_path = tmp_path / "state.db"
    state = AgentStateStore(state_path)
    state.create_run(
        run_id="run_manual",
        message="manual approval",
        session_id="cli-session",
        workspace=str(tmp_path),
        model="mock",
    )
    state.create_approval(
        approval_id="approval_manual",
        run_id="run_manual",
        tool_call_id="tool_shell",
        tool_name="shell.run",
        arguments={"command": ["echo", "approved"]},
        risk="high",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "approvals",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(state_path),
            "--json",
        ],
    )

    main()

    approvals_payload = json.loads(capsys.readouterr().out)
    assert approvals_payload["approvals"][0]["approval_id"] == "approval_manual"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "approve",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(state_path),
            "--workspace",
            str(tmp_path),
            "--json",
            "approval_manual",
        ],
    )

    main()

    approved_payload = json.loads(capsys.readouterr().out)
    assert approved_payload["approval"]["status"] == "approved"
    assert approved_payload["run"]["status"] == "completed"


def test_deny_subcommand_marks_run_failed(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object) -> None:
    state_path = tmp_path / "state.db"
    state = AgentStateStore(state_path)
    state.create_run(
        run_id="run_denied",
        message="manual approval",
        session_id="cli-session",
        workspace=str(tmp_path),
        model="mock",
    )
    state.create_approval(
        approval_id="approval_denied",
        run_id="run_denied",
        tool_call_id="tool_shell",
        tool_name="shell.run",
        arguments={"command": ["echo", "denied"]},
        risk="high",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "deny",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(state_path),
            "--json",
            "approval_denied",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["approval"]["status"] == "denied"
    assert payload["run"]["status"] == "failed"
    assert payload["run"]["stop_reason"] == "approval_denied"


def test_memory_consolidate_subcommand_uses_nested_learning(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    memory_dir = tmp_path / "memory"
    memory = build_memory_system("memory", memory_dir)
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.PROCEDURE,
            title="CLI consolidate recipe",
            content="CLI consolidate recipe: run pytest -q after CLI changes.",
            confidence=0.9,
            importance=0.8,
        )
    )
    memory.seal_all()
    memory.close_all()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "consolidate",
            "--backend",
            "memory",
            "--memory-dir",
            str(memory_dir),
            "--source-layer",
            "episodic",
            "--validation-score",
            "0.9",
            "--repeat-count",
            "2",
            "--dry-run",
            "CLI consolidate recipe",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["promoted"] is True
    assert payload["target_layer"] == "procedural"
    assert payload["dry_run"] is True


def test_eval_subcommand_delegates_to_golden_harness(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("nested_memvid_agent.cli.subprocess.run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "eval",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "eval-memory"),
            "--provider",
            "mock",
        ],
    )

    with raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 0
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == [sys.executable, str(Path.cwd() / "scripts" / "run_golden_evals.py")]
    assert "--backend" in command
    assert "--memory-dir" in command


def test_chat_help_slash_command(tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "chat",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--message",
            "/help",
        ],
    )

    main()

    output = capsys.readouterr().out
    assert "Available slash commands:" in output
    assert "/status" in output
    assert "/approve <approval_id>" in output


def test_product_readiness_subcommand_reports_status(monkeypatch: MonkeyPatch, capsys: object) -> None:
    monkeypatch.setattr(sys, "argv", ["nest-agent", "product", "readiness"])

    main()

    output = capsys.readouterr().out
    assert "Product readiness" in output
    assert "Product ready: no" in output
    assert "Production auth, users, and workspaces: missing" in output
    assert "Safe autonomous learning: ready" in output


def test_product_readiness_subcommand_can_emit_json(monkeypatch: MonkeyPatch, capsys: object) -> None:
    monkeypatch.setattr(sys, "argv", ["nest-agent", "product", "readiness", "--json"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "kestrel.product_readiness.v1"
    assert payload["headline"]["product_ready"] is False
    assert any(category["category_id"] == "golden_repair_workflow" for category in payload["categories"])


def test_product_provider_certification_subcommand_can_emit_json(
    monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(sys, "argv", ["nest-agent", "product", "provider-certification", "--json"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "kestrel.provider_certification.v1"
    assert payload["headline"]["release_certified"] is False
    assert any(provider["provider"] == "mock" for provider in payload["providers"])
