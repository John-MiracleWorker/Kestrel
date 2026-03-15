import asyncio
import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kestrel as kestrel_cli_entry
from kestrel_cli import cli_core as cli_core_impl
from kestrel_cli import cli_memory as cli_memory_impl


def test_cli_parser_supports_skill_install():
    parser = kestrel_cli_entry.build_parser()

    args = parser.parse_args(
        [
            "skill",
            "install",
            "marketplace-demo-orchestrator",
            "--scope",
            "workspace",
        ]
    )

    assert args.command == "skill"
    assert args.skill_cmd == "install"
    assert args.pack_id == "marketplace-demo-orchestrator"
    assert args.scope == "workspace"


def test_kestrel_client_skill_search_uses_control_request(monkeypatch, tmp_path):
    monkeypatch.setenv("KESTREL_HOME", str(tmp_path / ".kestrel"))
    seen: dict[str, object] = {}

    async def fake_send_control_request(method, params=None, *, paths=None, timeout_seconds=30):
        seen["method"] = method
        seen["params"] = params
        return {"query": params["query"], "results": [{"pack_id": "demo-pack"}], "total": 1}

    monkeypatch.setattr(cli_core_impl, "control_socket_available", lambda _paths: True)
    monkeypatch.setattr(cli_core_impl, "send_control_request", fake_send_control_request)

    client = cli_core_impl.KestrelClient(cli_core_impl.DEFAULT_CONFIG)
    result = asyncio.run(client.skill_search("demo pack", include_marketplace=True))

    assert seen["method"] == "skill.search"
    assert seen["params"] == {"query": "demo pack", "include_marketplace": True}
    assert result["results"][0]["pack_id"] == "demo-pack"


def test_kestrel_client_chat_passes_history_to_control_request(monkeypatch, tmp_path):
    monkeypatch.setenv("KESTREL_HOME", str(tmp_path / ".kestrel"))
    seen: dict[str, object] = {}

    async def fake_send_control_request(method, params=None, *, paths=None, timeout_seconds=30):
        seen["method"] = method
        seen["params"] = params
        return {"message": "Refined search", "status": "completed"}

    monkeypatch.setattr(cli_core_impl, "control_socket_available", lambda _paths: True)
    monkeypatch.setattr(cli_core_impl, "send_control_request", fake_send_control_request)

    client = cli_core_impl.KestrelClient(cli_core_impl.DEFAULT_CONFIG)
    history = [
        {"role": "user", "content": "Can you look for a skill that can help me manage my financial life?"},
        {"role": "assistant", "content": "Do you want me to download that skill or search for something else?"},
    ]
    result = asyncio.run(client.chat("Budget tracker", history=history))

    assert seen["method"] == "chat"
    assert seen["params"] == {"prompt": "Budget tracker", "history": history}
    assert result["message"] == "Refined search"


def test_interactive_repl_does_not_require_brain_package(monkeypatch):
    monkeypatch.setattr(cli_memory_impl, "print_logo", lambda: None)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": (_ for _ in ()).throw(EOFError()))

    client = cli_core_impl.KestrelClient(cli_core_impl.DEFAULT_CONFIG)

    asyncio.run(cli_memory_impl.interactive_repl(client, cli_core_impl.DEFAULT_CONFIG))
