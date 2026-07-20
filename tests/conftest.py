"""Shared pytest fixtures for exercising the ASGI application lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any

import pytest


@pytest.fixture
def started_test_client() -> Iterator[Callable[[Any], Any]]:
    """Enter TestClient contexts so Kestrel startup and shutdown both run."""

    with ExitStack() as stack:
        yield lambda client: stack.enter_context(client)
