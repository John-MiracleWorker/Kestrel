from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from nested_memvid_agent.cli import main
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.server_product_routes import register_product_routes
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.support_bundle import export_support_bundle


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

    assert raw_secret not in combined_text
    assert "abcdefghijklmnopqrstuvwxyz" not in combined_text
    assert "<redacted>" in combined_text
    assert runtime["provider"] == "openai"
    assert runtime["api_key_env"] == {"name": "OPENAI_API_KEY", "present": True}
    assert "secret_store_path" in runtime["paths"]
    assert state_summary["schema_version"] >= 1
    assert state_summary["tables"]["runs"] == 1
    assert state_summary["tables"]["approval_requests"] == 1


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
