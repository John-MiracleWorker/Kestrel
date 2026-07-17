from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.agent import _sanitize_llm_response
from nested_memvid_agent.runtime_models import LLMResponse, ToolCall
from nested_memvid_agent.security_boundary import (
    assert_path_not_sensitive,
    is_credential_env_name,
    redact_secrets,
    redact_text,
    register_secret_env_names,
    register_secret_value,
    sanitized_subprocess_environment,
)


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.production",
        ".npmrc",
        ".pypirc",
        ".git/config",
        "nested/secrets/provider.json",
        "nested/credentials/service.json",
        "keys/release.pem",
        "keys/release.p12",
        "client_secret-production.json",
    ],
)
def test_sensitive_path_rules_cover_nested_credentials(
    tmp_path: Path,
    relative_path: str,
) -> None:
    path = tmp_path / relative_path

    with pytest.raises(ValueError, match="sensitive credential paths"):
        assert_path_not_sensitive(tmp_path, path, requested_path=relative_path)


def test_redaction_covers_environment_values_and_common_secret_shapes() -> None:
    provider_secret = "opaque-provider-secret-12345"
    text = (
        f"OPENAI_API_KEY={provider_secret}\n"
        '"client_secret": "client-secret-value-123"\n'
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
        "registry=https://user:password-value@example.test/simple\n"
        "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----\n"
        "token_count: 42\n"
    )

    redacted = redact_text(text, environ={"OPENAI_API_KEY": provider_secret})

    assert provider_secret not in redacted
    assert "client-secret-value-123" not in redacted
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "password-value" not in redacted
    assert "private-material" not in redacted
    assert "token_count: 42" in redacted
    assert redacted.count("<redacted>") >= 5


def test_recursive_redaction_preserves_public_secret_metadata() -> None:
    payload = {
        "token": "raw-value",
        "nested": {"api_key": "another-value"},
        "secret_ref": "secret://provider_token",
        "api_key_env": "OPENAI_API_KEY",
        "fallback_api_key_env": {"name": "ANTHROPIC_API_KEY", "present": False},
        "token_configured": True,
        "secret_backend": "json",
        "password_ref": "correct-horse-battery-staple",
        "password_configured": "not-a-boolean-secret-value",
        "private_key_path": "/tmp/private-key-material.pem",
    }

    redacted = redact_secrets(payload, environ={})

    assert redacted["token"] == "<redacted>"
    assert redacted["nested"]["api_key"] == "<redacted>"
    assert redacted["secret_ref"] == "secret://provider_token"
    assert redacted["api_key_env"] == "OPENAI_API_KEY"
    assert redacted["fallback_api_key_env"] == {
        "name": "ANTHROPIC_API_KEY",
        "present": False,
    }
    assert redacted["token_configured"] is True
    assert redacted["secret_backend"] == "json"
    assert redacted["password_ref"] == "<redacted>"
    assert redacted["password_configured"] == "<redacted>"
    assert redacted["private_key_path"] == "<redacted>"


@pytest.mark.parametrize(
    "descriptor",
    [
        {"name": "raw-secret-value", "present": True},
        {"name": "OPENAI_API_KEY", "present": "yes"},
        {"name": "OPENAI_API_KEY", "present": True, "value": "raw-secret-value"},
    ],
)
def test_secret_env_descriptors_must_match_the_public_shape(
    descriptor: dict[str, object],
) -> None:
    assert redact_secrets({"api_key_env": descriptor}, environ={})["api_key_env"] == "<redacted>"


def test_exact_runtime_and_custom_environment_secrets_are_redacted() -> None:
    broker_secret = "opaque-broker-value-12345"
    configured_secret = "opaque-provider-auth-value-12345"
    custom_key_secret = "opaque-custom-provider-key-12345"
    register_secret_value(broker_secret)
    register_secret_env_names(
        {"DOMINION_AUTH"},
        environ={"DOMINION_AUTH": configured_secret},
    )

    redacted = redact_text(
        f"broker={broker_secret} configured={configured_secret} key={custom_key_secret}",
        environ={"DOMINION_KEY": custom_key_secret},
    )

    assert broker_secret not in redacted
    assert configured_secret not in redacted
    assert custom_key_secret not in redacted
    assert redacted.count("<redacted>") == 3


def test_recursive_redaction_removes_registered_secrets_from_mapping_keys() -> None:
    raw_secret = "opaque-secret-dictionary-key-12345"
    register_secret_value(raw_secret)

    redacted = redact_secrets({raw_secret: {"safe": True}}, environ={})

    assert raw_secret not in redacted
    assert redacted == {"<redacted>": {"safe": True}}


def test_subprocess_environment_removes_credentials_but_keeps_runtime_values() -> None:
    source = {
        "PATH": "/usr/bin",
        "PYTHONPATH": "src",
        "SAFE_FLAG": "present",
        "OPENAI_API_KEY": "provider-secret",
        "TELEGRAM_BOT_TOKEN": "telegram-secret",
        "NEST_AGENT_API_TOKEN": "server-secret",
        "CUSTOM_CLIENT_SECRET": "client-secret",
        "AWS_ACCESS_KEY_ID": "access-key",
    }

    sanitized = sanitized_subprocess_environment(source)

    assert sanitized == {
        "PATH": "/usr/bin",
        "PYTHONPATH": "src",
        "SAFE_FLAG": "present",
    }
    assert is_credential_env_name("OPENAI_API_KEY") is True
    assert is_credential_env_name("SAFE_FLAG") is False


def test_subprocess_environment_removes_registered_custom_secret_names() -> None:
    custom_name = "DOMINION_SESSION_HANDLE"
    assert is_credential_env_name(custom_name) is False
    register_secret_env_names({custom_name}, environ={})

    sanitized = sanitized_subprocess_environment(
        {
            "PATH": "/usr/bin",
            custom_name: "opaque-custom-runtime-secret",
        }
    )

    assert sanitized == {"PATH": "/usr/bin"}


def test_llm_tool_arguments_cross_the_same_secret_redaction_boundary() -> None:
    secret = "opaque-tool-argument-secret-12345"
    response = LLMResponse(
        content="invoke",
        tool_calls=(
            ToolCall(
                name="file.write",
                arguments={
                    "path": "debug.txt",
                    "content": f"api_key={secret}",
                    "secret_ref": "secret://provider_token",
                },
                id="call_secret",
            ),
        ),
    )

    sanitized = _sanitize_llm_response(response)

    arguments = sanitized.tool_calls[0].arguments
    assert secret not in str(arguments)
    assert "<redacted>" in str(arguments)
    assert arguments["secret_ref"] == "secret://provider_token"
