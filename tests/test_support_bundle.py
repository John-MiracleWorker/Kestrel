from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

import nested_memvid_agent.support_bundle as support_bundle
from nested_memvid_agent.cli import main
from nested_memvid_agent.cognition.models import FailureEpisode, ProofOfWorkSummary
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_log import AgentEvent, JsonlEventLog
from nested_memvid_agent.server_product_routes import register_product_routes
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.support_bundle import export_support_bundle


def test_support_bundle_git_probe_uses_absolute_resolved_executable(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="main\n", stderr="")

    monkeypatch.setattr(support_bundle.subprocess, "run", fake_run)

    result = support_bundle._run_git(tmp_path, "branch", "--show-current")  # noqa: SLF001

    assert result["returncode"] == 0
    assert calls[-1][-2:] == ["branch", "--show-current"]
    assert all(Path(command[0]).is_absolute() for command in calls)


def test_support_bundle_export_writes_redacted_archive(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    raw_secret = "sk-proj-supportBundleSecret123456"
    bearer_secret = "Bearer abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("OPENAI_API_KEY", raw_secret)
    config = _support_config(tmp_path, provider="openai", api_key_env="OPENAI_API_KEY")
    _write_raw_event_log(config.log_dir, bearer_secret)
    _seed_state(config.state_path)

    result = export_support_bundle(config, output_path=tmp_path / "bundle.zip", log_tail=5)

    assert result.bundle_path == tmp_path / "bundle.zip"
    assert result.manifest["schema"] == "kestrel.support_bundle.v1"
    with zipfile.ZipFile(result.bundle_path) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert {
            "manifest.json",
            "product_readiness.json",
            "setup_readiness.json",
            "runtime.json",
            "git.json",
            "state_summary.json",
            "logs/events_tail.json",
            "logs/files.json",
        } <= names
        combined_text = "\n".join(
            archive.read(name).decode("utf-8") for name in sorted(names) if name.endswith(".json")
        )
        runtime = json.loads(archive.read("runtime.json"))
        state_summary = json.loads(archive.read("state_summary.json"))

    if os.name != "nt":
        assert stat.S_IMODE(result.bundle_path.stat().st_mode) == 0o600

    assert raw_secret not in combined_text
    assert "abcdefghijklmnopqrstuvwxyz" not in combined_text
    assert "<redacted>" in combined_text
    assert runtime["provider"] == "openai"
    assert runtime["api_key_env"] == {"name": "OPENAI_API_KEY", "present": True}
    assert runtime["learning_flags"]["enable_proactive_routines"] is False
    assert runtime["learning_flags"]["max_routines_per_tick"] == 3
    assert "secret_store_path" in runtime["paths"]
    assert state_summary["schema_version"] >= 1
    assert state_summary["tables"]["runs"] == 1
    assert state_summary["tables"]["approval_requests"] == 1
    assert state_summary["tables"]["capability_overrides"] == 1
    assert state_summary["tables"]["capability_change_log"] == 1
    assert state_summary["tables"]["routines"] == 1
    assert state_summary["tables"]["routine_occurrences"] == 0
    assert state_summary["routine_summary"] == {
        "enabled_definitions": 0,
        "expired_claims": 0,
        "occurrences_by_status": {
            "claimed": 0,
            "completed": 0,
            "failed": 0,
            "running": 0,
            "skipped": 0,
        },
        "oldest_nonterminal": None,
    }


@pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic-link safety contract")
def test_support_bundle_refuses_symlink_destination_without_mutating_victim(
    tmp_path: Path,
) -> None:
    config = _support_config(tmp_path)
    victim = tmp_path / "victim.txt"
    victim_bytes = b"irreplaceable victim bytes\n"
    victim.write_bytes(victim_bytes)
    destination = tmp_path / "bundle.zip"
    destination.symlink_to(victim)

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        export_support_bundle(config, output_path=destination)

    assert destination.is_symlink()
    assert victim.read_bytes() == victim_bytes
    assert not list(tmp_path.glob(".kestrel-support-*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link safety contract")
def test_support_bundle_refuses_hardlinked_destination_without_mutating_victim(
    tmp_path: Path,
) -> None:
    config = _support_config(tmp_path)
    victim = tmp_path / "victim.txt"
    victim_bytes = b"hard-linked victim bytes\n"
    victim.write_bytes(victim_bytes)
    destination = tmp_path / "bundle.zip"
    os.link(victim, destination)

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        export_support_bundle(config, output_path=destination)

    assert destination.read_bytes() == victim_bytes
    assert victim.read_bytes() == victim_bytes
    assert os.path.samestat(victim.stat(), destination.stat())
    assert not list(tmp_path.glob(".kestrel-support-*.tmp"))


def test_support_bundle_write_failure_removes_private_temporary(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    config = _support_config(tmp_path)
    destination = tmp_path / "bundle.zip"

    def fail_write_json(_archive: zipfile.ZipFile, _name: str, _payload: object) -> None:
        raise OSError("simulated archive write failure")

    monkeypatch.setattr(support_bundle, "_write_json", fail_write_json)

    with pytest.raises(OSError, match="simulated archive write failure"):
        export_support_bundle(config, output_path=destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".kestrel-support-*.tmp"))


def test_support_bundle_concurrent_publish_is_create_once_and_leaves_valid_archive(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    config = _support_config(tmp_path)
    destination = tmp_path / "bundle.zip"
    publish_barrier = Barrier(2)
    real_publish = support_bundle._publish_bundle_entry

    def synchronized_publish(
        directory_fd: int | None,
        parent: Path,
        temporary_name: str,
        destination_name: str,
    ) -> None:
        publish_barrier.wait(timeout=5)
        real_publish(directory_fd, parent, temporary_name, destination_name)

    monkeypatch.setattr(support_bundle, "_publish_bundle_entry", synchronized_publish)

    def attempt_export() -> object:
        try:
            return export_support_bundle(config, output_path=destination)
        except FileExistsError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: attempt_export(), range(2)))

    assert sum(isinstance(outcome, FileExistsError) for outcome in outcomes) == 1
    assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert archive.namelist()[0] == "manifest.json"
    assert not list(tmp_path.glob(".kestrel-support-*.tmp"))


def test_support_bundle_reports_prompt_free_routine_aggregates(tmp_path: Path) -> None:
    config = _support_config(tmp_path)
    state = AgentStateStore(config.state_path)
    private_name = "Support-only private routine name 7e3c1a"
    private_prompt = "ROUTINE_PRIVATE_PROMPT_SENTINEL_7e3c1a"

    running_due = datetime(2021, 1, 1, tzinfo=UTC)
    running = state.create_routine(
        routine_id="support-running",
        name=private_name,
        prompt=private_prompt,
        schedule_kind="once",
        start_at=running_due,
    )
    state.update_routine(
        running.routine_id,
        expected_revision=running.revision,
        enabled=True,
    )
    running_claim = state.claim_due_routine_occurrences(
        now=running_due,
        claim_owner="support-running-owner",
    ).claimed[0]
    _current, applied = state.mark_routine_occurrence_running(
        running_claim.occurrence_id,
        claim_owner="support-running-owner",
        claim_generation=running_claim.claim_generation,
        run_id=running_claim.run_id,
        now=running_due,
    )
    assert applied is True

    claimed_due = datetime(2020, 1, 1, tzinfo=UTC)
    claimed = state.create_routine(
        routine_id="support-expired-claimed",
        name=private_name,
        prompt=private_prompt,
        schedule_kind="once",
        start_at=claimed_due,
    )
    state.update_routine(
        claimed.routine_id,
        expected_revision=claimed.revision,
        enabled=True,
    )
    expired_claim = state.claim_due_routine_occurrences(
        now=claimed_due,
        claim_owner="support-expired-owner",
    ).claimed[0]
    assert expired_claim.status == "claimed"

    result = export_support_bundle(config, output_path=tmp_path / "routine-bundle.zip")

    with zipfile.ZipFile(result.bundle_path) as archive:
        combined_text = "\n".join(
            archive.read(name).decode("utf-8")
            for name in sorted(archive.namelist())
            if name.endswith(".json")
        )
        state_summary = json.loads(archive.read("state_summary.json"))

    assert private_name not in combined_text
    assert private_prompt not in combined_text
    assert state_summary["routine_summary"] == {
        "enabled_definitions": 2,
        "expired_claims": 1,
        "occurrences_by_status": {
            "claimed": 1,
            "completed": 0,
            "failed": 0,
            "running": 1,
            "skipped": 0,
        },
        "oldest_nonterminal": {
            "created_at": claimed_due.isoformat(),
            "scheduled_for": claimed_due.isoformat(),
            "status": "claimed",
            "updated_at": claimed_due.isoformat(),
        },
    }


def test_support_bundle_event_tail_removes_turn_text_but_keeps_diagnostics(
    tmp_path: Path,
) -> None:
    config = _support_config(tmp_path)
    user_sentinel = "UNIQUE_USER_TURN_TEXT_4db615"
    routine_sentinel = "UNIQUE_ROUTINE_TURN_TEXT_8a26c1"
    content_sentinel = "UNIQUE_NESTED_CONTENT_TEXT_6f89e2"
    channel_sentinel = "UNIQUE_CHANNEL_TURN_TEXT_2c97f4"
    event_log = JsonlEventLog(config.log_dir / "events.jsonl")
    event_log.append(
        AgentEvent(
            id="evt_support_user_turn",
            type="turn.start",
            created_at="2026-07-19T12:00:00+00:00",
            payload={
                "session_id": "support-user-session",
                "run_id": "run_support_user",
                "user_message": user_sentinel,
                "message_length": len(user_sentinel),
                "turn_origin": "primary_user",
                "transcript_scope": "primary",
                "status": "accepted",
            },
        )
    )
    event_log.append(
        AgentEvent(
            id="evt_support_routine_turn",
            type="turn.start",
            created_at="2026-07-19T12:01:00+00:00",
            payload={
                "session_id": "support-routine-session",
                "run_id": "run_support_routine",
                "user_message": routine_sentinel,
                "turn_origin": "scheduled_routine",
                "transcript_scope": "internal",
                "request": {
                    "prompt": routine_sentinel,
                    "prompt_tokens": 17,
                    "result": {"content": content_sentinel, "status": "queued"},
                },
            },
        )
    )
    event_log.append(
        AgentEvent(
            id="evt_support_channel_turn",
            type="channel.receive",
            created_at="2026-07-19T12:02:00+00:00",
            payload={
                "channel": "telegram",
                "channel_id": "support-channel",
                "conversation_id": "support-conversation",
                "user_id": "support-user",
                "message_id": "support-message",
                "text": channel_sentinel,
                "metadata": {"status": "received"},
                "session_id": "support-channel-session",
            },
        )
    )
    raw_log = event_log.path.read_text(encoding="utf-8")
    assert user_sentinel in raw_log
    assert routine_sentinel in raw_log
    assert content_sentinel in raw_log
    assert channel_sentinel in raw_log

    result = export_support_bundle(config, output_path=tmp_path / "event-tail-bundle.zip")

    with zipfile.ZipFile(result.bundle_path) as archive:
        combined_text = "\n".join(
            archive.read(name).decode("utf-8")
            for name in sorted(archive.namelist())
            if name.endswith(".json")
        )
        events = json.loads(archive.read("logs/events_tail.json"))
        manifest = json.loads(archive.read("manifest.json"))

    assert user_sentinel not in combined_text
    assert routine_sentinel not in combined_text
    assert content_sentinel not in combined_text
    assert channel_sentinel not in combined_text
    assert manifest["redaction"]["logs"] == (
        "free_form_text_redacted_metadata_allowlist_tail_only"
    )
    assert events == [
        {
            "created_at": "2026-07-19T12:00:00+00:00",
            "id": "evt_support_user_turn",
            "payload": {
                "message_length": len(user_sentinel),
                "run_id": "run_support_user",
                "session_id": "support-user-session",
                "status": "accepted",
                "transcript_scope": "primary",
                "turn_origin": "primary_user",
                "user_message": "<redacted>",
            },
            "type": "turn.start",
        },
        {
            "created_at": "2026-07-19T12:01:00+00:00",
            "id": "evt_support_routine_turn",
            "payload": {
                "request": {
                    "prompt": "<redacted>",
                    "prompt_tokens": 17,
                    "result": {"content": "<redacted>", "status": "queued"},
                },
                "run_id": "run_support_routine",
                "session_id": "support-routine-session",
                "transcript_scope": "internal",
                "turn_origin": "scheduled_routine",
                "user_message": "<redacted>",
            },
            "type": "turn.start",
        },
        {
            "created_at": "2026-07-19T12:02:00+00:00",
            "id": "evt_support_channel_turn",
            "payload": {
                "channel": "telegram",
                "channel_id": "support-channel",
                "conversation_id": "support-conversation",
                "message_id": "support-message",
                "metadata": {"status": "received"},
                "session_id": "support-channel-session",
                "text": "<redacted>",
                "user_id": "support-user",
            },
            "type": "channel.receive",
        },
    ]


def test_support_bundle_event_tail_sanitizes_real_proof_of_work_payload(
    tmp_path: Path,
) -> None:
    config = _support_config(tmp_path)
    objective_sentinel = "PROOF_OBJECTIVE_SENTINEL_7fdcb1"
    command_sentinel = "PROOF_COMMAND_SENTINEL_d0cb3e"
    diagnosis_sentinel = "PROOF_DIAGNOSIS_SENTINEL_ef64a9"
    strategy_sentinel = "PROOF_STRATEGY_SENTINEL_a724c0"
    failure = FailureEpisode(
        failure_id="failure_support_bundle",
        run_id="run_support_proof",
        task_id="task_support_proof",
        tool_name="shell.run",
        command=command_sentinel,
        error_text=f"failure output: {diagnosis_sentinel}",
        category="tool_failure",
        diagnosis=diagnosis_sentinel,
        attempted_strategy=strategy_sentinel,
        similar_lessons_used=("lesson_support_proof",),
        resolved=False,
        confidence=0.76,
        created_at="2026-07-19T12:03:00+00:00",
    )
    proof = ProofOfWorkSummary(objective=objective_sentinel)
    proof.completed_steps.append(f"Ran {command_sentinel}")
    proof.tools_used.append(
        {
            "tool": "shell.run",
            "tool_call_id": "tool_support_proof",
            "success": False,
            "error": diagnosis_sentinel,
        }
    )
    proof.failures.append(failure.to_payload())
    proof.diagnoses.append(
        {
            "classification": "tool_failure",
            "confidence": 0.72,
            "retryable": True,
            "diagnosis": diagnosis_sentinel,
            "strategy": strategy_sentinel,
        }
    )
    proof.remaining_risks.append(f"risk: {diagnosis_sentinel}")
    proof.stop_reason = "tool_error"
    event_log = JsonlEventLog(config.log_dir / "events.jsonl")
    event_log.append(
        AgentEvent(
            id="evt_support_turn_end",
            type="turn.end",
            created_at="2026-07-19T12:04:00+00:00",
            payload={
                "session_id": "support-proof-session",
                "run_id": "run_support_proof",
                "stop_reason": "tool_error",
                "memory_writes": ["record_support_proof"],
                "tools": 1,
                "proof_of_work": proof.to_payload(),
            },
        )
    )
    raw_log = event_log.path.read_text(encoding="utf-8")
    for sentinel in (
        objective_sentinel,
        command_sentinel,
        diagnosis_sentinel,
        strategy_sentinel,
    ):
        assert sentinel in raw_log

    result = export_support_bundle(config, output_path=tmp_path / "proof-bundle.zip")

    with zipfile.ZipFile(result.bundle_path) as archive:
        combined_text = "\n".join(
            archive.read(name).decode("utf-8")
            for name in sorted(archive.namelist())
            if name.endswith(".json")
        )
        event = json.loads(archive.read("logs/events_tail.json"))[0]

    for sentinel in (
        objective_sentinel,
        command_sentinel,
        diagnosis_sentinel,
        strategy_sentinel,
    ):
        assert sentinel not in combined_text
    assert event["id"] == "evt_support_turn_end"
    assert event["type"] == "turn.end"
    assert event["created_at"] == "2026-07-19T12:04:00+00:00"
    assert event["payload"] == {
        "session_id": "support-proof-session",
        "run_id": "run_support_proof",
        "stop_reason": "tool_error",
        "memory_writes": ["record_support_proof"],
        "tools": 1,
        "proof_of_work": {
            "objective": "<redacted>",
            "completed_steps": ["<redacted>"],
            "tools_used": [
                {
                    "tool": "shell.run",
                    "tool_call_id": "tool_support_proof",
                    "success": False,
                    "error": "<redacted>",
                }
            ],
            "validation_evidence": [],
            "failures": [
                {
                    "failure_id": "failure_support_bundle",
                    "run_id": "run_support_proof",
                    "task_id": "task_support_proof",
                    "tool_name": "shell.run",
                    "command": "<redacted>",
                    "error_text": "<redacted>",
                    "category": "tool_failure",
                    "diagnosis": "<redacted>",
                    "attempted_strategy": "<redacted>",
                    "similar_lessons_used": ["lesson_support_proof"],
                    "resolved": False,
                    "resolution_summary": None,
                    "validation_evidence": [],
                    "confidence": 0.76,
                    "created_at": "2026-07-19T12:03:00+00:00",
                }
            ],
            "diagnoses": [
                {
                    "classification": "tool_failure",
                    "confidence": 0.72,
                    "retryable": True,
                    "diagnosis": "<redacted>",
                    "strategy": "<redacted>",
                }
            ],
            "lessons_applied": [],
            "lessons_created": [],
            "remaining_risks": ["<redacted>"],
            "stop_reason": "tool_error",
        },
    }


def test_support_bundle_event_tail_preserves_malformed_json_diagnostic(
    tmp_path: Path,
) -> None:
    config = _support_config(tmp_path)
    event_log = JsonlEventLog(config.log_dir / "events.jsonl")
    event_log.append(
        AgentEvent(
            id="evt_before_malformed",
            type="diagnostic.test",
            created_at="2026-07-19T12:05:00+00:00",
            payload={"status": "ok"},
        )
    )
    with event_log.path.open("a", encoding="utf-8") as handle:
        handle.write("{malformed-json\n")

    result = export_support_bundle(
        config,
        output_path=tmp_path / "malformed-tail-bundle.zip",
        log_tail=2,
    )

    with zipfile.ZipFile(result.bundle_path) as archive:
        events = json.loads(archive.read("logs/events_tail.json"))

    assert events[0]["id"] == "evt_before_malformed"
    assert events[1] == {"line": 1, "error": "invalid_json: Expecting property name enclosed in double quotes"}


def test_product_support_bundle_route_exports_default_bundle(tmp_path: Path) -> None:
    config = _support_config(tmp_path)
    app = FastAPI()
    register_product_routes(app, active_config=lambda: config)
    client = TestClient(app)

    response = client.post("/api/product/support-bundle")

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "kestrel.support_bundle.v1"
    bundle_path = Path(payload["bundle_path"])
    assert bundle_path.exists()
    assert bundle_path.parent == tmp_path / "support-bundles"
    assert "manifest.json" in payload["entries"]


def test_product_support_bundle_cli_exports_json(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: object
) -> None:
    raw_secret = "sk-proj-cliSupportBundleSecret123456"
    monkeypatch.setenv("OPENAI_API_KEY", raw_secret)
    output_path = tmp_path / "cli-support.zip"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nest-agent",
            "product",
            "support-bundle",
            "--provider",
            "openai",
            "--api-key-env",
            "OPENAI_API_KEY",
            "--workspace",
            str(tmp_path),
            "--memory-dir",
            str(tmp_path / "memory"),
            "--state-path",
            str(tmp_path / "state" / "agent.db"),
            "--log-dir",
            str(tmp_path / "logs"),
            "--output",
            str(output_path),
            "--json",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "kestrel.support_bundle.v1"
    assert Path(payload["bundle_path"]) == output_path
    assert output_path.exists()
    assert raw_secret not in json.dumps(payload)


def _support_config(
    tmp_path: Path, *, provider: str = "mock", api_key_env: str | None = None
) -> AgentConfig:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    return AgentConfig(
        provider=provider,
        model="mock" if provider == "mock" else "gpt-test",
        api_key_env=api_key_env,
        workspace=tmp_path,
        memory_dir=memory_dir,
        state_path=tmp_path / "state" / "agent.db",
        log_dir=tmp_path / "logs",
        secret_store_path=tmp_path / "secrets" / "local_vault.json",
    )


def _seed_state(path: Path) -> None:
    state = AgentStateStore(path)
    state.create_run(
        run_id="run_support",
        message="debug productization",
        session_id="support",
        workspace=".",
        provider="mock",
        model="mock",
    )
    state.create_approval(
        approval_id="approval_support",
        run_id="run_support",
        tool_call_id="tool_support",
        tool_name="shell.run",
        arguments={"command": ["echo", "hello"]},
        risk="high",
    )
    state.set_capability_override(
        "tool",
        "shell.run",
        False,
        expected_revision=0,
        default_enabled=True,
        updated_by="support-test",
    )
    state.create_routine(
        routine_id="support-routine",
        name="Support routine",
        prompt="Inspect local support state",
        schedule_kind="once",
        start_at="2030-01-01T00:00:00+00:00",
    )


def _write_raw_event_log(log_dir: Path, secret_text: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "id": "evt_support",
                "type": "provider.trace",
                "created_at": "2026-05-23T00:00:00+00:00",
                "payload": {"authorization": secret_text, "status": "failed"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
