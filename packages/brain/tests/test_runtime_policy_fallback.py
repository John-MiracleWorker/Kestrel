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

    async def execute(self, *, tool_name, payload):
        return self._result


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
    hybrid = HybridRuntime(
        docker=_FakeDocker({"success": False, "error": "pull access denied for kestrel/sandbox"}),
        native=_FakeNative({"success": True, "output": "ok from native"}),
    )

    result = await hybrid.execute(
        tool_name="code_execute",
        payload={"language": "python", "code": "print('hello')"},
    )

    assert result["success"] is True
    assert result["output"] == "ok from native"
    assert "warnings" in result
    assert "native runtime fallback" in result["warnings"][0].lower()


@pytest.mark.asyncio
async def test_hybrid_runtime_keeps_docker_result_for_non_runtime_error():
    docker_result = {"success": False, "error": "SyntaxError: invalid syntax"}
    hybrid = HybridRuntime(
        docker=_FakeDocker(docker_result),
        native=_FakeNative({"success": True, "output": "should not be used"}),
    )

    result = await hybrid.execute(
        tool_name="code_execute",
        payload={"language": "python", "code": "bad code"},
    )

    assert result == docker_result
