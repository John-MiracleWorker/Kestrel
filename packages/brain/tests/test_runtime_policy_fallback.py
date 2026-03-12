import pytest

from agent.runtime.policy import HybridRuntime, _should_fallback_to_native


class _FakeDocker:
    def __init__(self, result, supports=True):
        self._result = result
        self.capabilities = type("Caps", (), {"supports_docker_execution": supports})()

    async def execute(self, *, tool_name, payload):
        return self._result


class _FakeNative:
    def __init__(self, result):
        self._result = result
        self.last_payload = None

    async def execute(self, *, tool_name, payload):
        self.last_payload = payload
        return dict(self._result)


@pytest.mark.parametrize(
    "error_text,expected",
    [
        ("pull access denied for kestrel/sandbox", True),
        ("image not found", True),
        ("Hands service is not connected", True),
        ("syntax error in python code", False),
        ("", False),
    ],
)
def test_should_fallback_to_native_detects_runtime_errors(error_text, expected):
    result = _should_fallback_to_native(
        language="python",
        docker_result={"success": False, "error": error_text},
    )
    assert result is expected


@pytest.mark.asyncio
async def test_hybrid_runtime_falls_back_to_native_when_docker_image_missing():
    native = _FakeNative({"success": True, "output": "ok from native", "runtime_class": "hybrid_native_fallback"})
    hybrid = HybridRuntime(
        docker=_FakeDocker({"success": False, "error": "pull access denied for kestrel/sandbox"}),
        native=native,
    )

    result = await hybrid.execute(
        tool_name="code_execute",
        payload={"language": "python", "code": "print('hello')"},
    )

    assert result["success"] is True
    assert result["output"] == "ok from native"
    assert "warnings" in result
    assert "native runtime fallback" in result["warnings"][0].lower()
    assert result["fallback_used"] is True
    assert result["fallback_from"] == "sandboxed_docker"
    assert result["fallback_to"] == "hybrid_native_fallback"
    assert native.last_payload["_fallback_from"] == "sandboxed_docker"


@pytest.mark.asyncio
async def test_hybrid_runtime_keeps_docker_result_for_non_runtime_error():
    docker_result = {"success": False, "error": "SyntaxError: invalid syntax", "runtime_class": "sandboxed_docker"}
    hybrid = HybridRuntime(
        docker=_FakeDocker(docker_result),
        native=_FakeNative({"success": True, "output": "should not be used"}),
    )

    result = await hybrid.execute(
        tool_name="code_execute",
        payload={"language": "python", "code": "bad code"},
    )

    assert result == docker_result


@pytest.mark.asyncio
async def test_hybrid_runtime_marks_native_execution_when_docker_capability_is_unavailable():
    native = _FakeNative({"success": True, "output": "native direct", "runtime_class": "hybrid_native_fallback"})
    hybrid = HybridRuntime(
        docker=_FakeDocker({"success": False, "error": "unused"}, supports=False),
        native=native,
    )

    result = await hybrid.execute(
        tool_name="code_execute",
        payload={"language": "python", "code": "print('hi')"},
    )

    assert result["success"] is True
    assert result["fallback_used"] is True
    assert result["fallback_from"] == "sandboxed_docker"
    assert "docker sandbox unavailable" in result["warnings"][0].lower()
    assert native.last_payload["_fallback_reason"]
