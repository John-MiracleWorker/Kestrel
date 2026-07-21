from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from nested_memvid_agent.server_models import SecretStoreRequest
from nested_memvid_agent.server_secret_routes import register_secret_routes


def test_secret_route_registration_is_extracted() -> None:
    assert callable(register_secret_routes)


class _FakeApp:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}

    def _decorator(self, method: str, path: str) -> Any:
        def register(handler: Any) -> Any:
            self.routes[(method, path)] = handler
            return handler

        return register

    def get(self, path: str) -> Any:
        return self._decorator("GET", path)

    def post(self, path: str) -> Any:
        return self._decorator("POST", path)

    def delete(self, path: str) -> Any:
        return self._decorator("DELETE", path)


class _HTTPException(Exception):
    def __init__(self, *, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def test_secret_store_route_publishes_only_inside_sensitive_transition() -> None:
    app = _FakeApp()
    events: list[str] = []

    class _Broker:
        def store_secret(self, **kwargs: object) -> dict[str, object]:
            events.append("store")
            return {"id": kwargs["secret_id"] or "token"}

    @contextmanager
    def transition() -> Any:
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    register_secret_routes(
        app,
        http_exception=_HTTPException,
        secret_broker=_Broker(),
        sensitive_material_transition=transition,
    )
    handler = app.routes[("POST", "/api/secrets")]

    result = handler(
        SecretStoreRequest(
            id="token",
            name="TOKEN",
            purpose="test",
            value="opaque",
        )
    )

    assert result == {"id": "token"}
    assert events == ["enter", "store", "exit"]


def test_secret_store_route_reports_failed_quiescence_without_storing() -> None:
    app = _FakeApp()
    stored = False

    class _CodedTransitionError(ValueError):
        code = "mcp_stdio_quiesce_failed"

    class _Broker:
        def store_secret(self, **kwargs: object) -> dict[str, object]:
            nonlocal stored
            del kwargs
            stored = True
            return {}

    @contextmanager
    def transition() -> Any:
        raise _CodedTransitionError("sensitive detail")
        yield

    register_secret_routes(
        app,
        http_exception=_HTTPException,
        secret_broker=_Broker(),
        sensitive_material_transition=transition,
    )
    handler = app.routes[("POST", "/api/secrets")]

    with pytest.raises(_HTTPException) as raised:
        handler(
            SecretStoreRequest(
                name="TOKEN",
                purpose="test",
                value="must-not-store",
            )
        )

    assert raised.value.status_code == 409
    assert raised.value.detail == "mcp_stdio_quiesce_failed"
    assert stored is False
