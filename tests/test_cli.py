from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from pytest import MonkeyPatch, raises

from nested_memvid_agent.agent_backup import AgentBackupManager
from nested_memvid_agent.cli import (
    _cli_run_idle_timeout_seconds,
    _run_exit_code,
    _shutdown_run_manager,
    _validate_server_bind,
    _wait_for_run,
    main,
)
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayerSpec, load_layer_specs
from nested_memvid_agent.models import (
    EvidenceRef,
    MemoryKind,
    MemoryLayer,
    MemoryRecord,
    RetrievalQuery,
)
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.promotion_ledger import PromotionEntry, PromotionLedger
from nested_memvid_agent.runtime_ownership import PrimaryRuntimeOwnership
from nested_memvid_agent.state_store import AgentStateStore, routine_session_id

PINNED_VALIDATION_IMAGE = "example.invalid/kestrel-validation@sha256:" + "a" * 64


class _ConceptEmbedder:
    model_name = "concept-test"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(1, dtype=np.float32)
        synonyms = {"pythonpath": 0, "module": 0, "sys": 0}
        for raw in text.lower().replace(".", " ").split():
            idx = synonyms.get(raw.strip())
            if idx is not None:
                vector[idx] = 1.0
        return vector


def test_cli_run_idle_timeout_covers_all_configured_provider_attempts() -> None:
    assert (
        _cli_run_idle_timeout_seconds(SimpleNamespace(timeout_seconds=60, max_retries=2))
        == 195.0
    )
    assert (
        _cli_run_idle_timeout_seconds(SimpleNamespace(timeout_seconds=1, max_retries=0))
        == 16.0
    )
    assert (
        _cli_run_idle_timeout_seconds(
            SimpleNamespace(
                timeout_seconds=60,
                max_retries=2,
                llm_turn_summaries=True,
            )
        )
        == 375.0
    )


def test_cli_shutdown_failure_is_reported_without_runtime_traceback() -> None:
    mcp_shutdowns: list[bool] = []
    manager = SimpleNamespace(
        shutdown=lambda **_kwargs: False,
        mcp=SimpleNamespace(shutdown=lambda: mcp_shutdowns.append(True)),
    )

    with raises(SystemExit, match="worker did not stop"):
        _shutdown_run_manager(manager)

    assert mcp_shutdowns == [True]


def test_cli_mcp_shutdown_failure_is_reported_without_runtime_traceback() -> None:
    manager = SimpleNamespace(
        shutdown=lambda **_kwargs: True,
        mcp=SimpleNamespace(shutdown=lambda: False),
    )

    with raises(SystemExit, match="MCP worker did not stop"):
        _shutdown_run_manager(manager)


def test_cli_wait_does_not_cancel_a_retry_that_outlives_one_attempt(
    monkeypatch: MonkeyPatch,
) -> None:
    now = 0.0
    polls = 0

    def clock() -> float:
        nonlocal now
        now += 1.0
        return now

    def get_run(_run_id: str) -> dict[str, str]:
        nonlocal polls
        polls += 1
        return {"status": "completed" if polls >= 80 else "running"}

    monkeypatch.setattr("nested_memvid_agent.cli.monotonic", clock)
    monkeypatch.setattr("nested_memvid_agent.cli.sleep", lambda _seconds: None)
    manager = SimpleNamespace(
        config=SimpleNamespace(timeout_seconds=60, max_retries=2),
        _threads={},
        get_run=get_run,
    )

    assert _wait_for_run(manager, "run_cold_start")["status"] == "completed"


def _clear_nest_agent_env(monkeypatch: MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("NEST_AGENT_") or name.startswith("NESTED_MEMVID_"):
            monkeypatch.delenv(name, raising=False)


def test_memory_verify_subcommand_reports_layers(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
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


def test_agent_backup_list_subcommand_reports_json(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "backup",
            "list",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(tmp_path / "state" / "agent.db"),
            "--skills-dir",
            str(tmp_path / "skills"),
            "--plugins-dir",
            str(tmp_path / "plugins"),
            "--mcp-config",
            str(tmp_path / "config" / "mcp.json"),
            "--channels-config",
            str(tmp_path / "config" / "channels.json"),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )

    main()

    assert json.loads(capsys.readouterr().out) == []


def test_backup_cli_mutations_refuse_a_live_primary_runtime_before_writing(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    state_path = tmp_path / "state" / "agent.db"
    memory_dir = tmp_path / "memory"
    cases = (
        (
            ["backup", "create"],
            tmp_path / "agent-create-backups",
        ),
        (
            ["backup", "restore", "missing", "--yes"],
            tmp_path / "agent-restore-backups",
        ),
        (
            ["memory", "backup"],
            tmp_path / "memory-create-backups",
        ),
        (
            ["memory", "restore", "missing", "--yes"],
            tmp_path / "memory-restore-backups",
        ),
    )
    ownership = PrimaryRuntimeOwnership(state_path)
    ownership.acquire()
    try:
        for command, backup_dir in cases:
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    "nest-agent",
                    *command,
                    "--backend",
                    "memvid",
                    "--memory-dir",
                    str(memory_dir),
                    "--state-path",
                    str(state_path),
                    "--backup-dir",
                    str(backup_dir),
                ],
            )
            with raises(SystemExit) as exc_info:
                main()
            assert str(exc_info.value) == (
                "Another Kestrel runtime already owns this state database. Stop it "
                "cleanly before creating or restoring a backup."
            )
            assert not backup_dir.exists()
    finally:
        ownership.release()


def test_agent_backup_cli_restores_clean_host_with_backed_up_layer_config(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    runtime = tmp_path / ".nest"
    memory_dir = runtime / "memory"
    state_path = runtime / "state" / "agent.db"
    source_layer_config = runtime / "config" / "custom-layers.json"
    canonical_layer_config = runtime / "config" / "layers.json"
    backup_dir = tmp_path / "backups"
    specs: dict[MemoryLayer, LayerSpec] = {
        layer: replace(spec, mv2_file=f"portable-{layer.value}.mv2")
        for layer, spec in DEFAULT_LAYER_SPECS.items()
    }
    memory_dir.mkdir(parents=True)
    for layer, spec in specs.items():
        (memory_dir / spec.mv2_file).write_bytes(f"{layer.value}:portable".encode())
    state_path.parent.mkdir(parents=True)
    # Commit and close explicitly: sqlite3's context manager alone leaves the
    # native handle open, which makes the clean-host deletion correctly fail on
    # Windows even though no runtime owns this fixture database.
    with closing(sqlite3.connect(state_path)) as connection, connection:
        connection.execute("CREATE TABLE restore_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO restore_probe(value) VALUES ('portable')")
    source_layer_config.parent.mkdir(parents=True)
    source_layer_config.write_text(
        json.dumps({layer.value: {"mv2_file": spec.mv2_file} for layer, spec in specs.items()}),
        encoding="utf-8",
    )
    manager = AgentBackupManager(
        memory_dir=memory_dir,
        state_path=state_path,
        backup_root=backup_dir,
        runs_dir=runtime / "runs",
        skills_dir=runtime / "skills",
        plugins_dir=runtime / "plugins",
        mcp_config_path=runtime / "config" / "mcp.json",
        channel_config_path=runtime / "config" / "channels.json",
        runtime_settings_path=runtime / "config" / "runtime_settings.json",
        layer_config_path=source_layer_config,
        specs=specs,
    )
    manifest = manager.create(retain=4)
    shutil.rmtree(runtime)
    verified: list[dict[MemoryLayer, str]] = []

    def verify_staged_memory(
        config: AgentConfig,
        *,
        specs: dict[MemoryLayer, LayerSpec] | None = None,
    ) -> None:
        assert specs is not None
        layer_files = {layer: spec.mv2_file for layer, spec in specs.items()}
        assert all((config.memory_dir / name).is_file() for name in layer_files.values())
        verified.append(layer_files)

    monkeypatch.setattr(
        "nested_memvid_agent.cli._seal_and_verify_memory",
        verify_staged_memory,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "backup",
            "restore",
            str(manifest["backup_id"]),
            "--backend",
            "memvid",
            "--memory-dir",
            str(memory_dir),
            "--state-path",
            str(state_path),
            "--skills-dir",
            str(runtime / "skills"),
            "--plugins-dir",
            str(runtime / "plugins"),
            "--mcp-config",
            str(runtime / "config" / "mcp.json"),
            "--channels-config",
            str(runtime / "config" / "channels.json"),
            "--backup-dir",
            str(backup_dir),
            "--yes",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    expected_files = {layer: spec.mv2_file for layer, spec in specs.items()}
    assert payload["backup_id"] == manifest["backup_id"]
    assert verified == [expected_files]
    assert canonical_layer_config.is_file()
    assert not source_layer_config.exists()
    assert {
        layer: spec.mv2_file for layer, spec in load_layer_specs(canonical_layer_config).items()
    } == expected_files
    with closing(sqlite3.connect(state_path)) as connection, connection:
        assert connection.execute("SELECT value FROM restore_probe").fetchone() == ("portable",)


def test_server_can_disable_access_logging(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr("nested_memvid_agent.server.create_app", lambda config: object())
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kwargs: captured.update({"app": app, **kwargs})
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "server",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(tmp_path / "state" / "agent.db"),
            "--no-access-log",
        ],
    )

    main()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["access_log"] is False


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


def test_product_setup_check_exits_nonzero_when_required_setup_is_not_ready(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    _clear_nest_agent_env(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "product",
            "setup",
            "--backend",
            "memory",
            "--provider",
            "mock",
            "--workspace",
            str(tmp_path / "missing-workspace"),
            "--memory-dir",
            str(tmp_path / "missing-memory"),
            "--state-path",
            str(tmp_path / "state" / "agent.db"),
            "--log-dir",
            str(tmp_path / "logs"),
            "--json",
            "--check",
        ],
    )

    with raises(SystemExit) as error:
        main()

    assert error.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["fail_count"] > 0


def test_channel_telegram_set_webhook_cli_redacts_secrets(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-super-secret")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "telegram-secret-token")
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "id": "telegram",
                        "provider": "telegram",
                        "token_env": "TELEGRAM_BOT_TOKEN",
                        "settings": {
                            "signature_provider": "telegram",
                            "signature_secret_env": "TELEGRAM_WEBHOOK_SECRET",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true,"result":true}'

    monkeypatch.setattr(
        "nested_memvid_agent.net_safety.public_url_allowed",
        lambda url, require_https=False: (True, ""),
    )
    monkeypatch.setattr(
        "nested_memvid_agent.channels.adapters.urlopen", lambda request, timeout: FakeResponse()
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "channel",
            "--backend",
            "memory",
            "--channels-config",
            str(channels_path),
            "telegram",
            "--telegram-webhook-action",
            "set",
            "--webhook-url",
            "https://kestrel.example/api/channels/telegram/webhook?channel_id=telegram",
        ],
    )

    main()

    output = capsys.readouterr().out
    assert '"method": "setWebhook"' in output
    assert "123456:ABC-super-secret" not in output
    assert "telegram-secret-token" not in output
    assert '"secret_token": "<configured>"' in output


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


def test_memory_vector_subcommands_report_status_and_rebuild(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    monkeypatch.setattr(
        "nested_memvid_agent.layers.make_local_embedder",
        lambda model_name=None: _ConceptEmbedder(),
    )
    memory_dir = tmp_path / "memory"
    layer_config = tmp_path / "layers.json"
    layer_config.write_text(
        json.dumps(
            {
                "semantic": {
                    "search_mode": "hybrid",
                    "vector": {
                        "enabled": True,
                        "embedding_provider": "local",
                        "embedding_model": "concept-test",
                        "index_path": "semantic.mv2.vector.sqlite",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    memory = build_memory_system(
        "memory",
        memory_dir,
        specs=load_layer_specs(layer_config),
        enforce_stable_write_integrity=False,
    )
    memory.put(
        MemoryRecord(
            id="pythonpath-fix",
            title="Python path import fix",
            content="Set PYTHONPATH before pytest invocations.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.9,
        )
    )
    memory.close_all()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "vector",
            "status",
            "--backend",
            "memory",
            "--memory-dir",
            str(memory_dir),
            "--layer-config",
            str(layer_config),
            "--json",
        ],
    )

    main()

    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["layers"]["semantic"]["enabled"] is True
    assert status_payload["layers"]["semantic"]["indexed_count"] == 1

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "memory",
            "vector",
            "rebuild",
            "--backend",
            "memory",
            "--memory-dir",
            str(memory_dir),
            "--layer-config",
            str(layer_config),
            "--layer",
            "semantic",
            "--json",
        ],
    )

    main()

    rebuild_payload = json.loads(capsys.readouterr().out)
    assert rebuild_payload["rebuilt"]["semantic"]["indexed_count"] == 1
    assert rebuild_payload["rebuilt"]["semantic"]["stale_count"] == 0


def test_memory_correct_subcommand_supersedes_target(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    memory_dir = tmp_path / "memory"
    memory = build_memory_system(
        "memory", memory_dir, enforce_stable_write_integrity=False
    )
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
    assert reopened.retrieve(
        RetrievalQuery(query="beta not enabled", layers=(MemoryLayer.SEMANTIC,))
    )


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


def test_chat_self_and_web_slash_commands(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
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

    monkeypatch.setattr(
        sys,
        "argv",
        [*common, "--allow-web", "--web-backend", "mock", "--message", "/web kestrel soul"],
    )
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
    monkeypatch.setattr(
        sys, "argv", ["nest-agent", "plugins", "review", "owner/repo", *common_args, "--json"]
    )
    main()
    review = json.loads(capsys.readouterr().out)
    assert review["manifest"]["id"] == "clip"
    assert review["enable_blockers"] == []

    monkeypatch.setattr(
        sys, "argv", ["nest-agent", "plugins", "install", "owner/repo", *common_args]
    )
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


def test_context_subcommand_compiles_prompt(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
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
    assert '"validation_container"' in output
    assert '"required": false' in output
    assert '"host_fallback": false' in output
    assert '"default_command": "pytest -q"' in output


def test_doctor_validation_container_fails_closed_for_enabled_tools() -> None:
    from nested_memvid_agent import cli

    missing = cli._doctor_validation_container(
        AgentConfig(allow_shell=True, allow_codex_cli=True)
    )
    assert missing["ok"] is False
    assert missing["required"] is True
    assert missing["configured"] is False
    assert missing["required_by_config_gates"] == [
        "test.run",
        "lint.run",
        "repair.validate",
        "repair.orchestrate_validate",
        "codex.exec",
    ]

    mutable = cli._doctor_validation_container(
        AgentConfig(
            allow_shell=True,
            validation_container_image="example.invalid/kestrel-validation:latest",
        )
    )
    assert mutable["ok"] is False
    assert mutable["digest_pinned"] is False

    pinned = cli._doctor_validation_container(
        AgentConfig(
            allow_shell=True,
            validation_container_image=PINNED_VALIDATION_IMAGE,
        )
    )
    assert pinned["ok"] is True
    assert pinned["digest_pinned"] is True
    assert pinned["preload_check"] == "deferred_until_execution"
    assert pinned["host_fallback"] is False


def test_doctor_exits_nonzero_when_enabled_tools_have_no_validation_image(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    _clear_nest_agent_env(monkeypatch)
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
            "--allow-shell",
        ],
    )

    with raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["validation_container"]["ok"] is False
    assert payload["validation_container"]["required"] is True
    assert payload["validation_container"]["configured"] is False


def test_doctor_subcommand_exits_nonzero_when_runtime_is_not_ready(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(
        "nested_memvid_agent.cli._doctor_runtime",
        lambda config: {"ok": False, "memory": {"ok": False, "path": str(config.memory_dir)}},
    )
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
        ],
    )

    with raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["memory"]["ok"] is False


def test_doctor_reports_installed_but_unimportable_memvid_sdk(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    from nested_memvid_agent import cli

    real_find_spec = cli.importlib.util.find_spec

    def fake_find_spec(name: str) -> object | None:
        if name == "memvid_sdk":
            return object()
        return real_find_spec(name)

    real_import_module = cli.importlib.import_module

    def fake_import_module(name: str) -> object:
        if name == "memvid_sdk":
            raise ImportError("cannot allocate memory in static TLS block")
        return real_import_module(name)

    monkeypatch.setattr(cli.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(cli.importlib, "import_module", fake_import_module)

    report = cli._doctor_runtime(
        AgentConfig(backend="memvid", memory_dir=tmp_path / "memory", workspace=tmp_path)
    )

    assert report["ok"] is False
    assert report["optional_extras"]["extras"]["memvid"] == {
        "available": True,
        "importable": False,
        "error": "ImportError: cannot allocate memory in static TLS block",
    }
    assert report["memory"]["memvid_available"] is True
    assert report["memory"]["memvid_importable"] is False
    assert report["memory"]["error"] == (
        "memvid-sdk import failed: ImportError: cannot allocate memory in static TLS block"
    )


def test_doctor_subcommand_uses_env_config(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    _clear_nest_agent_env(monkeypatch)
    monkeypatch.setenv("NEST_AGENT_BACKEND", "memory")
    monkeypatch.setenv("NEST_AGENT_MEMORY_DIR", str(tmp_path / "env-memory"))
    monkeypatch.setenv("NEST_AGENT_PROVIDER", "openai-compatible")
    monkeypatch.setenv("NEST_AGENT_MODEL", "env-model")
    monkeypatch.setenv("NEST_AGENT_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("NEST_AGENT_ALLOW_SHELL", "true")
    monkeypatch.setenv("NEST_AGENT_VALIDATION_CONTAINER_IMAGE", PINNED_VALIDATION_IMAGE)
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
    assert payload["validation_container"]["image"] == PINNED_VALIDATION_IMAGE
    assert payload["validation_container"]["digest_pinned"] is True


def test_doctor_flags_override_env_config(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    _clear_nest_agent_env(monkeypatch)
    monkeypatch.setenv("NEST_AGENT_BACKEND", "memvid")
    monkeypatch.setenv("NEST_AGENT_MEMORY_DIR", str(tmp_path / "env-memory"))
    monkeypatch.setenv("NEST_AGENT_PROVIDER", "openai-compatible")
    monkeypatch.setenv("NEST_AGENT_MODEL", "env-model")
    monkeypatch.setenv("NEST_AGENT_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("NEST_AGENT_ALLOW_SHELL", "true")
    monkeypatch.setenv(
        "NEST_AGENT_VALIDATION_CONTAINER_IMAGE",
        "example.invalid/kestrel-validation:latest",
    )
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
            "--validation-container-image",
            PINNED_VALIDATION_IMAGE,
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
    assert payload["validation_container"]["image"] == PINNED_VALIDATION_IMAGE
    assert payload["validation_container"]["digest_pinned"] is True


def test_doctor_default_memory_dir_is_nest_memory(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    _clear_nest_agent_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["nest-agent", "doctor"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["memory"]["backend"] == "memory"
    assert Path(payload["memory"]["path"]) == Path(".nest") / "memory"


def test_run_subcommand_reports_structured_turn(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
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


def test_run_subcommand_prints_json_provider_failure_before_nonzero_exit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    payload = {
        "run_id": "run_provider_failure",
        "session_id": "cli",
        "status": "failed",
        "stop_reason": "provider_error",
        "assistant_message": "Provider authentication failed.",
        "context_chars": 0,
        "tool_count": 0,
        "error": "authentication",
        "approvals": [],
    }
    shutdowns: list[bool] = []
    manager = SimpleNamespace(
        config=SimpleNamespace(timeout_seconds=1),
        _threads={},
        mcp=SimpleNamespace(shutdown=lambda: shutdowns.append(True)),
        create_run=lambda **_kwargs: SimpleNamespace(run_id=payload["run_id"]),
        get_run=lambda _run_id: dict(payload),
    )
    monkeypatch.setattr("nested_memvid_agent.cli._build_run_manager", lambda _config: manager)
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
            str(tmp_path / "state.db"),
            "--provider",
            "openai",
            "--model",
            "fake-provider-model",
            "--json",
            "provider failure",
        ],
    )

    with raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    assert json.loads(capsys.readouterr().out) == payload
    assert shutdowns == [True]


def test_one_shot_chat_prints_provider_failure_before_nonzero_exit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    payload = {
        "run_id": "run_chat_provider_failure",
        "session_id": "cli",
        "status": "failed",
        "stop_reason": "provider_error",
        "assistant_message": "Provider authentication failed.",
        "context_chars": 0,
        "tool_count": 0,
        "error": "authentication",
        "approvals": [],
    }
    shutdowns: list[bool] = []
    manager = SimpleNamespace(
        config=SimpleNamespace(timeout_seconds=1),
        _threads={},
        mcp=SimpleNamespace(shutdown=lambda: shutdowns.append(True)),
        create_run=lambda **_kwargs: SimpleNamespace(run_id=payload["run_id"]),
        get_run=lambda _run_id: dict(payload),
    )
    monkeypatch.setattr("nested_memvid_agent.cli._build_run_manager", lambda _config: manager)
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
            "--state-path",
            str(tmp_path / "state.db"),
            "--provider",
            "openai",
            "--model",
            "fake-provider-model",
            "--message",
            "provider failure",
        ],
    )

    with raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "Provider authentication failed." in output
    assert "status: failed" in output
    assert "stop_reason: provider_error" in output
    assert shutdowns == [True]


def test_interactive_chat_reports_failed_turn_without_exiting_session(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    payload = {
        "run_id": "run_interactive_failure",
        "session_id": "cli",
        "status": "failed",
        "stop_reason": "provider_error",
        "assistant_message": "Provider unavailable.",
        "context_chars": 0,
        "tool_count": 0,
        "error": "unavailable",
        "approvals": [],
    }
    manager = SimpleNamespace(
        config=SimpleNamespace(timeout_seconds=1),
        _threads={},
        mcp=SimpleNamespace(shutdown=lambda: None),
        create_run=lambda **_kwargs: SimpleNamespace(run_id=payload["run_id"]),
        get_run=lambda _run_id: dict(payload),
    )
    messages = iter(["try once", "/exit"])
    monkeypatch.setattr("nested_memvid_agent.cli._build_run_manager", lambda _config: manager)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(messages))
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
            "--state-path",
            str(tmp_path / "state.db"),
            "--provider",
            "mock",
        ],
    )

    main()

    output = capsys.readouterr().out
    assert "Kestrel chat. Type /exit to quit." in output
    assert "agent> Provider unavailable." in output
    assert "status: failed" in output


def test_run_subcommand_no_wait_fails_before_manager_or_admission(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    manager_builds: list[bool] = []

    def build_manager(_config: object) -> None:
        manager_builds.append(True)
        raise AssertionError("--no-wait must fail before manager construction")

    monkeypatch.setattr("nested_memvid_agent.cli._build_run_manager", build_manager)
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
            "--provider",
            "mock",
            "--json",
            "--no-wait",
            "queue this",
        ],
    )

    with raises(SystemExit) as exc_info:
        main()

    assert "authenticated server API at POST /api/runs" in str(exc_info.value.code)
    assert manager_builds == []
    assert not state_path.exists()


def test_run_exit_code_contract_distinguishes_outcome_from_admission() -> None:
    assert _run_exit_code({"status": "completed"}) == 0
    assert _run_exit_code({"status": "queued"}) == 1
    assert _run_exit_code({"status": "running"}) == 1
    assert _run_exit_code({"status": "blocked", "stop_reason": "approval_required"}) == 2
    assert _run_exit_code({"status": "failed"}) == 1
    assert _run_exit_code({"status": "cancelled"}) == 1
    assert _run_exit_code({"status": "unexpected"}) == 1


def test_approval_subcommands_fail_closed_for_unbound_persistent_approval(
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

    with raises(SystemExit) as exc_info:
        main()

    approved_payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert approved_payload["approval"]["status"] == "approved"
    assert approved_payload["run"]["status"] == "failed"
    assert approved_payload["run"]["stop_reason"] == "approval_invalid_before_continuation"


def test_approve_no_wait_fails_before_decision_or_continuation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.db"
    state = AgentStateStore(state_path)
    state.create_run(
        run_id="run_no_wait_approval",
        message="manual approval",
        session_id="cli-session",
        workspace=str(tmp_path),
        model="mock",
    )
    state.create_approval(
        approval_id="approval_no_wait",
        run_id="run_no_wait_approval",
        tool_call_id="tool_shell",
        tool_name="shell.run",
        arguments={"command": ["echo", "approved"]},
        risk="high",
    )
    manager_builds: list[bool] = []

    def build_manager(_config: object) -> None:
        manager_builds.append(True)
        raise AssertionError("--no-wait must fail before manager construction")

    monkeypatch.setattr("nested_memvid_agent.cli._build_run_manager", build_manager)
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
            "--no-wait",
            "approval_no_wait",
        ],
    )

    with raises(SystemExit) as exc_info:
        main()

    assert "/api/approvals/{approval_id}/decision" in str(exc_info.value.code)
    assert manager_builds == []
    assert state.get_approval("approval_no_wait")["status"] == "pending"
    assert state.get_run("run_no_wait_approval").status == "queued"


def test_deny_subcommand_marks_run_failed(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
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


def test_memory_consolidate_subcommand_rejects_raw_score_only_promotion(
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

    assert capsys.readouterr().out == "No promotion proposed.\n"


def test_eval_subcommand_delegates_to_golden_harness(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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


def test_product_readiness_subcommand_reports_status(
    monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(sys, "argv", ["nest-agent", "product", "readiness"])

    main()

    output = capsys.readouterr().out
    assert "Full product roadmap" in output
    assert "Full hosted/team product roadmap complete: no" in output
    assert "Hosted/team auth, users, and workspaces: missing" in output
    assert "Safe autonomous learning: ready" in output


def test_product_readiness_subcommand_can_emit_json(
    monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(sys, "argv", ["nest-agent", "product", "readiness", "--json"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "kestrel.product_readiness.v2"
    assert payload["scope"] == "full_product_including_hosted_team"
    assert payload["headline"]["product_ready"] is False
    assert any(
        category["category_id"] == "golden_repair_workflow" for category in payload["categories"]
    )


def test_product_provider_certification_subcommand_can_emit_json(
    monkeypatch: MonkeyPatch, capsys: object
) -> None:
    monkeypatch.setattr(sys, "argv", ["nest-agent", "product", "provider-certification", "--json"])

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "kestrel.provider_certification.v2"
    assert payload["policy_version"] == "kestrel.provider_certification_policy.v1"
    assert payload["headline"]["release_certified"] is False
    mock = next(provider for provider in payload["providers"] if provider["provider"] == "mock")
    assert mock["status"] == "certified"
    assert mock["readiness"]["status"] == "configured"
    assert mock["certification_state"] == "implemented"
    assert mock["generate"]["status"] == "not_run"


def test_product_provider_certification_human_output_separates_readiness_and_assurance(
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    secret = "provider-certification-human-secret-7d91"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "product",
            "provider-certification",
            "--provider",
            "openai",
            "--model",
            "gpt-test",
        ],
    )

    main()

    output = capsys.readouterr().out
    assert "Policy: kestrel.provider_certification_policy.v1" in output
    assert "Assurance: 0 release certified" in output
    assert "Readiness:" in output
    assert "openai: readiness=configured certification=implemented" in output
    assert "generate=not_run" in output
    assert "learning_e2e=not_run" in output
    assert "Last tested: never" in output
    assert secret not in output


def test_routines_cli_creates_enables_ticks_and_reports_history(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    due = datetime.now(UTC) - timedelta(seconds=1)
    common = [
        "--backend",
        "memory",
        "--memory-dir",
        str(tmp_path / "memory"),
        "--state-path",
        str(tmp_path / "state" / "agent.db"),
        "--log-dir",
        str(tmp_path / "logs"),
        "--skills-dir",
        str(tmp_path / "skills"),
        "--plugins-dir",
        str(tmp_path / "plugins"),
        "--mcp-config",
        str(tmp_path / "config" / "mcp.json"),
        "--workspace",
        str(tmp_path),
    ]

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "routines",
            "create",
            "--id",
            "cli-once",
            "--name",
            "CLI once",
            "--prompt",
            "Give a deterministic mock response",
            "--schedule-kind",
            "once",
            "--start-at",
            due.isoformat(),
            *common,
            "--json",
        ],
    )
    main()
    created = json.loads(capsys.readouterr().out)
    assert created["routine_id"] == "cli-once"
    assert created["enabled"] is False

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "routines",
            "enable",
            "cli-once",
            "--expected-revision",
            str(created["revision"]),
            *common,
            "--json",
        ],
    )
    main()
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["enabled"] is True

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "routines",
            "tick",
            "--enable-proactive-routines",
            *common,
            "--json",
        ],
    )
    main()
    tick = json.loads(capsys.readouterr().out)
    assert tick["claimed"] == 1
    assert tick["occurrences"][0]["status"] == "completed"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "routines",
            "history",
            "cli-once",
            *common,
            "--json",
        ],
    )
    main()
    history = json.loads(capsys.readouterr().out)
    assert len(history) == 1
    assert history[0]["run_id"] == tick["dispatches"][0]["run_id"]


def test_routines_cli_read_does_not_resume_unrelated_queued_run(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    state_path = tmp_path / "state" / "agent.db"
    state = AgentStateStore(state_path)
    state.create_run(
        run_id="queued-before-routine-read",
        message="must remain queued",
        session_id="unrelated",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    state.create_routine(
        routine_id="read-only-list",
        name="Read only list",
        prompt="Do not execute queued work",
        schedule_kind="once",
        start_at="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "routines",
            "list",
            "--state-path",
            str(state_path),
            "--json",
        ],
    )

    main()

    assert json.loads(capsys.readouterr().out)[0]["routine_id"] == "read-only-list"
    assert state.get_run("queued-before-routine-read").status == "queued"


def test_routines_cli_tick_does_not_resume_unrelated_queued_run(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    state_path = tmp_path / "state" / "agent.db"
    state = AgentStateStore(state_path)
    state.create_run(
        run_id="queued-before-routine-tick",
        message="must remain queued",
        session_id="unrelated",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    due = datetime.now(UTC) - timedelta(seconds=1)
    routine = state.create_routine(
        routine_id="scoped-cli-tick",
        name="Scoped CLI tick",
        prompt="Give a deterministic mock response",
        schedule_kind="once",
        start_at=due,
        misfire_grace_seconds=60,
    )
    state.update_routine(
        routine.routine_id,
        expected_revision=routine.revision,
        enabled=True,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "routines",
            "tick",
            "--enable-proactive-routines",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(state_path),
            "--log-dir",
            str(tmp_path / "logs"),
            "--skills-dir",
            str(tmp_path / "skills"),
            "--plugins-dir",
            str(tmp_path / "plugins"),
            "--mcp-config",
            str(tmp_path / "config" / "mcp.json"),
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["claimed"] == 1
    assert payload["occurrences"][0]["status"] == "completed"
    assert state.get_run("queued-before-routine-tick").status == "queued"


def test_routines_cli_tick_selectively_recovers_admitted_routine_crash_window(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: object,
) -> None:
    _clear_nest_agent_env(monkeypatch)
    state_path = tmp_path / "state" / "agent.db"
    state = AgentStateStore(state_path)
    state.create_run(
        run_id="unrelated-queued-during-recovery",
        message="must remain queued",
        session_id="unrelated",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
    routine = state.create_routine(
        routine_id="recover-scoped-cli-tick",
        name="Recover scoped CLI tick",
        prompt="Give a deterministic mock response",
        schedule_kind="once",
        start_at=scheduled_for,
        misfire_grace_seconds=60,
    )
    state.update_routine(
        routine.routine_id,
        expected_revision=routine.revision,
        enabled=True,
    )
    dispatch_at = datetime.now(UTC)
    claim = state.claim_due_routine_occurrences(
        now=dispatch_at,
        claim_owner="crashed-cli-owner",
    ).claimed[0]
    state.create_run_for_routine_occurrence(
        occurrence_id=claim.occurrence_id,
        claim_owner="crashed-cli-owner",
        claim_generation=claim.claim_generation,
        dispatch_at=dispatch_at,
        run_id=claim.run_id,
        message=str(claim.request["prompt"]),
        session_id=routine_session_id(claim.routine_id),
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
        config_revision="crash-window",
        config_snapshot={
            "revision": "crash-window",
            "autonomy_mode": "background",
        },
    )
    assert state.list_task_nodes(claim.run_id) == []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "routines",
            "tick",
            "--enable-proactive-routines",
            "--backend",
            "memory",
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(state_path),
            "--log-dir",
            str(tmp_path / "logs"),
            "--skills-dir",
            str(tmp_path / "skills"),
            "--plugins-dir",
            str(tmp_path / "plugins"),
            "--mcp-config",
            str(tmp_path / "config" / "mcp.json"),
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["claimed"] == 0
    assert payload["recovered_run_ids"] == [claim.run_id]
    assert state.get_run(claim.run_id).status == "completed"
    assert state.get_routine_occurrence(claim.occurrence_id).status == "completed"
    assert state.get_run("unrelated-queued-during-recovery").status == "queued"
