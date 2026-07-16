from nested_memvid_agent.server_mcp_routes import mcp_public, mcp_result_public


class _FakeSecretBroker:
    def resolve(self, value: str) -> str | None:
        return "resolved" if value == "secret://configured" else None

    def status(self, value: str) -> dict[str, object]:
        return {"validated": value == "secret://configured", "last_validated_at": "now"}


def test_mcp_public_redacts_secret_env_to_status_metadata() -> None:
    payload = mcp_public(
        {
            "id": "local-mcp",
            "env": {"LOG_LEVEL": "debug"},
            "args": ["--safe-flag"],
            "secret_env": {
                "API_TOKEN": "secret://configured",
                "PLAIN_ENV": "PLAIN_ENV_NAME",
            },
        },
        _FakeSecretBroker(),
    )

    assert "secret_env" not in payload
    assert "env" not in payload
    assert "args" not in payload
    assert payload["env_keys"] == ["LOG_LEVEL"]
    assert payload["argument_count"] == 1
    assert payload["secret_env_status"] == {
        "API_TOKEN": {
            "source_env": "secret://configured",
            "secret_ref": "secret://configured",
            "configured": True,
            "validated": True,
            "last_validated_at": "now",
        },
        "PLAIN_ENV": {
            "source_env": "PLAIN_ENV_NAME",
            "secret_ref": None,
            "configured": False,
            "validated": False,
            "last_validated_at": "now",
        },
    }


def test_mcp_result_public_redacts_nested_server() -> None:
    payload = mcp_result_public(
        {"ok": True, "server": {"id": "local-mcp", "secret_env": {"API_TOKEN": "secret://configured"}}},
        _FakeSecretBroker(),
    )

    server = payload["server"]
    assert isinstance(server, dict)
    assert "secret_env" not in server
    assert server["secret_env_status"]["API_TOKEN"]["configured"] is True
