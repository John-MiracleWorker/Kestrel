from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("brain.agent.subsystems")

SubsystemFactory = Callable[[], Awaitable[Any] | Any]


class SubsystemBootstrapper:
    """Lazy initializer and health tracker for optional subsystems."""

    def __init__(self) -> None:
        self._factories: dict[str, SubsystemFactory] = {}
        self._instances: dict[str, Any] = {}
        self._status: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def register(self, name: str, factory: SubsystemFactory, *, eager_instance: Any = None) -> None:
        self._factories[name] = factory
        self._locks.setdefault(name, asyncio.Lock())
        if eager_instance is not None:
            self._instances[name] = eager_instance
            self._status[name] = "ready"
        else:
            self._status.setdefault(name, "registered")

    def set_status(self, name: str, status: str, *, error: str = "") -> None:
        self._status[name] = status
        if error:
            self._errors[name] = error

    async def ensure(self, name: str) -> Any:
        if name in self._instances:
            return self._instances[name]
        factory = self._factories.get(name)
        if factory is None:
            self._status[name] = "unavailable"
            return None

        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            if name in self._instances:
                return self._instances[name]

            self._status[name] = "initializing"
            try:
                value = factory()
                if asyncio.iscoroutine(value):
                    value = await value
                self._instances[name] = value
                self._status[name] = "ready" if value is not None else "unavailable"
                return value
            except Exception as exc:
                self._status[name] = "degraded"
                self._errors[name] = str(exc)
                logger.warning("Subsystem '%s' failed to initialize: %s", name, exc)
                return None

    def status(self, name: str) -> str:
        return self._status.get(name, "unavailable")

    def snapshot(self) -> dict[str, str]:
        return dict(self._status)

    def error(self, name: str) -> str:
        return self._errors.get(name, "")
