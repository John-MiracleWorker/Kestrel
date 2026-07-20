from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nested_memvid_agent.capability_policy import (
    CapabilityPolicy,
    tool_spec_digest,
)
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.runtime_models import ToolSpec
from nested_memvid_agent.state_store import (
    SCHEMA_VERSION,
    AgentStateStore,
    ApprovalConflictError,
    CapabilityConflictError,
)


def test_capability_override_missing_default_cas_and_audit(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")

    missing = state.get_capability_override(
        "tool",
        "memory.search",
        default_enabled=True,
    )

    assert missing == {
        "kind": "tool",
        "capability_id": "memory.search",
        "enabled": True,
        "revision": 0,
        "resource_digest": None,
        "updated_by": None,
        "created_at": None,
        "updated_at": None,
        "persisted": False,
    }

    disabled = state.set_capability_override(
        "tool",
        "memory.search",
        False,
        expected_revision=0,
        default_enabled=True,
        resource_digest="sha256:first",
        updated_by="owner",
    )
    assert disabled["revision"] == 1
    assert disabled["enabled"] is False
    assert disabled["persisted"] is True

    with pytest.raises(CapabilityConflictError) as raised:
        state.set_capability_override(
            "tool",
            "memory.search",
            True,
            expected_revision=0,
            default_enabled=True,
        )
    assert raised.value.current["revision"] == 1
    assert raised.value.current["enabled"] is False

    enabled = state.set_capability_override(
        "tool",
        "memory.search",
        True,
        expected_revision=1,
        default_enabled=True,
        updated_by="owner",
    )
    assert enabled["revision"] == 2
    assert enabled["resource_digest"] == "sha256:first"
    assert state.list_capability_overrides(kind="tool") == [enabled]

    changes = state.list_capability_changes(
        kind="tool",
        capability_id="memory.search",
    )
    assert [(item["previous_revision"], item["revision"]) for item in changes] == [
        (1, 2),
        (0, 1),
    ]
    assert changes[0]["previous_enabled"] is False
    assert changes[0]["enabled"] is True


def test_capability_override_compare_and_swap_is_atomic_across_stores(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    AgentStateStore(path)

    def update(enabled: bool) -> tuple[str, int]:
        store = AgentStateStore(path)
        try:
            row = store.set_capability_override(
                "skill",
                "review",
                enabled,
                expected_revision=0,
                default_enabled=False,
            )
        except CapabilityConflictError as exc:
            return "conflict", int(exc.current["revision"])
        return "updated", int(row["revision"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (True, False)))

    assert sorted(results) == [("conflict", 1), ("updated", 1)]
    assert len(AgentStateStore(path).list_capability_changes()) == 1


def test_capability_override_delete_revokes_and_forgets_recreated_resource(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.set_capability_override(
        "mcp_server",
        "local",
        True,
        expected_revision=0,
        default_enabled=False,
        resource_digest="sha256:old-server",
    )

    assert state.delete_capability_override("mcp_server", "local") is True
    assert state.delete_capability_override("mcp_server", "local") is False
    recreated = state.get_capability_override(
        "mcp_server",
        "local",
        default_enabled=False,
    )
    assert recreated["enabled"] is False
    assert recreated["revision"] == 0
    assert recreated["persisted"] is False
    deletion = state.list_capability_changes(
        kind="mcp_server",
        capability_id="local",
        limit=1,
    )[0]
    assert deletion["previous_enabled"] is True
    assert deletion["enabled"] is False
    assert deletion["previous_revision"] == 1
    assert deletion["revision"] == 2


@pytest.mark.parametrize("kind", ["plugin", "", "TOOLING"])
def test_capability_override_rejects_unsupported_kinds(tmp_path: Path, kind: str) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="capability kind"):
        state.get_capability_override(kind, "demo", default_enabled=True)


def test_schema_15_migrates_approval_capability_binding_and_adds_policy_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v14.db"
    now = datetime.now(UTC)
    expires_at = (now + timedelta(minutes=10)).isoformat()
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE approval_requests (
                approval_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                risk TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_json TEXT,
                result_json TEXT,
                principal TEXT NOT NULL DEFAULT 'owner',
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO schema_version VALUES (1, 14, ?)",
            (now.isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO approval_requests (
                approval_id, run_id, tool_call_id, tool_name, arguments_json,
                risk, status, principal, expires_at, created_at, updated_at
            ) VALUES ('approval_old', 'run_old', 'call_old', 'shell.run', '{}',
                'high', 'pending', 'owner', ?, ?, ?)
            """,
            (expires_at, now.isoformat(), now.isoformat()),
        )

    state = AgentStateStore(path)

    assert state.schema_version() == SCHEMA_VERSION == 19
    approval = state.get_approval("approval_old")
    assert approval["capability_revision"] == 0
    assert approval["resource_digest"] == ""
    assert state.list_capability_overrides() == []
    assert state.list_capability_changes() == []


def test_pending_approval_identity_includes_capability_revision_and_digest(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    approval = state.create_approval(
        approval_id="approval_bound",
        run_id="run_bound",
        tool_call_id="call_bound",
        tool_name="shell.run",
        arguments={"command": ["echo", "bound"]},
        risk="high",
        expires_at=expires_at,
        capability_revision=4,
        resource_digest="sha256:tool-and-config",
    )
    assert approval["capability_revision"] == 4
    assert approval["resource_digest"] == "sha256:tool-and-config"

    reused, created = state.create_approval_once(
        approval_id="approval_retry",
        run_id="run_bound",
        tool_call_id="call_bound",
        tool_name="shell.run",
        arguments={"command": ["echo", "bound"]},
        risk="high",
        expires_at=expires_at,
        capability_revision=4,
        resource_digest="sha256:tool-and-config",
    )
    assert created is False
    assert reused["approval_id"] == "approval_bound"

    with pytest.raises(ApprovalConflictError):
        state.create_approval_once(
            approval_id="approval_changed",
            run_id="run_bound",
            tool_call_id="call_bound",
            tool_name="shell.run",
            arguments={"command": ["echo", "bound"]},
            risk="high",
            expires_at=expires_at,
            capability_revision=5,
            resource_digest="sha256:changed",
        )


def test_policy_defaults_high_risk_tools_off_until_explicitly_enabled(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    policy = CapabilityPolicy(state, AgentConfig())
    low = ToolSpec(
        name="memory.search",
        description="Search memory",
        parameters={"type": "object"},
    )
    high = ToolSpec(
        name="git.create_local_branch",
        description="Create branch",
        parameters={"type": "object"},
        risk="high",
        requires_approval=True,
    )

    assert policy.tool_decision(low).to_public_dict() == {
        "default_enabled": True,
        "configured_enabled": True,
        "effective_enabled": True,
        "blocked_by": [],
        "revision": 0,
        "updated_at": None,
        "enablement_flag": None,
    }
    disabled = policy.tool_decision(high)
    assert disabled.default_enabled is False
    assert disabled.effective_enabled is False
    assert disabled.blocked_by == ("tool:git.create_local_branch",)

    digest = tool_spec_digest(high)
    state.set_capability_override(
        "tool",
        high.name,
        True,
        expected_revision=0,
        default_enabled=False,
        resource_digest=digest,
    )
    enabled = policy.tool_decision(high)
    assert enabled.configured_enabled is True
    assert enabled.effective_enabled is True
    assert enabled.revision == 1

    changed_spec = ToolSpec(
        name=high.name,
        description="Create a materially changed branch",
        parameters=high.parameters,
        risk="high",
        requires_approval=True,
    )
    changed = policy.tool_decision(changed_spec)
    assert changed.configured_enabled is True
    assert changed.effective_enabled is False
    assert changed.blocked_by == ("resource_changed",)


def test_policy_combines_current_config_and_parent_capability_state(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    configs = [AgentConfig()]
    policy = CapabilityPolicy(state, lambda: configs[0])
    shell = ToolSpec(
        name="shell.run",
        description="Run a command",
        parameters={"type": "object"},
        risk="high",
        requires_approval=True,
    )

    gated = policy.tool_decision(shell)
    assert gated.default_enabled is True
    assert gated.configured_enabled is True
    assert gated.effective_enabled is False
    assert gated.blocked_by == ("config:allow_shell",)

    configs[0] = AgentConfig(allow_shell=True)
    assert policy.tool_decision(shell).effective_enabled is True

    state.upsert_skill(
        {
            "id": "review",
            "name": "Review",
            "description": "Review work",
            "path": str(tmp_path / "skills" / "review"),
            "manifest": {},
            "enabled": True,
        }
    )
    skill_tool = ToolSpec(
        name="skill.review.run",
        description="Run review",
        parameters={"type": "object"},
        source="skill",
        skill_id="review",
    )
    assert policy.tool_decision(skill_tool).blocked_by == (
        "tool:skill.review.run",
    )
    state.set_capability_override(
        "tool",
        skill_tool.name,
        True,
        expected_revision=0,
        default_enabled=False,
        resource_digest=tool_spec_digest(skill_tool),
    )
    assert policy.tool_decision(skill_tool).effective_enabled is True
    state.set_capability_override(
        "skill",
        "review",
        False,
        expected_revision=0,
        default_enabled=True,
    )
    child_disabled = policy.tool_decision(skill_tool)
    assert child_disabled.effective_enabled is False
    assert child_disabled.blocked_by == ("skill:review",)

    state.upsert_mcp_server(
        {
            "id": "local",
            "name": "Local",
            "transport": "stdio",
            "enabled": False,
            "tools": [],
        }
    )
    mcp_tool = ToolSpec(
        name="mcp.local.echo",
        description="Echo",
        parameters={"type": "object"},
        source="mcp",
        server_id="local",
    )
    assert policy.tool_decision(mcp_tool).blocked_by == (
        "tool:mcp.local.echo",
        "mcp_server:local",
    )
    state.set_capability_override(
        "tool",
        mcp_tool.name,
        True,
        expected_revision=0,
        default_enabled=False,
        resource_digest=tool_spec_digest(mcp_tool),
    )
    state.set_capability_override(
        "mcp_server",
        "local",
        True,
        expected_revision=0,
        default_enabled=False,
    )
    assert policy.tool_decision(mcp_tool).effective_enabled is True


def test_policy_fails_closed_for_missing_parent_and_launch_allowlist(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    policy = CapabilityPolicy(
        state,
        AgentConfig(enabled_tools=("memory.search",)),
    )
    file_read = ToolSpec(
        name="file.read",
        description="Read a file",
        parameters={"type": "object"},
    )
    missing_skill = ToolSpec(
        name="skill.missing.run",
        description="Missing skill",
        parameters={"type": "object"},
        source="skill",
        skill_id="missing",
    )

    assert policy.tool_decision(file_read).blocked_by == ("config:enabled_tools",)
    assert policy.tool_decision(missing_skill).blocked_by == (
        "tool:skill.missing.run",
        "skill_missing:missing",
    )
