"""Backward-compatible entrypoint wrapper for the Brain application."""

import asyncio

from app import serve
from db import get_pool, get_redis
from providers_registry import get_available_providers, get_provider, resolve_provider

__all__ = [
    "get_available_providers",
    "get_pool",
    "get_provider",
    "get_redis",
    "resolve_provider",
    "serve",
]


if __name__ == "__main__":
    asyncio.run(serve())
