"""Thin entrypoint wrapper for the Brain application."""

import asyncio

from app import serve


if __name__ == "__main__":
    asyncio.run(serve())
