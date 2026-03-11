from pathlib import Path
from types import SimpleNamespace

import executor as executor_module
from executor import DockerExecutor


class _FakeImages:
    def __init__(self, existing):
        self._existing = existing
        self.build_calls = []

    def list(self, name):
        self.list_name = name
        return self._existing

    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        return SimpleNamespace(id="sha256:testimage"), []


class _FakeClient:
    def __init__(self, images):
        self.images = images


def _sandbox_context_path() -> str:
    return str(Path(executor_module.__file__).with_name("sandbox"))


def test_ensure_sandbox_image_skips_build_when_image_exists(monkeypatch):
    monkeypatch.setattr(executor_module, "SANDBOX_BUILD_CONTEXT", _sandbox_context_path())
    monkeypatch.setattr(executor_module, "SANDBOX_IMAGE", "kestrel/sandbox:latest")

    images = _FakeImages(existing=[object()])
    executor = DockerExecutor()

    executor._ensure_sandbox_image_sync(_FakeClient(images))

    assert images.list_name == "kestrel/sandbox:latest"
    assert images.build_calls == []


def test_ensure_sandbox_image_builds_missing_image(monkeypatch):
    monkeypatch.setattr(executor_module, "SANDBOX_BUILD_CONTEXT", _sandbox_context_path())
    monkeypatch.setattr(executor_module, "SANDBOX_IMAGE", "kestrel/sandbox:latest")
    monkeypatch.setattr(executor_module, "SANDBOX_AUTO_BUILD", True)

    images = _FakeImages(existing=[])
    executor = DockerExecutor()

    executor._ensure_sandbox_image_sync(_FakeClient(images))

    assert images.build_calls == [
        {
            "path": _sandbox_context_path(),
            "dockerfile": "Dockerfile",
            "tag": "kestrel/sandbox:latest",
            "rm": True,
        }
    ]
