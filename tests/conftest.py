"""Shared pytest fixtures for exercising the ASGI application lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.validation_runner import IsolatedValidationResult


@pytest.fixture(autouse=True)
def isolate_git_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep synthetic Git repositories from inheriting an outer repository."""

    for variable in (
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def started_test_client() -> Iterator[Callable[[Any], Any]]:
    """Enter TestClient contexts so Kestrel startup and shutdown both run."""

    with ExitStack() as stack:
        yield lambda client: stack.enter_context(client)


@pytest.fixture
def contained_validation_stub(monkeypatch: pytest.MonkeyPatch) -> str:
    """Provide deterministic OCI-shaped evidence without running host code."""

    image = "example.invalid/kestrel-validation@sha256:" + "a" * 64

    def run_stub(
        *,
        workspace: Path,
        image: str | None,
        command: list[str],
        timeout_seconds: float,
        expected_repair_snapshot: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> IsolatedValidationResult:
        del workspace, timeout_seconds, expected_repair_snapshot
        returncode = 4 if any("sys.exit(4)" in item for item in command) else 0
        stdout = ""
        if returncode == 0:
            for marker in ("full-flow-ok", "semantic-review-ok"):
                if any(marker in item for item in command):
                    stdout = f"{marker}\n"
                    break
            else:
                stdout = "contained validation\n"
        return IsolatedValidationResult(
            args=tuple(command),
            returncode=returncode,
            stdout=stdout,
            stderr="",
            isolation={
                "schema_version": 1,
                "mode": "oci_snapshot_v1",
                "image": image,
                "network": "none",
                "workspace_mount": "private_read_only_snapshot",
                "host_fallback": False,
                "source_tree_digest": "sha256:" + "b" * 64,
            },
        )

    monkeypatch.setattr(
        "nested_memvid_agent.tools.process_tools.run_isolated_validation",
        run_stub,
    )
    return image
