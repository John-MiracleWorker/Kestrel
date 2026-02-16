"""
Tests for Brain LLM provider initialization and routing.
"""

import os
from unittest.mock import patch, MagicMock
import pytest


# ── Test LocalProvider ────────────────────────────────────────────────

def test_local_provider_not_ready_without_model():
    """LocalProvider should not be ready when model file doesn't exist."""
    with patch.dict(os.environ, {"LOCAL_MODEL_PATH": "/nonexistent/model.gguf"}):
        # Re-import to pick up env change
        from providers.local import LocalProvider
        provider = LocalProvider()
        assert provider.is_ready() is False


def test_local_provider_last_response_default():
    """last_response should be empty string before any generation."""
    from providers.local import LocalProvider
    provider = LocalProvider()
    assert provider.last_response == ""


@pytest.mark.asyncio
async def test_local_provider_stream_error_without_model():
    """Streaming without a loaded model should yield an error message."""
    with patch.dict(os.environ, {"LOCAL_MODEL_PATH": "/nonexistent/model.gguf"}):
        from providers.local import LocalProvider
        provider = LocalProvider()

        tokens = []
        async for token in provider.stream([{"role": "user", "content": "hello"}]):
            tokens.append(token)

        assert len(tokens) == 1
        assert "Error" in tokens[0] or "not loaded" in tokens[0]


# ── Test CloudProvider ────────────────────────────────────────────────

def test_cloud_provider_initializes():
    """CloudProvider should initialize with a provider name."""
    from providers.cloud import CloudProvider
    provider = CloudProvider("openai")
    assert provider is not None


# ── Test Provider Registry ────────────────────────────────────────────

def test_get_provider_local():
    """get_provider('local') should return a LocalProvider."""
    from server import get_provider
    from providers.local import LocalProvider
    provider = get_provider("local")
    assert isinstance(provider, LocalProvider)


def test_get_provider_cloud():
    """get_provider('openai') should return a CloudProvider."""
    from server import get_provider
    from providers.cloud import CloudProvider
    provider = get_provider("openai")
    assert isinstance(provider, CloudProvider)


def test_get_provider_caches():
    """get_provider should return the same instance on subsequent calls."""
    from server import get_provider
    p1 = get_provider("anthropic")
    p2 = get_provider("anthropic")
    assert p1 is p2
