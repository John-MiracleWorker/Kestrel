from __future__ import annotations

import json
from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.extension_runner import (
    ContainerExecutionRequest,
    ContainerExecutionResult,
)
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.skill_validation import validate_skill_manifest
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools

PINNED_IMAGE = "example.invalid/kestrel-skill@sha256:" + "b" * 64


class RecordingContainerRunner:
    def __init__(self) -> None:
        self.requests: list[ContainerExecutionRequest] = []

    def run(self, request: ContainerExecutionRequest) -> ContainerExecutionResult:
        self.requests.append(request)
        return ContainerExecutionResult(
            success=True,
            stdout="contained output",
            returncode=0,
            content="Container execution completed.",
            tree_digest=request.expected_tree_digest,
            scope_digest=request.scopes.digest(),
        )


def test_container_skill_routes_through_pinned_runner_with_canonical_scopes(tmp_path: Path) -> None:
    from nested_memvid_agent.skill_manager import SkillManager

    state = AgentStateStore(tmp_path / "state.db")
    skill_dir = tmp_path / "skills" / "contained"
    skill_dir.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    inputs.mkdir(parents=True)
    manifest = {
        "id": "contained",
        "name": "Contained",
        "description": "Run only in an OCI sandbox.",
        "risk": "low",
        "runtime": {
            "type": "container",
            "image": PINNED_IMAGE,
            "command": ["python", "/extension/skill.py"],
            "timeout": 5,
        },
        "scopes": {
            "filesystem": [
                {"root": "workspace", "path": "inputs", "access": "read"}
            ],
            "network": {"mode": "none"},
            "secrets": [],
        },
    }
    (skill_dir / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("Read JSON stdin.", encoding="utf-8")
    (skill_dir / "skill.py").write_text("print('contained')\n", encoding="utf-8")
    runner = RecordingContainerRunner()
    manager = SkillManager(tmp_path / "skills", state, container_runner=runner)  # type: ignore[arg-type]
    manager.discover()
    manager.set_enabled("contained", True)
    adapter = manager.tool_adapters()[0]
    assert adapter.wait_for_completion_on_timeout is True
    registry = build_default_tools()
    registry.register(adapter)
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(name=adapter.spec.name, arguments={"task": "inspect inputs"}, id="container-call")

    result = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_executable_skills=True),
            workspace=workspace,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: call.arguments},
        ),
    )

    assert result.success is True
    assert result.data["containment"] == "oci"
    assert result.data["image"] == PINNED_IMAGE
    assert "contained output" in result.content
    assert len(runner.requests) == 1
    request = runner.requests[0]
    assert request.image == PINNED_IMAGE
    assert request.command == ("python", "/extension/skill.py")
    assert request.scopes.to_payload() == {
        "filesystem": [
            {"root": "workspace", "path": "inputs", "access": "read"}
        ],
        "network": {"mode": "none"},
        "secrets": [],
    }
    assert "container-isolated" in adapter.spec.capabilities
    assert "network:none" in adapter.spec.capabilities
    assert any(item.startswith("extension-scope:sha256:") for item in adapter.spec.capabilities)

    too_small = registry.execute(
        ToolCall(
            name=adapter.spec.name,
            arguments={"task": "do not launch"},
            id="small-timeout",
        ),
        ToolContext(
            memory=memory,
            config=AgentConfig(
                allow_executable_skills=True,
                tool_timeout_seconds=1,
            ),
            workspace=workspace,
            approved_tool_call_ids=frozenset({"small-timeout"}),
            approved_tool_call_arguments={"small-timeout": {"task": "do not launch"}},
        ),
    )
    assert too_small.error == "extension_timeout_budget_too_small"
    assert len(runner.requests) == 1


def test_container_skill_manifest_requires_digest_command_and_supported_scopes() -> None:
    invalid = validate_skill_manifest(
        {
            "id": "unsafe-container",
            "description": "Unsafe container declaration.",
            "runtime": {"type": "container", "image": "python:latest", "command": []},
            "scopes": {"network": {"mode": "egress"}, "secrets": ["token"]},
        }
    )

    assert invalid["ok"] is False
    assert {
        "container_image_not_digest_pinned",
        "invalid_container_command",
        "extension_network_scope_unsupported",
    } <= set(invalid["errors"])

    option_shaped = validate_skill_manifest(
        {
            "id": "option-shaped-image",
            "description": "Must not alter the engine argv.",
            "runtime": {
                "type": "container",
                "image": "--env=ESCAPE@sha256:" + "c" * 64,
                "command": ["python", "bad\x7fargument"],
            },
        }
    )
    assert {
        "container_image_not_digest_pinned",
        "invalid_container_command",
    } <= set(option_shaped["errors"])

    writable = validate_skill_manifest(
        {
            "id": "writable-container",
            "description": "Host writes are deferred until staged writeback exists.",
            "runtime": {
                "type": "container",
                "image": PINNED_IMAGE,
                "command": ["python", "/extension/skill.py"],
            },
            "scopes": {
                "filesystem": [
                    {"root": "workspace", "path": "output", "access": "write"}
                ]
            },
        }
    )
    assert "extension_write_scope_unsupported" in writable["errors"]


def test_host_runtime_manifest_remains_discoverable_but_warns_execution_is_disabled() -> None:
    result = validate_skill_manifest(
        {
            "id": "legacy-python",
            "description": "Legacy host runtime.",
            "runtime": {"type": "python", "entrypoint": "skill.py"},
        }
    )

    assert result["ok"] is True
    assert "host_runtime_execution_disabled" in result["warnings"]
