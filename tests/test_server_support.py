from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution
from nested_memvid_agent.server_support import (
    api_auth_error,
    bounded_limit,
    csv_layers,
    execution_response,
    host_is_trusted,
    hostname_from_header,
    hostname_from_url,
    known_secret_env_names,
    tool_response_payload,
)


def test_api_auth_error_accepts_bearer_and_api_key(monkeypatch) -> None:
    config = AgentConfig(require_api_auth=True, api_auth_token_env="KESTREL_TEST_TOKEN")
    monkeypatch.setenv("KESTREL_TEST_TOKEN", "secret")

    assert api_auth_error(config, {"authorization": "Bearer secret"}) is None
    assert api_auth_error(config, {"x-kestrel-api-key": "secret"}) is None
    assert api_auth_error(config, {}) == (401, "Invalid or missing Kestrel API token.")


def test_api_auth_error_reports_missing_token_env(monkeypatch) -> None:
    config = AgentConfig(require_api_auth=True, api_auth_token_env="KESTREL_TEST_TOKEN")
    monkeypatch.delenv("KESTREL_TEST_TOKEN", raising=False)

    assert api_auth_error(config, {}) == (503, "Missing API auth token env: KESTREL_TEST_TOKEN")


def test_server_support_normalizes_request_helpers() -> None:
    assert csv_layers("policy, semantic ,,working") == ["policy", "semantic", "working"]
    assert csv_layers(None) is None
    assert bounded_limit(0, default=20, maximum=100) == 20
    assert bounded_limit(200, default=20, maximum=100) == 100
    assert hostname_from_header("127.0.0.1:8765") == "127.0.0.1"
    assert hostname_from_url("http://localhost:8765/path") == "localhost"
    assert host_is_trusted("coming-emacs-experienced-dome.trycloudflare.com", ["*.trycloudflare.com"])
    assert not host_is_trusted("trycloudflare.com", ["*.trycloudflare.com"])
    assert not host_is_trusted("evil.example", ["*.trycloudflare.com"])


def test_server_support_collects_allowed_secret_env_names() -> None:
    names = known_secret_env_names(
        [{"token_env": "TELEGRAM_TOKEN", "webhook_url_env": "WEBHOOK_URL", "settings": {"signature_secret_env": "SIGNING_SECRET"}}],
        [{"secret_env": {"API_KEY": "OPENAI_API_KEY"}}],
    )

    assert names == {"TELEGRAM_TOKEN", "WEBHOOK_URL", "SIGNING_SECRET", "OPENAI_API_KEY"}


def test_server_support_serializes_tool_execution() -> None:
    execution = ToolExecution(
        call=ToolCall(name="memory.search", arguments={}, id="call_1"),
        success=True,
        content="ok",
        data={"hits": []},
    )

    assert execution_response(execution) == {
        "tool": "memory.search",
        "tool_call_id": "call_1",
        "success": True,
        "content": "ok",
        "data": {"hits": []},
        "error": None,
    }
    assert tool_response_payload(execution) == {"success": True, "hits": [], "content": "ok", "error": None}
