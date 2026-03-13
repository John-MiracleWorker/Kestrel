import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kestrel_native as native


def test_ensure_home_layout_creates_native_contract(tmp_path):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))

    assert paths.home.exists()
    assert paths.run_dir.exists()
    assert paths.logs_dir.exists()
    assert paths.audit_dir.exists()
    assert paths.state_dir.exists()
    assert paths.memory_dir.exists()
    assert paths.watchlist_dir.exists()
    assert paths.artifacts_dir.exists()
    assert paths.cache_dir.exists()
    assert paths.models_dir.exists()
    assert paths.config_yml.exists()
    assert paths.heartbeat_md.exists()
    assert paths.workspace_md.exists()
    assert paths.watchlist_yml.exists()


def test_sqlite_state_and_journal_roundtrip(tmp_path):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    state = native.SQLiteStateStore(paths.sqlite_db)
    journal = native.SQLiteEventJournal(paths.sqlite_db)

    state.initialize()
    journal.initialize()

    task = state.create_task(goal="shell:pwd", kind="task", metadata={"workspace_id": "local"})
    assert task["status"] == "queued"

    updated = state.update_task(task["id"], status="running", metadata={"phase": "exec"})
    assert updated["status"] == "running"
    assert updated["metadata"]["phase"] == "exec"

    approval = state.create_approval(task_id=task["id"], operation="shell", command="rm -rf ./tmp")
    pending = state.list_pending_approvals()
    assert len(pending) == 1
    assert pending[0]["id"] == approval["id"]

    resolved = state.resolve_approval(approval["id"], approved=True)
    assert resolved is not None
    assert resolved["status"] == "approved"

    event = journal.append_event(task["id"], "task_started", {"status": "running"})
    events = journal.list_events(task["id"])
    assert len(events) == 1
    assert events[0]["seq"] == event["seq"]
    assert events[0]["type"] == "task_started"

    state.set_daemon_state({"status": "running"})
    state.set_runtime_profile({"runtime_mode": "native"})
    assert state.get_daemon_state()["status"] == "running"
    assert state.get_runtime_profile()["runtime_mode"] == "native"

    inflight = state.create_task(goal="continue indexing", kind="task")
    recovered = state.recover_inflight_tasks()
    recovered_ids = {item["id"] for item in recovered}
    assert inflight["id"] in recovered_ids


def test_exact_vector_store_prefers_relevant_document(tmp_path):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    vectors = native.SQLiteExactVectorStore(paths.sqlite_db)
    vectors.initialize()

    vectors.upsert_text(
        doc_id="doc-native",
        namespace="memory",
        content="Kestrel native daemon runtime profile with local model orchestration",
        metadata={"kind": "runtime"},
    )
    vectors.upsert_text(
        doc_id="doc-unrelated",
        namespace="memory",
        content="Weather forecast and weekend beach plans",
        metadata={"kind": "misc"},
    )

    results = vectors.search_text(namespace="memory", query="native daemon local runtime", limit=2)
    assert results[0]["doc_id"] == "doc-native"
    assert results[0]["metadata"]["kind"] == "runtime"


def test_sqlite_state_store_tracks_paired_nodes(tmp_path):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    state = native.SQLiteStateStore(paths.sqlite_db)
    state.initialize()

    node = state.upsert_paired_node(
        node_id="screen-local",
        node_type="screen",
        capabilities=["screenshot", "desktop_actions"],
        platform_name="windows",
        health="ok",
        address="http://127.0.0.1:9800",
        workspace_binding="local",
    )

    assert node["node_id"] == "screen-local"
    assert state.list_paired_nodes()[0]["capabilities"] == ["screenshot", "desktop_actions"]


def test_native_runtime_policy_and_fake_completion(monkeypatch):
    policy = native.NativeRuntimePolicy(
        {
            "permissions": {
                "broad_local_control": True,
                "require_approval_for_mutations": True,
            }
        }
    )

    read_only = policy.evaluate_command("ls -la")
    mutating = policy.evaluate_command("mkdir build")
    destructive = policy.evaluate_command("rm -rf build")

    assert read_only == {
        "allowed": True,
        "risk_class": "read_only",
        "approval_required": False,
    }
    assert mutating["allowed"] is True
    assert mutating["risk_class"] == "mutating"
    assert mutating["approval_required"] is True
    assert destructive["risk_class"] == "destructive"
    assert destructive["approval_required"] is True

    locked_down = native.NativeRuntimePolicy(
        {
            "permissions": {
                "broad_local_control": False,
                "require_approval_for_mutations": True,
            }
        }
    )
    assert locked_down.evaluate_command("mkdir build")["allowed"] is False

    monkeypatch.setenv("KESTREL_FAKE_MODEL_RESPONSE", "native ok")
    completion = asyncio.run(
        native.complete_local_prompt(
            prompt="status",
            config=native.DEFAULT_CONFIG,
        )
    )
    assert completion["provider"] == "fake"
    assert completion["content"] == "native ok"


def test_control_request_uses_tcp_transport_for_windows(monkeypatch, tmp_path):
    async def handler(reader, writer):
        raw = await reader.readline()
        request = native.json.loads(raw.decode("utf-8"))
        writer.write(
            (
                native.json.dumps(
                    {
                        "request_id": request["request_id"],
                        "ok": True,
                        "done": True,
                        "result": {"status": "running"},
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def scenario():
        server = await asyncio.start_server(handler, host="127.0.0.1", port=0)
        address = server.sockets[0].getsockname()
        monkeypatch.setenv("KESTREL_CONTROL_HOST", "127.0.0.1")
        monkeypatch.setenv("KESTREL_CONTROL_PORT", str(address[1]))
        paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
        with patch.object(native.os, "name", "nt"):
            assert native.control_socket_available(paths) is True
            result = await native.send_control_request("status", paths=paths, timeout_seconds=5)
        server.close()
        await server.wait_closed()
        return result

    result = asyncio.run(scenario())
    assert result == {"status": "running"}
