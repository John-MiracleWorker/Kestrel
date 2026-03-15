import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kestrel_daemon as daemon
import kestrel_native as native
from kestrel_cli import native_chat_tools as native_chat_tools_impl
from kestrel_cli import native_models as native_models_impl


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
    assert paths.tools_dir.exists()
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


def test_extract_json_object_repairs_multiline_string_values():
    payload = """{
  "action": "store_result",
  "summary": "Generated SVG markup",
  "result": "<svg>
  <path d='M0 0 L10 10'/>
</svg>"
}"""

    parsed = native._extract_json_object(payload)

    assert parsed["action"] == "store_result"
    assert parsed["summary"] == "Generated SVG markup"
    assert "<path d='M0 0 L10 10'/>" in parsed["result"]
    assert "\n" in parsed["result"]


def test_chat_tool_categories_default_to_all_native_categories():
    categories = native.resolve_chat_tool_categories(native.DEFAULT_CONFIG)
    assert categories == ("file", "system", "web", "memory", "media", "desktop", "custom")

    desktop_tools = native.get_chat_tools(("desktop",))
    tool_names = [tool["function"]["name"] for tool in desktop_tools]
    assert tool_names == ["take_screenshot"]


def test_take_screenshot_tool_returns_saved_path_and_delivery_status(monkeypatch, tmp_path):
    monkeypatch.setattr(native.Path, "home", lambda: tmp_path)

    def _fake_capture(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-png-data")

    monkeypatch.setattr(native_chat_tools_impl, "_capture_screenshot_to_file", _fake_capture)
    monkeypatch.setattr(
        native_chat_tools_impl,
        "_send_file_to_telegram",
        lambda path, caption="": (True, "Sent to Telegram chat 123."),
    )

    result = native._execute_tool("take_screenshot", {"send_to_telegram": True, "caption": "hello"})

    assert "Screenshot captured successfully." in result
    assert "Sent to Telegram chat 123." in result
    assert str(tmp_path / ".kestrel" / "artifacts" / "media") in result


def test_daemon_screenshot_request_helpers():
    assert daemon._looks_like_screenshot_request("take a screenshot and send it to me")
    assert daemon._looks_like_screenshot_request("can you show me what's on my screen?")
    assert not daemon._looks_like_screenshot_request("summarize this screenshot I uploaded")

    assert daemon._wants_telegram_delivery("take a screenshot and send it to me")
    assert daemon._wants_telegram_delivery("share it with me on telegram")
    assert not daemon._wants_telegram_delivery("take a screenshot and save it locally")


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


def test_native_agent_runner_routes_direct_and_planned_paths(tmp_path, monkeypatch):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    state_store = native.SQLiteStateStore(paths.sqlite_db)
    state_store.initialize()
    runner = native.NativeAgentRunner(
        paths=paths,
        config=native.DEFAULT_CONFIG,
        runtime_policy=native.NativeRuntimePolicy(native.DEFAULT_CONFIG),
        state_store=state_store,
        workspace_root=tmp_path,
    )
    direct_task = state_store.create_task(goal="say hi", kind="chat")

    async def direct_plan(self, goal, history, initial_tool_call):
        return {"mode": "direct_response", "response": "direct answer"}, "fake", "model"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", direct_plan)
    direct = asyncio.run(runner.run(goal="say hi", task_id=direct_task["id"]))
    assert direct.status == "completed"
    assert direct.message == "direct answer"
    assert direct.plan is None
    persisted_direct = state_store.get_task(direct_task["id"])
    assert persisted_direct["status"] == "completed"
    assert persisted_direct["result"]["message"] == "direct answer"

    async def planned_goal(self, goal, history, initial_tool_call):
        return {
            "mode": "plan",
            "summary": "Inspect and report",
            "reasoning": "This needs a step.",
            "steps": [
                {
                    "id": "step_1",
                    "description": "Inspect the task",
                    "success_criteria": "A concise result exists",
                    "preferred_tools": [],
                }
            ],
        }, "fake", "model"

    async def planned_action(self, state, step):
        return {"action": "finish", "scope": "task", "summary": "planned answer"}, "fake", "model"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", planned_goal)
    monkeypatch.setattr(native.NativeAgentRunner, "_next_action", planned_action)
    planned_task = state_store.create_task(goal="inspect this", kind="task")
    planned = asyncio.run(runner.run(goal="inspect this", task_id=planned_task["id"]))
    assert planned.status == "completed"
    assert planned.message == "planned answer"
    assert planned.plan is not None
    assert planned.plan["summary"] == "Inspect and report"


def test_native_agent_runner_builds_fallback_plan_for_empty_write_task(tmp_path, monkeypatch):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    runner = native.NativeAgentRunner(
        paths=paths,
        config=native.DEFAULT_CONFIG,
        runtime_policy=native.NativeRuntimePolicy(native.DEFAULT_CONFIG),
        workspace_root=tmp_path,
    )

    async def empty_plan(self, goal, history, initial_tool_call):
        return {
            "mode": "plan",
            "summary": "Execute the task.",
            "reasoning": "",
            "steps": [],
        }, "fake", "model"

    async def next_action(self, state, step):
        if step["id"] == "step_1":
            assert step["preferred_tools"] == []
            return {"action": "store_result", "summary": "Generated content", "result": "<svg/>"}, "fake", "model"
        assert step["preferred_tools"] == ["write_file"]
        return {"action": "finish", "scope": "task", "summary": "fallback used"}, "fake", "model"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", empty_plan)
    monkeypatch.setattr(native.NativeAgentRunner, "_next_action", next_action)

    outcome = asyncio.run(runner.run(goal="Generate an SVG and save it to my desktop"))
    assert outcome.status == "completed"
    assert outcome.message == "fallback used"
    assert outcome.plan is not None
    assert outcome.plan["summary"] == "Use write_file to complete the requested file task."
    assert [step["id"] for step in outcome.plan["steps"]] == ["step_1", "step_2"]
    assert outcome.state["step_outputs"]["step_1"]["content"] == "<svg/>"


