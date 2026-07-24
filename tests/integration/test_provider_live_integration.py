from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.factory import build_llm_provider
from nested_memvid_agent.runtime_models import ChatMessage, LLMOptions, ToolSpec

CERTIFICATION_MARKER = "kestrel-provider-certification-7d91"

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PROVIDER_INTEGRATION") != "1",
    reason="set RUN_PROVIDER_INTEGRATION=1 and provider env vars to run live provider tests",
)


@dataclass(frozen=True)
class ProviderCase:
    name: str
    config: AgentConfig
    available: bool
    reason: str


def test_live_provider_generate_smoke(provider_case: ProviderCase) -> None:
    if not provider_case.available:
        pytest.skip(provider_case.reason)
    provider = build_llm_provider(provider_case.config)

    response = provider.generate(
        [
            ChatMessage(
                role="user",
                content=f"Reply with {CERTIFICATION_MARKER} exactly and nothing else.",
            )
        ],
        tools=[],
        options=LLMOptions(
            timeout_seconds=provider_case.config.timeout_seconds,
            max_retries=0,
            temperature=0.0,
        ),
    )

    assert response.content.strip() == CERTIFICATION_MARKER
    assert response.tool_calls == ()


def test_live_provider_stream_smoke(provider_case: ProviderCase) -> None:
    if not provider_case.available:
        pytest.skip(provider_case.reason)
    provider = build_llm_provider(provider_case.config)
    if not provider.capabilities.supports_streaming:
        pytest.skip(f"{provider_case.name} does not advertise streaming")

    events = list(
        provider.stream(
            [
                ChatMessage(
                    role="user",
                    content=f"Reply with {CERTIFICATION_MARKER} exactly and nothing else.",
                )
            ],
            tools=[],
            options=LLMOptions(
                stream=True,
                timeout_seconds=provider_case.config.timeout_seconds,
                max_retries=0,
                temperature=0.0,
            ),
        )
    )

    assert events
    assert events[-1].type == "message_complete"
    streamed_text = "".join(event.content for event in events if event.type == "token").strip()
    completed_text = (
        events[-1].response.content.strip()
        if events[-1].response is not None
        else ""
    )
    assert streamed_text == CERTIFICATION_MARKER or completed_text == CERTIFICATION_MARKER


def test_live_provider_native_tool_call_certification(provider_case: ProviderCase) -> None:
    if not provider_case.available:
        pytest.skip(provider_case.reason)
    provider = build_llm_provider(provider_case.config)
    if not provider.capabilities.supports_native_tools:
        pytest.skip(f"{provider_case.name} does not advertise native tools")
    certification_tool = ToolSpec(
        name="certification.echo",
        description="Return the provided text unchanged for provider tool-call certification.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )

    response = provider.generate(
        [
            ChatMessage(
                role="system",
                content=(
                    "Use the provider-native function-calling interface. Do not emit a JSON "
                    "tool envelope in assistant text."
                ),
            ),
            ChatMessage(
                role="user",
                content="Call certification.echo exactly once with text set to kestrel.",
            ),
        ],
        tools=[certification_tool],
        options=LLMOptions(
            timeout_seconds=provider_case.config.timeout_seconds,
            max_retries=0,
            temperature=0.0,
        ),
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "certification.echo"
    assert response.tool_calls[0].arguments == {"text": "kestrel"}


def _provider_cases() -> list[ProviderCase]:
    return [
        ProviderCase(
            name="lm-studio",
            config=AgentConfig(
                provider="lm-studio",
                model=os.getenv("KESTREL_IT_LM_STUDIO_MODEL", ""),
                base_url=os.getenv("KESTREL_IT_LM_STUDIO_BASE_URL"),
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("KESTREL_IT_LM_STUDIO_MODEL")),
            reason="set KESTREL_IT_LM_STUDIO_MODEL and optionally KESTREL_IT_LM_STUDIO_BASE_URL",
        ),
        ProviderCase(
            name="openai",
            config=AgentConfig(
                provider="openai",
                model=os.getenv("KESTREL_IT_OPENAI_MODEL", ""),
                api_key_env="OPENAI_API_KEY",
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("OPENAI_API_KEY") and os.getenv("KESTREL_IT_OPENAI_MODEL")),
            reason="set OPENAI_API_KEY and KESTREL_IT_OPENAI_MODEL",
        ),
        ProviderCase(
            name="anthropic",
            config=AgentConfig(
                provider="anthropic",
                model=os.getenv("KESTREL_IT_ANTHROPIC_MODEL", ""),
                api_key_env="ANTHROPIC_API_KEY",
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("ANTHROPIC_API_KEY") and os.getenv("KESTREL_IT_ANTHROPIC_MODEL")),
            reason="set ANTHROPIC_API_KEY and KESTREL_IT_ANTHROPIC_MODEL",
        ),
        ProviderCase(
            name="grok",
            config=AgentConfig(
                provider="grok",
                model=os.getenv("KESTREL_IT_GROK_MODEL", ""),
                api_key_env="XAI_API_KEY",
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("XAI_API_KEY") and os.getenv("KESTREL_IT_GROK_MODEL")),
            reason="set XAI_API_KEY and KESTREL_IT_GROK_MODEL",
        ),
        ProviderCase(
            name="gemini",
            config=AgentConfig(
                provider="gemini",
                model=os.getenv("KESTREL_IT_GEMINI_MODEL", ""),
                api_key_env="GEMINI_API_KEY",
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("GEMINI_API_KEY") and os.getenv("KESTREL_IT_GEMINI_MODEL")),
            reason="set GEMINI_API_KEY and KESTREL_IT_GEMINI_MODEL",
        ),
        ProviderCase(
            name="openai-compatible",
            config=AgentConfig(
                provider="openai-compatible",
                model=os.getenv("KESTREL_IT_OPENAI_COMPATIBLE_MODEL", ""),
                base_url=os.getenv("KESTREL_IT_OPENAI_COMPATIBLE_BASE_URL"),
                api_key_env=os.getenv("KESTREL_IT_OPENAI_COMPATIBLE_API_KEY_ENV"),
                timeout_seconds=_timeout(),
            ),
            available=bool(
                os.getenv("KESTREL_IT_OPENAI_COMPATIBLE_MODEL")
                and os.getenv("KESTREL_IT_OPENAI_COMPATIBLE_BASE_URL")
            ),
            reason="set KESTREL_IT_OPENAI_COMPATIBLE_BASE_URL and KESTREL_IT_OPENAI_COMPATIBLE_MODEL",
        ),
        ProviderCase(
            name="ollama",
            config=AgentConfig(
                provider="ollama",
                model=os.getenv("KESTREL_IT_OLLAMA_MODEL", ""),
                base_url=os.getenv("KESTREL_IT_OLLAMA_BASE_URL"),
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("KESTREL_IT_OLLAMA_MODEL")),
            reason="set KESTREL_IT_OLLAMA_MODEL and optionally KESTREL_IT_OLLAMA_BASE_URL",
        ),
        ProviderCase(
            name="ollama-cloud",
            config=AgentConfig(
                provider="ollama-cloud",
                model=os.getenv("KESTREL_IT_OLLAMA_CLOUD_MODEL", ""),
                base_url=os.getenv("KESTREL_IT_OLLAMA_CLOUD_BASE_URL"),
                api_key_env="OLLAMA_API_KEY",
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("OLLAMA_API_KEY") and os.getenv("KESTREL_IT_OLLAMA_CLOUD_MODEL")),
            reason="set OLLAMA_API_KEY and KESTREL_IT_OLLAMA_CLOUD_MODEL",
        ),
        ProviderCase(
            name="deepseek",
            config=AgentConfig(
                provider="deepseek",
                model=os.getenv("KESTREL_IT_DEEPSEEK_MODEL", ""),
                api_key_env="DEEPSEEK_API_KEY",
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("DEEPSEEK_API_KEY") and os.getenv("KESTREL_IT_DEEPSEEK_MODEL")),
            reason="set DEEPSEEK_API_KEY and KESTREL_IT_DEEPSEEK_MODEL",
        ),
        ProviderCase(
            name="kimi",
            config=AgentConfig(
                provider="kimi",
                model=os.getenv("KESTREL_IT_KIMI_MODEL", ""),
                api_key_env="MOONSHOT_API_KEY",
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("MOONSHOT_API_KEY") and os.getenv("KESTREL_IT_KIMI_MODEL")),
            reason="set MOONSHOT_API_KEY and KESTREL_IT_KIMI_MODEL",
        ),
        ProviderCase(
            name="openrouter",
            config=AgentConfig(
                provider="openrouter",
                model=os.getenv("KESTREL_IT_OPENROUTER_MODEL", ""),
                api_key_env="OPENROUTER_API_KEY",
                timeout_seconds=_timeout(),
            ),
            available=bool(os.getenv("OPENROUTER_API_KEY") and os.getenv("KESTREL_IT_OPENROUTER_MODEL")),
            reason="set OPENROUTER_API_KEY and KESTREL_IT_OPENROUTER_MODEL",
        ),
        ProviderCase(
            name="codex-cli",
            config=AgentConfig(
                provider="codex-cli",
                model=os.getenv("KESTREL_IT_CODEX_MODEL", "mock"),
                timeout_seconds=_timeout(default=120),
                codex_skip_git_repo_check=True,
            ),
            available=os.getenv("KESTREL_IT_CODEX_CLI") == "1" and shutil.which("codex") is not None,
            reason="set KESTREL_IT_CODEX_CLI=1 and ensure codex is on PATH",
        ),
    ]


def _timeout(default: int = 30) -> int:
    raw = os.getenv("KESTREL_IT_PROVIDER_TIMEOUT_SECONDS")
    return default if raw is None or not raw.strip() else int(raw)


@pytest.fixture(params=_provider_cases(), ids=lambda case: case.name)
def provider_case(request: pytest.FixtureRequest) -> ProviderCase:
    return request.param
