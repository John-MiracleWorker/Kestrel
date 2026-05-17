import json

from nested_memvid_agent.server_channel_routes import channel_public, parse_json_body


class _FakeSecretBroker:
    def resolve(self, value: str) -> str | None:
        return (
            "raw-secret-value" if value in {"TOKEN_ENV", "WEBHOOK_ENV", "SIGNATURE_ENV"} else None
        )

    def status(self, value: str) -> dict[str, object]:
        return {"configured": self.resolve(value) is not None, "validated": value == "TOKEN_ENV"}


def test_channel_public_reports_secret_status_without_values() -> None:
    payload = channel_public(
        {
            "id": "telegram",
            "settings": {"signature_secret_env": "SIGNATURE_ENV"},
            "token_env": "TOKEN_ENV",
            "webhook_url_env": "WEBHOOK_ENV",
        },
        _FakeSecretBroker(),
    )

    encoded = json.dumps(payload)
    assert "raw-secret-value" not in encoded
    assert payload["env_status"]["token_env_configured"] is True
    assert payload["env_status"]["webhook_url_env_configured"] is True
    assert payload["env_status"]["signature_secret_env_configured"] is True


def test_parse_json_body_requires_object() -> None:
    assert parse_json_body(b'{"provider":"webhook"}') == {"provider": "webhook"}

    try:
        parse_json_body(b'["not", "object"]')
    except ValueError as exc:
        assert str(exc) == "JSON body must be an object."
    else:
        raise AssertionError("parse_json_body accepted a non-object JSON body")
