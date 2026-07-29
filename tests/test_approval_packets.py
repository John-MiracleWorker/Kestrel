from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.engineering.approval_packets import (
    ApprovalPacketCall,
    ApprovalPacketService,
)
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution, ToolSpec
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.registry import ToolRegistry


def _service(tmp_path: Path) -> tuple[AgentStateStore, ApprovalPacketService]:
    state = AgentStateStore(tmp_path / "state.sqlite3")
    state.create_run(
        run_id="run_packet",
        message="Repair",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    return state, ApprovalPacketService(state)


def _calls() -> tuple[ApprovalPacketCall, ...]:
    return (
        ApprovalPacketCall(
            tool_call_id="call_patch",
            tool_name="repair.apply_patch",
            arguments={"patch": "*** Begin Patch\n*** End Patch"},
            risk="high",
            capability_revision=3,
            resource_digest="a" * 64,
            reason="Apply the reviewed bounded patch.",
            resource_scope="repository worktree",
            expected_side_effect="Modify one candidate file.",
            rollback="Discard the isolated worktree.",
        ),
        ApprovalPacketCall(
            tool_call_id="call_validate",
            tool_name="repair.validate",
            arguments={"command": ["pytest", "-q"]},
            risk="high",
            capability_revision=4,
            resource_digest="b" * 64,
            reason="Validate the exact candidate.",
            resource_scope="OCI candidate snapshot",
            expected_side_effect="Run tests without host mutation.",
            rollback="No repository rollback required.",
        ),
    )


def test_packet_keeps_individual_exact_call_records_and_digest_binds_decision(
    tmp_path: Path,
) -> None:
    _state, service = _service(tmp_path)
    packet = service.create(
        packet_id="packet_repair",
        run_id="run_packet",
        objective="Repair parser safely.",
        calls=_calls(),
        actor="planner",
        checkpoint="Before patch and validation",
    )

    assert packet.status == "pending"
    assert len(packet.calls) == 2
    assert len({item.call_digest for item in packet.calls}) == 2
    assert all(item.status == "pending" for item in packet.calls)
    with pytest.raises(ValueError, match="packet digest"):
        service.decide(
            "packet_repair",
            expected_packet_digest="0" * 64,
            decisions={"call_patch": True, "call_validate": True},
            actor="owner",
        )

    decided = service.decide(
        "packet_repair",
        expected_packet_digest=packet.packet_digest,
        decisions={"call_patch": True, "call_validate": False},
        actor="owner",
    )
    assert decided.status == "decided"
    assert {item.tool_call_id: item.status for item in decided.calls} == {
        "call_patch": "approved",
        "call_validate": "denied",
    }


def test_packet_grant_is_single_use_and_every_binding_is_revalidated(
    tmp_path: Path,
) -> None:
    _state, service = _service(tmp_path)
    packet = service.create(
        packet_id="packet_single_use",
        run_id="run_packet",
        objective="Apply patch.",
        calls=(_calls()[0],),
        actor="planner",
    )
    service.decide(
        packet.packet_id,
        expected_packet_digest=packet.packet_digest,
        decisions={"call_patch": True},
        actor="owner",
    )

    grant = service.consume_exact(
        run_id="run_packet",
        tool_call_id="call_patch",
        tool_name="repair.apply_patch",
        arguments={"patch": "*** Begin Patch\n*** End Patch"},
        risk="high",
        capability_revision=3,
        resource_digest="a" * 64,
    )
    assert grant is not None
    assert grant.status == "consumed"
    assert (
        service.consume_exact(
            run_id="run_packet",
            tool_call_id="call_patch",
            tool_name="repair.apply_patch",
            arguments={"patch": "*** Begin Patch\n*** End Patch"},
            risk="high",
            capability_revision=3,
            resource_digest="a" * 64,
        )
        is None
    )


def test_argument_or_capability_drift_invalidates_preapproval(tmp_path: Path) -> None:
    _state, service = _service(tmp_path)
    packet = service.create(
        packet_id="packet_drift",
        run_id="run_packet",
        objective="Apply patch.",
        calls=(_calls()[0],),
        actor="planner",
    )
    service.decide(
        packet.packet_id,
        expected_packet_digest=packet.packet_digest,
        decisions={"call_patch": True},
        actor="owner",
    )

    assert (
        service.consume_exact(
            run_id="run_packet",
            tool_call_id="call_patch",
            tool_name="repair.apply_patch",
            arguments={"patch": "changed"},
            risk="high",
            capability_revision=3,
            resource_digest="a" * 64,
        )
        is None
    )
    invalidated = service.get(packet.packet_id)
    assert invalidated.calls[0].status == "invalidated"
    assert invalidated.calls[0].decision["reason"] == "exact_call_binding_changed"


def test_parent_capability_revocation_invalidates_every_affected_pending_call(
    tmp_path: Path,
) -> None:
    _state, service = _service(tmp_path)
    packet = service.create(
        packet_id="packet_revoke",
        run_id="run_packet",
        objective="Repair.",
        calls=_calls(),
        actor="planner",
    )
    service.decide(
        packet.packet_id,
        expected_packet_digest=packet.packet_digest,
        decisions={"call_patch": True, "call_validate": True},
        actor="owner",
    )

    count = service.invalidate_tools(
        {"repair.apply_patch"},
        reason="capability_disabled",
    )

    assert count == 1
    current = service.get(packet.packet_id)
    assert {item.tool_call_id: item.status for item in current.calls} == {
        "call_patch": "invalidated",
        "call_validate": "approved",
    }


def test_packet_rejects_sensitive_display_text_before_persistence(
    tmp_path: Path,
) -> None:
    _state, service = _service(tmp_path)
    secret = "approval-packet-sensitive-value-74139"  # gitleaks:allow
    register_secret_value(secret)

    with pytest.raises(ValueError, match="sensitive material"):
        service.create(
            packet_id="packet_sensitive",
            run_id="run_packet",
            objective=f"Repair using {secret}",
            calls=(_calls()[0],),
            actor="planner",
        )

    assert service.list(run_id="run_packet") == []


def test_registry_executes_a_packet_authorized_exact_call_once(tmp_path: Path) -> None:
    class MutatingTool(AgentTool):
        spec = ToolSpec(
            name="repair.apply_patch",
            description="Apply one reviewed patch.",
            parameters={"type": "object", "properties": {"patch": {"type": "string"}}},
            risk="high",
            requires_approval=True,
        )

        def __init__(self) -> None:
            self.calls = 0

        def run(
            self,
            arguments: dict[str, Any],
            context: ToolContext,
        ) -> ToolExecution:
            del context
            self.calls += 1
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments=arguments),
                success=True,
                content="applied",
                data={"changed": True},
            )

    tool = MutatingTool()
    registry = ToolRegistry()
    registry.register(tool)
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        id="call_patch",
        name=tool.spec.name,
        arguments={"patch": "*** Begin Patch\n*** End Patch"},
    )

    def packet_grant(
        requested: ToolCall,
        _spec: ToolSpec,
        _context: ToolContext,
    ) -> ToolExecution:
        return ToolExecution(
            call=requested,
            success=True,
            content="authorized",
            data={
                "runtime_exact_call_approved": True,
                "packet_id": "packet_repair",
                "packet_call_id": "packet_call_repair",
                "call_digest": "c" * 64,
                "capability_revision": 3,
                "resource_digest": "d" * 64,
            },
        )

    result = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_file_write=True),
            workspace=tmp_path,
            run_id="run_packet",
            approval_handler=packet_grant,
        ),
    )

    assert result.success is True
    assert result.data["changed"] is True
    assert result.data["approval_packet"]["packet_id"] == "packet_repair"
    assert tool.calls == 1
