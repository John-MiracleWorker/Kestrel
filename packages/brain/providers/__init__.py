"""
Local LLM provider — wraps llama-cpp-python for on-device inference.
Compatible with the existing LLMEngine from the monolithic app.
"""

import os
import asyncio
import logging
from typing import AsyncIterator

logger = logging.getLogger("brain.providers.local")

# ── Configuration ─────────────────────────────────────────────────────
MODEL_PATH = os.getenv("LOCAL_MODEL_PATH", "./models/llama-3-8b.gguf")
N_CTX = int(os.getenv("LOCAL_MODEL_N_CTX", "4096"))
N_GPU_LAYERS = int(os.getenv("LOCAL_MODEL_N_GPU_LAYERS", "35"))


class LocalProvider:
    """Wrapper around llama-cpp-python for local LLM inference."""

    def __init__(self):
        self._model = None
        self._last_response = ""
        self._loaded = False

    def is_ready(self) -> bool:
        return self._loaded

    @property
    def last_response(self) -> str:
        return self._last_response

    def _ensure_loaded(self):
        """Lazy-load the model on first use."""
        if self._model is not None:
            return

        if not os.path.exists(MODEL_PATH):
            logger.warning(f"Model file not found: {MODEL_PATH}")
            self._loaded = False
            return

        try:
            from llama_cpp import Llama

            logger.info(f"Loading local model: {MODEL_PATH}")
            self._model = Llama(
                model_path=MODEL_PATH,
                n_ctx=N_CTX,
                n_gpu_layers=N_GPU_LAYERS,
                verbose=False,
            )
            self._loaded = True
            logger.info("Local model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load local model: {e}")
            self._loaded = False

    async def stream(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """Stream tokens from the local model."""
        self._ensure_loaded()

        if not self._model:
            yield "[Error: Local model not loaded]"
            return

        self._last_response = ""

        # Run synchronous llama-cpp in a thread executor
        loop = asyncio.get_event_loop()

        # Create completion generator
        def _generate():
            return self._model.create_chat_completion(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )

        stream = await loop.run_in_executor(None, _generate)

        for chunk in stream:
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            token = delta.get("content", "")
            if token:
                self._last_response += token
                yield token

    async def generate(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """Non-streaming generation (collects all tokens)."""
        result = []
        async for token in self.stream(messages, model, temperature, max_tokens):
            result.append(token)
        return "".join(result)
