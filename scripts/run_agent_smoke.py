from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from nested_memvid_agent.app_factory import build_agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.models import MemoryLayer


def _isolated_config(root: Path) -> AgentConfig:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    return AgentConfig(
        provider="mock",
        model="mock",
        backend="memory",
        memory_dir=root / "memory",
        log_dir=root / "logs",
        state_path=root / "state" / "agent.db",
        secret_store_path=root / "secrets" / "local_vault.json",
        workspace=workspace,
        skills_dir=root / "skills",
        plugins_dir=root / "plugins",
        mcp_config_path=root / "config" / "mcp_servers.json",
        channel_config_path=root / "config" / "channels.json",
        worker_worktree_dir=root / "worktrees",
        allow_web=False,
        allow_shell=False,
        allow_file_write=False,
        allow_policy_writes=False,
        allow_git_commit=False,
        allow_git_push=False,
        allow_remote_mutation=False,
        enable_autonomous_scheduler=False,
        enable_proactive_routines=False,
    )


def _configured_paths(config: AgentConfig) -> tuple[Path, ...]:
    return (
        config.memory_dir,
        config.log_dir,
        config.state_path,
        config.secret_store_path,
        config.workspace,
        config.skills_dir,
        config.plugins_dir,
        config.mcp_config_path,
        config.channel_config_path,
        config.worker_worktree_dir,
    )


def _under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def run_agent_smoke(*, root: Path, marker: str) -> dict[str, Any]:
    root = root.resolve(strict=False)
    marker = marker.strip()
    if not marker:
        raise ValueError("Smoke marker must be nonempty")
    root.mkdir(parents=True, exist_ok=False)
    config = _isolated_config(root)
    isolated_paths = _configured_paths(config)
    agent = build_agent(config)
    try:
        write_session = f"smoke-write-{uuid4().hex}"
        search_session = f"smoke-search-{uuid4().hex}"
        first_prompt = f"Remember this fresh smoke marker exactly: {marker}"
        expected_first_answer = f"Mock response: {first_prompt}"
        first = agent.chat(first_prompt, session_id=write_session)

        ordinary_records = [
            record
            for record_id in first.memory_writes
            if (
                record := agent.memory.get_record(
                    None,
                    record_id,
                    include_inactive=True,
                )
            )
            is not None
        ]
        ordinary_policy_writes = [
            record.id for record in ordinary_records if record.layer == MemoryLayer.POLICY
        ]
        policy_records_after_ordinary = list(
            agent.memory.iter_records(layer=MemoryLayer.POLICY, include_inactive=True)
        )

        second = agent.chat(f"/search {marker}", session_id=search_session)
        exact_searches = [
            execution
            for execution in second.tool_executions
            if execution.call.name == "memory.search"
            and execution.call.arguments == {"query": marker, "k": 5}
            and execution.success
        ]
        search_execution = exact_searches[0] if len(exact_searches) == 1 else None
        search_hits = search_execution.data.get("hits", []) if search_execution is not None else []
        marker_hit = any(
            hit.get("layer") in {MemoryLayer.WORKING.value, MemoryLayer.EPISODIC.value}
            and marker in str(hit.get("snippet", ""))
            for hit in search_hits
        )

        assertions = {
            "all_configured_paths_under_unique_root": all(
                _under_root(path, root) for path in isolated_paths
            ),
            "ordinary_turn_completed": first.stop_reason == "complete",
            "ordinary_turn_used_exact_mock_conversation": (
                first.session_id == write_session
                and first.user_message == first_prompt
                and first.assistant_message == expected_first_answer
                and not first.tool_executions
            ),
            "ordinary_turn_wrote_memory": bool(first.memory_writes),
            "ordinary_turn_persisted_fresh_marker": any(
                record.layer in {MemoryLayer.WORKING, MemoryLayer.EPISODIC}
                and marker in record.content
                for record in ordinary_records
            ),
            "ordinary_turn_did_not_write_policy": not ordinary_policy_writes,
            "policy_layer_empty_after_ordinary_turn": not policy_records_after_ordinary,
            "search_used_fresh_session": (first.session_id != second.session_id == search_session),
            "search_completed": second.stop_reason == "complete",
            "exact_memory_search_succeeded": search_execution is not None,
            "memory_search_returned_fresh_marker": marker_hit,
            "search_answer_is_exact_tool_evidence": bool(
                search_execution is not None
                and second.assistant_message == search_execution.content
                and marker in second.assistant_message
            ),
        }
        return {
            "schema": "kestrel.agent_smoke.v1",
            "mode": "isolated_mock",
            "marker": marker,
            "assertions": assertions,
            "passed": all(assertions.values()),
            "evidence": {
                "write_session": write_session,
                "search_session": search_session,
                "ordinary_memory_write_count": len(first.memory_writes),
                "ordinary_policy_write_ids": ordinary_policy_writes,
                "policy_record_count_after_ordinary": len(policy_records_after_ordinary),
                "search_tool_count": len(second.tool_executions),
                "search_hit_count": len(search_hits),
                "search_stop_reason": second.stop_reason,
            },
            "isolation": {
                "root": str(root),
                "configured_paths": [str(path) for path in isolated_paths],
            },
        }
    finally:
        agent.close()


def main() -> int:
    marker = f"kestrel_smoke_{uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="kestrel-agent-smoke-") as tmpdir:
        report = run_agent_smoke(root=Path(tmpdir) / "run", marker=marker)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
