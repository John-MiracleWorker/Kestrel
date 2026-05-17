from fastapi import FastAPI
from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.server_runtime_routes import register_runtime_routes


class _FakeState:
    def schema_version(self) -> int:
        return 10


def test_runtime_routes_report_health_and_redacted_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KESTREL_TEST_API_KEY", "raw-secret-value")
    config = AgentConfig(
        name="Test Kestrel",
        memory_dir=tmp_path / "memory",
        state_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
        api_key_env="KESTREL_TEST_API_KEY",
    )
    app = FastAPI()
    register_runtime_routes(app, active_config=config, state=_FakeState())
    client = TestClient(app)

    health = client.get("/api/health")
    runtime = client.get("/api/runtime/config")

    assert health.status_code == 200
    assert health.json() == {"ok": True, "name": "Test Kestrel"}
    assert runtime.status_code == 200
    payload = runtime.json()
    assert payload["schema_version"] == 10
    assert payload["provider"]["api_key_env"] == "KESTREL_TEST_API_KEY"
    assert payload["provider"]["api_key_configured"] is True
    assert "raw-secret-value" not in runtime.text
