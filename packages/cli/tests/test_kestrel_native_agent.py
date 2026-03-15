import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kestrel_daemon as daemon
import kestrel_native as native
from kestrel_cli import native_agent_base as native_agent_base_impl
from kestrel_cli import native_chat_tools as native_chat_tools_impl
from kestrel_cli import native_models as native_models_impl


def test_native_agent_runner_searches_todos_without_approval(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("Intro\nTODO: write docs\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("# TODO: clean this up\nprint('ok')\n", encoding="utf-8")
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    runner = native.NativeAgentRunner(
        paths=paths,
        config=native.DEFAULT_CONFIG,
        runtime_policy=native.NativeRuntimePolicy(native.DEFAULT_CONFIG),
        workspace_root=tmp_path,
    )

    async def plan_goal(self, goal, history, initial_tool_call):
        return {
            "mode": "plan",
            "summary": "Find TODOs",
            "reasoning": "Search the repo and summarize the matches.",
            "steps": [
                {
                    "id": "step_1",
                    "description": "Search for TODO markers",
                    "success_criteria": "Matching files are identified",
                    "preferred_tools": ["search_files"],
                }
            ],
        }, "fake", "model"

    async def next_action(self, state, step):
        if not state.get("tool_evidence"):
            return {
                "action": "tool_call",
                "tool_name": "search_files",
                "arguments": {"query": "TODO", "path": str(tmp_path), "limit": 10},
            }, "fake", "model"
        return {"action": "finish", "scope": "task", "summary": "Found TODOs in the repo."}, "fake", "model"

    async def verify(self, state, draft_response):
        return {"ok": True, "final_response": draft_response, "reason": "grounded"}, "fake", "model"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", plan_goal)
    monkeypatch.setattr(native.NativeAgentRunner, "_next_action", next_action)
    monkeypatch.setattr(native.NativeAgentRunner, "_verify_response", verify)

    outcome = asyncio.run(runner.run(goal="find TODOs in this repo and summarize them"))
    assert outcome.status == "completed"
    assert outcome.approval is None
    assert outcome.plan is not None
    assert outcome.state["tool_evidence"][0]["tool_name"] == "search_files"
    matches = outcome.state["tool_evidence"][0]["data"]["matches"]
    assert len(matches) == 2


def test_native_agent_runner_write_file_requires_approval_and_resumes(tmp_path, monkeypatch):
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
    task = state_store.create_task(goal="create a README section and save it", kind="task")
    readme_path = tmp_path / "README.md"
    readme_text = "## Native Agent Loop\nImplemented.\n"

    async def plan_goal(self, goal, history, initial_tool_call):
        return {
            "mode": "plan",
            "summary": "Write the requested README section",
            "reasoning": "This mutates a file, so approval is required.",
            "steps": [
                {
                    "id": "step_1",
                    "description": "Write the README update",
                    "success_criteria": "README contains the requested section",
                    "preferred_tools": ["write_file"],
                }
            ],
        }, "fake", "model"

    async def next_action(self, state, step):
        if not state.get("tool_evidence"):
            return {
                "action": "tool_call",
                "tool_name": "write_file",
                "arguments": {"path": str(readme_path), "content": readme_text},
            }, "fake", "model"
        return {
            "action": "finish",
            "scope": "task",
            "summary": f"Saved the README section to {readme_path}",
        }, "fake", "model"

    async def verify(self, state, draft_response):
        return {"ok": True, "final_response": draft_response, "reason": "grounded"}, "fake", "model"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", plan_goal)
    monkeypatch.setattr(native.NativeAgentRunner, "_next_action", next_action)
    monkeypatch.setattr(native.NativeAgentRunner, "_verify_response", verify)

    pending = asyncio.run(runner.run(goal=task["goal"], task_id=task["id"]))
    assert pending.status == "waiting_approval"
    approvals = state_store.list_pending_approvals()
    assert len(approvals) == 1
    assert approvals[0]["operation"] == "file_write"

    approved = state_store.resolve_approval(approvals[0]["id"], approved=True)
    resumed = asyncio.run(
        runner.run(
            goal=task["goal"],
            task_id=task["id"],
            resume_state=approved["resume"]["state"],
            approved=True,
        )
    )
    assert resumed.status == "completed"
    assert readme_path.read_text(encoding="utf-8") == readme_text
    assert str(readme_path) in resumed.message
    assert resumed.state["tool_evidence"][0]["artifacts"][0]["path"] == str(readme_path)


def test_native_agent_runner_persists_intermediate_step_output(tmp_path, monkeypatch):
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
    task = state_store.create_task(goal="create an SVG and save it", kind="task")
    svg_path = tmp_path / "Desktop" / "hand.svg"
    svg_text = "<svg xmlns='http://www.w3.org/2000/svg'><path d='M0 0 L10 10'/></svg>"

    async def plan_goal(self, goal, history, initial_tool_call):
        return {
            "mode": "plan",
            "summary": "Generate SVG and save it",
            "reasoning": "The first step produces SVG markup that the second step writes to disk.",
            "steps": [
                {
                    "id": "step_1",
                    "description": "Create SVG markup",
                    "success_criteria": "SVG text is available for later steps",
                    "preferred_tools": [],
                },
                {
                    "id": "step_2",
                    "description": "Write the SVG file",
                    "success_criteria": "The SVG file exists on disk",
                    "preferred_tools": ["write_file"],
                },
            ],
        }, "fake", "model"

    async def next_action(self, state, step):
        if not state.get("tool_evidence"):
            return {
                "action": "tool_call",
                "tool_name": "write_file",
                "arguments": {
                    "path": str(svg_path),
                    "content": state["step_outputs"]["step_1"]["content"],
                },
            }, "fake", "model"
        return {
            "action": "finish",
            "scope": "task",
            "summary": f"Saved the SVG to {svg_path}",
        }, "fake", "model"

    async def verify(self, state, draft_response):
        return {"ok": True, "final_response": draft_response, "reason": "grounded"}, "fake", "model"

    async def generate_step_output(self, state, step):
        assert step["id"] == "step_1"
        return svg_text, "fake", "model"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", plan_goal)
    monkeypatch.setattr(native.NativeAgentRunner, "_next_action", next_action)
    monkeypatch.setattr(native.NativeAgentRunner, "_generate_step_output", generate_step_output)
    monkeypatch.setattr(native.NativeAgentRunner, "_verify_response", verify)

    pending = asyncio.run(runner.run(goal=task["goal"], task_id=task["id"]))
    assert pending.status == "waiting_approval"
    assert pending.state["step_outputs"]["step_1"]["content"] == svg_text

    approvals = state_store.list_pending_approvals()
    assert len(approvals) == 1
    approved = state_store.resolve_approval(approvals[0]["id"], approved=True)
    resumed = asyncio.run(
        runner.run(
            goal=task["goal"],
            task_id=task["id"],
            resume_state=approved["resume"]["state"],
            approved=True,
        )
    )
    assert resumed.status == "completed"
    assert svg_path.read_text(encoding="utf-8") == svg_text
    assert resumed.state["step_outputs"]["step_1"]["content"] == svg_text
    assert str(svg_path) in resumed.message


def test_store_result_does_not_complete_a_step_that_still_requires_a_tool(tmp_path, monkeypatch):
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
    task = state_store.create_task(goal="generate and save an SVG", kind="task")
    svg_path = tmp_path / "Desktop" / "hand.svg"
    svg_text = "<svg xmlns='http://www.w3.org/2000/svg'><path d='M0 0 L10 10'/></svg>"

    async def plan_goal(self, goal, history, initial_tool_call):
        return {
            "mode": "plan",
            "summary": "Generate and save the SVG",
            "reasoning": "The model may first produce SVG text before calling write_file.",
            "steps": [
                {
                    "id": "step_1",
                    "description": "Generate the SVG and save it with write_file",
                    "success_criteria": "The SVG file exists on disk",
                    "preferred_tools": ["write_file"],
                }
            ],
        }, "fake", "model"

    async def next_action(self, state, step):
        if not state.get("step_outputs"):
            return {
                "action": "store_result",
                "summary": "Generated SVG markup.",
                "result": svg_text,
            }, "fake", "model"
        if not state.get("tool_evidence"):
            return {
                "action": "tool_call",
                "tool_name": "write_file",
                "arguments": {
                    "path": str(svg_path),
                    "content": state["step_outputs"]["step_1"]["content"],
                },
            }, "fake", "model"
        return {
            "action": "finish",
            "scope": "task",
            "summary": f"Saved the SVG to {svg_path}",
        }, "fake", "model"

    async def verify(self, state, draft_response):
        return {"ok": True, "final_response": draft_response, "reason": "grounded"}, "fake", "model"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", plan_goal)
    monkeypatch.setattr(native.NativeAgentRunner, "_next_action", next_action)
    monkeypatch.setattr(native.NativeAgentRunner, "_verify_response", verify)

    pending = asyncio.run(runner.run(goal=task["goal"], task_id=task["id"]))
    assert pending.status == "waiting_approval"
    assert pending.state["step_outputs"]["step_1"]["content"] == svg_text
    approvals = state_store.list_pending_approvals()
    assert len(approvals) == 1

    approved = state_store.resolve_approval(approvals[0]["id"], approved=True)
    resumed = asyncio.run(
        runner.run(
            goal=task["goal"],
            task_id=task["id"],
            resume_state=approved["resume"]["state"],
            approved=True,
        )
    )
    assert resumed.status == "completed"
    assert svg_path.read_text(encoding="utf-8") == svg_text


def test_toolless_step_falls_back_to_direct_content_generation(tmp_path, monkeypatch):
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
    task = state_store.create_task(goal="generate an SVG and save it", kind="task")
    svg_path = tmp_path / "Desktop" / "hand.svg"
    svg_text = "<svg xmlns='http://www.w3.org/2000/svg'><path d='M0 0 L10 10'/></svg>"

    async def plan_goal(self, goal, history, initial_tool_call):
        return {
            "mode": "plan",
            "summary": "Generate SVG and save it",
            "reasoning": "The first step is content generation; the second writes the file.",
            "steps": [
                {
                    "id": "step_1",
                    "description": "Create SVG markup",
                    "success_criteria": "SVG text is available for later steps",
                    "preferred_tools": [],
                },
                {
                    "id": "step_2",
                    "description": "Write the SVG file",
                    "success_criteria": "The SVG file exists on disk",
                    "preferred_tools": ["write_file"],
                },
            ],
        }, "fake", "model"

    async def next_action(self, state, step):
        if step["id"] == "step_1":
            return {"action": ""}, "fake", "model"
        if not state.get("tool_evidence"):
            return {
                "action": "tool_call",
                "tool_name": "write_file",
                "arguments": {
                    "path": str(svg_path),
                    "content": state["step_outputs"]["step_1"]["content"],
                },
            }, "fake", "model"
        return {
            "action": "finish",
            "scope": "task",
            "summary": f"Saved the SVG to {svg_path}",
        }, "fake", "model"

    async def generate_step_output(self, state, step):
        assert step["id"] == "step_1"
        return svg_text, "fake", "model"

    async def verify(self, state, draft_response):
        return {"ok": True, "final_response": draft_response, "reason": "grounded"}, "fake", "model"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", plan_goal)
    monkeypatch.setattr(native.NativeAgentRunner, "_next_action", next_action)
    monkeypatch.setattr(native.NativeAgentRunner, "_generate_step_output", generate_step_output)
    monkeypatch.setattr(native.NativeAgentRunner, "_verify_response", verify)

    pending = asyncio.run(runner.run(goal=task["goal"], task_id=task["id"]))
    assert pending.status == "waiting_approval"
    assert pending.state["step_outputs"]["step_1"]["content"] == svg_text


def test_stalled_write_file_step_falls_back_to_generated_content_and_approval(tmp_path, monkeypatch):
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
    task = state_store.create_task(goal="generate an SVG of a hand and save it to my desktop", kind="task")
    svg_text = "<svg xmlns='http://www.w3.org/2000/svg'><path d='M0 0 L10 10'/></svg>"

    async def plan_goal(self, goal, history, initial_tool_call):
        return {
            "mode": "plan",
            "summary": "Generate and save the SVG",
            "reasoning": "This can be completed with write_file once SVG content exists.",
            "steps": [
                {
                    "id": "step_1",
                    "description": "Create an SVG file named hand.svg on the desktop containing a simple hand illustration.",
                    "success_criteria": "File exists at ~/Desktop/hand.svg and contains valid SVG markup.",
                    "preferred_tools": ["write_file"],
                }
            ],
        }, "fake", "model"

    async def next_action(self, state, step):
        if not state.get("tool_evidence"):
            return {"action": ""}, "fake", "model"
        return {
            "action": "finish",
            "scope": "task",
            "summary": "Saved the SVG to the desktop.",
        }, "fake", "model"

    async def generate_step_output(self, state, step):
        return svg_text, "fake", "model"

    async def verify(self, state, draft_response):
        return {"ok": True, "final_response": draft_response, "reason": "grounded"}, "fake", "model"

    def infer_write_target_path(self, goal, step, content):
        return tmp_path / "Desktop" / "hand.svg"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", plan_goal)
    monkeypatch.setattr(native.NativeAgentRunner, "_next_action", next_action)
    monkeypatch.setattr(native.NativeAgentRunner, "_generate_step_output", generate_step_output)
    monkeypatch.setattr(native.NativeAgentRunner, "_infer_write_target_path", infer_write_target_path)
    monkeypatch.setattr(native.NativeAgentRunner, "_verify_response", verify)

    pending = asyncio.run(runner.run(goal=task["goal"], task_id=task["id"]))
    assert pending.status == "waiting_approval"
    approval = pending.approval or {}
    assert approval["operation"] == "file_write"
    assert approval["summary"].endswith("/Desktop/hand.svg")
    approvals = state_store.list_pending_approvals()
    approved = state_store.resolve_approval(approvals[0]["id"], approved=True)

    resumed = asyncio.run(
        runner.run(
            goal=task["goal"],
            task_id=task["id"],
            resume_state=approved["resume"]["state"],
            approved=True,
        )
    )
    assert resumed.status == "completed"
    assert str(tmp_path / "Desktop" / "hand.svg") in resumed.message


def test_native_agent_runner_scaffolds_gmail_tool_after_approval(tmp_path, monkeypatch):
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
    task = state_store.create_task(goal="create a Gmail tool", kind="task")

    async def plan_goal(self, goal, history, initial_tool_call):
        return {
            "mode": "plan",
            "summary": "Close the Gmail capability gap",
            "reasoning": "No Gmail tool exists yet, so scaffold one locally.",
            "steps": [
                {
                    "id": "step_1",
                    "description": "Create a reusable Gmail custom tool",
                    "success_criteria": "A custom Gmail tool scaffold exists",
                    "preferred_tools": ["custom_tool_create"],
                }
            ],
        }, "fake", "model"

    async def next_action(self, state, step):
        return {
            "action": "capability_gap",
            "strategy": "custom_tool",
            "reason": "Scaffold a Gmail tool with local setup notes.",
            "name": "gmail_tool",
            "description": "Use Gmail once OAuth credentials are configured.",
        }, "fake", "model"

    async def verify(self, state, draft_response):
        return {"ok": True, "final_response": draft_response, "reason": "grounded"}, "fake", "model"

    monkeypatch.setattr(native.NativeAgentRunner, "_plan_goal", plan_goal)
    monkeypatch.setattr(native.NativeAgentRunner, "_next_action", next_action)
    monkeypatch.setattr(native.NativeAgentRunner, "_verify_response", verify)

    pending = asyncio.run(runner.run(goal=task["goal"], task_id=task["id"]))
    assert pending.status == "waiting_approval"
    approvals = state_store.list_pending_approvals()
    assert len(approvals) == 1
    assert approvals[0]["operation"] == "custom_tool_create"

    approved = state_store.resolve_approval(approvals[0]["id"], approved=True)
    resumed = asyncio.run(
        runner.run(
            goal=task["goal"],
            task_id=task["id"],
            resume_state=approved["resume"]["state"],
            approved=True,
        )
    )
    tool_dir = paths.tools_dir / "gmail_tool"
    assert resumed.status == "completed"
    assert (tool_dir / "tool.json").exists()
    assert (tool_dir / "tool.py").exists()
    assert (tool_dir / "SETUP.md").exists()
    assert runner.tool_registry.get("gmail_tool") is not None
    assert "credentials" in resumed.message.lower()
    assert "oauth" in resumed.message.lower()


def test_take_screenshot_failure_reports_real_error(monkeypatch):
    def fail_capture(path: Path) -> None:
        raise RuntimeError("screen recording permission denied")

    monkeypatch.setattr(native_chat_tools_impl, "_capture_screenshot_to_file", fail_capture)
    result = native._execute_tool("take_screenshot", {})
    lowered = result.lower()
    assert "permission denied" in lowered or "screenshot capture failed" in lowered
    assert "captured successfully" not in lowered
    assert "simulated" not in lowered


def test_native_agent_runner_uses_svg_render_heuristic_for_svg_to_png_goals(tmp_path, monkeypatch):
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

    async def generate_step_output(self, state, step):
        assert step["id"] == "step_1"
        return "<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32'><path d='M8 24 L8 10 L12 4 L16 10 L16 24 Z'/></svg>", "fake", "model"

    async def verify(self, state, draft_response):
        return {"ok": True, "final_response": draft_response, "reason": "grounded"}, "fake", "model"

    def fake_render(_self, _svg_path: Path, png_path: Path) -> None:
        png_path.write_bytes(b"fake-png")

    monkeypatch.setattr(native.NativeAgentRunner, "_generate_step_output", generate_step_output)
    monkeypatch.setattr(native.NativeAgentRunner, "_verify_response", verify)
    monkeypatch.setattr(native.NativeToolRegistry, "_render_svg_to_png", fake_render)

    goal = "Create an SVG of a hand then render it as a png and send it to me in telegram"
    outcome = asyncio.run(runner.run(goal=goal))

    assert outcome.status == "completed"
    assert outcome.plan is not None
    assert outcome.plan["summary"] == "Generate SVG markup and render it to a PNG artifact."
    artifact_paths = [artifact["path"] for artifact in outcome.artifacts]
    assert any(path.endswith(".svg") for path in artifact_paths)
    assert any(path.endswith(".png") for path in artifact_paths)
    assert "Done" in outcome.message or "Rendered" in outcome.message


def test_render_svg_asset_can_send_png_to_telegram(tmp_path, monkeypatch):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    registry = native.NativeToolRegistry(
        paths=paths,
        config=native.DEFAULT_CONFIG,
        runtime_policy=native.NativeRuntimePolicy(native.DEFAULT_CONFIG),
        workspace_root=tmp_path,
    )

    def fake_render(_svg_path: Path, png_path: Path) -> None:
        png_path.write_bytes(b"fake-png")

    monkeypatch.setattr(native.NativeToolRegistry, "_render_svg_to_png", staticmethod(fake_render))
    monkeypatch.setattr(
        native_chat_tools_impl,
        "_send_file_to_telegram",
        lambda path, caption="": (True, f"Sent {path.name}"),
    )

    result = registry.execute(
        "render_svg_asset",
        {
            "svg_content": "<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32'></svg>",
            "prompt": "hand",
            "send_to_telegram": True,
            "caption": "hand render",
        },
    )

    assert result.success is True
    assert result.data["sent_to_telegram"] is True
    assert any(artifact["path"].endswith(".png") for artifact in result.artifacts)


def test_load_native_config_uses_fallback_yaml_parser(monkeypatch, tmp_path):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    paths.config_yml.write_text(
        """
agent:
  max_plan_steps: 3
tools:
  enabled_categories:
    - file
    - web
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(native, "yaml", None)

    config = native.load_native_config(paths)
    assert config["agent"]["max_plan_steps"] == 3
    assert config["tools"]["enabled_categories"] == ["file", "web"]


@pytest.mark.parametrize("provider", ["ollama", "lmstudio"])
def test_native_agent_runner_uses_same_json_loop_for_local_providers(tmp_path, monkeypatch, provider):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    runner = native.NativeAgentRunner(
        paths=paths,
        config=native.DEFAULT_CONFIG,
        runtime_policy=native.NativeRuntimePolicy(native.DEFAULT_CONFIG),
        workspace_root=tmp_path,
    )
    requests: list[str] = []

    async def fake_detect(_config):
        return {
            "default_provider": provider,
            "default_model": "local-model",
            "providers": {
                "ollama": {"base_url": "http://ollama.local"},
                "lmstudio": {"base_url": "http://lmstudio.local"},
            },
        }

    async def fake_post(url, payload, timeout_seconds=60):
        requests.append(url)
        if provider == "ollama":
            return {
                "message": {
                    "content": native.json.dumps({"mode": "direct_response", "response": f"{provider} ok"})
                }
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": native.json.dumps({"mode": "direct_response", "response": f"{provider} ok"})
                    }
                }
            ]
        }

    monkeypatch.setattr(native_models_impl, "detect_local_model_runtime", fake_detect)
    monkeypatch.setattr(native_models_impl, "_http_post_json", fake_post)

    outcome = asyncio.run(runner.run(goal="say hello"))
    assert outcome.status == "completed"
    assert outcome.message == f"{provider} ok"
    assert outcome.provider == provider
    assert requests
    if provider == "ollama":
        assert requests[0].endswith("/api/chat")
    else:
        assert requests[0].endswith("/v1/chat/completions")


def test_generate_local_text_response_can_disable_lmstudio_thinking(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_detect(_config):
        return {
            "default_provider": "lmstudio",
            "default_model": "ministral-3-14b-instruct-2512",
            "reasoning_provider": "lmstudio",
            "reasoning_model": "qwen3.5-9b",
            "reasoning_escalation": True,
            "reasoning_auto_restore_primary": True,
            "providers": {
                "lmstudio": {
                    "base_url": "http://lmstudio.local",
                }
            },
        }

    async def fake_post(url, payload, timeout_seconds=60):
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout_seconds"] = timeout_seconds
        return {
            "choices": [
                {
                    "message": {
                        "content": "ok",
                    }
                }
            ]
        }

    monkeypatch.setattr(native_models_impl, "detect_local_model_runtime", fake_detect)
    monkeypatch.setattr(native_models_impl, "_http_post_json", fake_post)

    result = asyncio.run(
        native_models_impl._generate_local_text_response(
            messages=[{"role": "user", "content": "hello"}],
            config=native.DEFAULT_CONFIG,
            enable_thinking=False,
            timeout_seconds=45,
        )
    )

    assert result["content"] == "ok"
    assert captured["url"] == "http://lmstudio.local/v1/chat/completions"
    assert captured["timeout_seconds"] == 45
    assert captured["payload"]["model"] == "ministral-3-14b-instruct-2512"
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_detect_local_model_runtime_tracks_lmstudio_reasoning_catalog(monkeypatch):
    async def fake_get(url, timeout_seconds=2.5):
        if url.endswith("/api/tags"):
            raise RuntimeError("Ollama offline")
        if url.endswith("/api/v1/models"):
            return {
                "models": [
                    {
                        "type": "llm",
                        "key": "ministral-3-14b-instruct-2512",
                        "loaded_instances": [{"id": "ministral-3-14b-instruct-2512"}],
                    },
                    {
                        "type": "llm",
                        "key": "qwen3.5-9b",
                        "loaded_instances": [],
                    },
                ]
            }
        if url.endswith("/v1/models"):
            return {
                "data": [
                    {"id": "ministral-3-14b-instruct-2512"},
                ]
            }
        raise AssertionError(url)

    monkeypatch.setattr(native_models_impl, "_http_get_json", fake_get)

    runtime = asyncio.run(
        native_models_impl.detect_local_model_runtime(
            {
                "models": {
                    "preferred_provider": "lmstudio",
                    "preferred_model": "ministral-3-14b-instruct-2512",
                    "reasoning_provider": "lmstudio",
                    "reasoning_model": "qwen3.5-9b",
                    "reasoning_escalation": True,
                    "lmstudio_url": "http://lmstudio.local",
                }
            }
        )
    )

    assert runtime["default_provider"] == "lmstudio"
    assert runtime["default_model"] == "ministral-3-14b-instruct-2512"
    assert runtime["reasoning_model"] == "qwen3.5-9b"
    assert runtime["reasoning_escalation"] is True
    assert runtime["providers"]["lmstudio"]["models"] == ["ministral-3-14b-instruct-2512"]
    assert runtime["providers"]["lmstudio"]["available_models"] == [
        "ministral-3-14b-instruct-2512",
        "qwen3.5-9b",
    ]
    assert runtime["providers"]["lmstudio"]["supports_model_swap"] is True


def test_generate_local_text_response_uses_reasoning_model_and_restores_primary(monkeypatch):
    captured: dict[str, object] = {}
    loaded_models: list[str] = []

    async def fake_detect(_config):
        return {
            "default_provider": "lmstudio",
            "default_model": "ministral-3-14b-instruct-2512",
            "reasoning_provider": "lmstudio",
            "reasoning_model": "qwen3.5-9b",
            "reasoning_escalation": True,
            "reasoning_auto_restore_primary": True,
            "providers": {
                "lmstudio": {
                    "base_url": "http://lmstudio.local",
                    "supports_model_swap": True,
                    "available_models": [
                        "ministral-3-14b-instruct-2512",
                        "qwen3.5-9b",
                    ],
                }
            },
        }

    async def fake_ensure(*, base_url, target_model, available_models=None):
        assert base_url == "http://lmstudio.local"
        loaded_models.append(target_model)
        return {"previous_active_models": ["ministral-3-14b-instruct-2512"], "swapped": True}

    async def fake_post(url, payload, timeout_seconds=60):
        captured["url"] = url
        captured["payload"] = payload
        return {
            "choices": [
                {
                    "message": {
                        "reasoning_content": "deep answer",
                    }
                }
            ]
        }

    monkeypatch.setattr(native_models_impl, "detect_local_model_runtime", fake_detect)
    monkeypatch.setattr(native_models_impl, "_ensure_lmstudio_model_loaded", fake_ensure)
    monkeypatch.setattr(native_models_impl, "_http_post_json", fake_post)

    result = asyncio.run(
        native_models_impl._generate_local_text_response(
            messages=[{"role": "user", "content": "Think step by step about the architecture tradeoffs here."}],
            config=native.DEFAULT_CONFIG,
        )
    )

    assert result["model"] == "qwen3.5-9b"
    assert result["content"] == "deep answer"
    assert captured["url"] == "http://lmstudio.local/v1/chat/completions"
    assert captured["payload"]["model"] == "qwen3.5-9b"
    assert loaded_models == ["qwen3.5-9b", "ministral-3-14b-instruct-2512"]


def test_generate_step_output_retries_without_thinking_after_failure(tmp_path, monkeypatch):
    paths = native.ensure_home_layout(str(tmp_path / ".kestrel"))
    runner = native.NativeAgentRunner(
        paths=paths,
        config=native.DEFAULT_CONFIG,
        runtime_policy=native.NativeRuntimePolicy(native.DEFAULT_CONFIG),
        workspace_root=tmp_path,
    )
    calls: list[dict[str, object]] = []

    async def fake_generate_local_text_response(
        *,
        messages,
        config,
        temperature=0.2,
        max_tokens=4096,
        enable_thinking=None,
        timeout_seconds=120,
    ):
        calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "enable_thinking": enable_thinking,
                "timeout_seconds": timeout_seconds,
            }
        )
        if len(calls) == 1:
            raise RuntimeError("LM Studio timed out")
        return {
            "provider": "lmstudio",
            "model": "qwen3-30b-a3b-instruct-2507",
            "content": "<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32' viewBox='0 0 32 32'></svg>",
        }

    monkeypatch.setattr(native_agent_base_impl, "_generate_local_text_response", fake_generate_local_text_response)

    content, provider, model = asyncio.run(
        runner._generate_step_output(
            {
                "goal": "Create an SVG of a hand then render it as a png and send it to me in telegram",
                "plan": {"summary": "Generate SVG markup and render it to a PNG artifact."},
                "step_outputs": {},
                "tool_evidence": [],
            },
            {
                "id": "step_1",
                "description": "Generate the requested SVG markup.",
                "success_criteria": "Valid SVG markup exists for the requested subject.",
                "preferred_tools": [],
            },
        )
    )

    assert content.startswith("<svg")
    assert provider == "lmstudio"
    assert model == "qwen3-30b-a3b-instruct-2507"
    assert len(calls) == 2
    assert calls[0]["enable_thinking"] is True
    assert calls[1]["enable_thinking"] is False
    assert calls[0]["timeout_seconds"] == 90
    assert calls[1]["timeout_seconds"] == 45
    assert "Return only valid standalone SVG markup." in calls[0]["messages"][0]["content"]
