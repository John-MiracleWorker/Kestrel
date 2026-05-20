from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import platform
import subprocess  # nosec B404
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import metadata as importlib_metadata
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from .agent import NestedMV2Agent
from .app_factory import build_agent
from .behavior_delta_extractor import BehaviorDeltaExtractor
from .behavior_delta_ledger import BehaviorDeltaLedger
from .channels import ChannelManager, ChannelPayloadError
from .config import AgentConfig
from .context_compiler import ContextCompiler
from .context_packer import ContextPacker, ContextPackRequest
from .event_bus import RunEventBus
from .layers import load_layer_specs
from .llm.model_catalog import PROVIDER_OPTIONS
from .mcp_manager import MCPManager
from .models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from .orchestrator import build_memory_system
from .plugin_manager import PluginError, PluginManager
from .promotion_ledger import OUTCOME_KINDS, PromotionLedger
from .run_manager import RunManager
from .runtime_models import LLMStreamEvent, ToolCall
from .skill_manager import SkillManager
from .state_store import AgentStateStore
from .task_capsule import summarize_run_capsule
from .tools.base import ToolContext
from .tools.builtin import build_default_tools


def _add_common_args(parser: argparse.ArgumentParser, *, default: object = argparse.SUPPRESS) -> None:
    parser.add_argument("--backend", choices=["memory", "memvid"], default=default)
    parser.add_argument("--memory-dir", type=Path, default=default)
    parser.add_argument("--layer-config", type=Path, default=default)


def _add_agent_args(parser: argparse.ArgumentParser) -> None:
    _add_common_args(parser)
    parser.add_argument(
        "--provider",
        choices=list(PROVIDER_OPTIONS),
        default=argparse.SUPPRESS,
    )
    parser.add_argument("--model", default=argparse.SUPPRESS)
    parser.add_argument("--base-url", default=argparse.SUPPRESS)
    parser.add_argument("--api-key-env", default=argparse.SUPPRESS)
    parser.add_argument("--timeout-seconds", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--max-retries", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--temperature", type=float, default=argparse.SUPPRESS)
    parser.add_argument(
        "--codex-sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default=argparse.SUPPRESS,
    )
    parser.add_argument("--codex-profile", default=argparse.SUPPRESS)
    parser.add_argument("--codex-skip-git-repo-check", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--codex-persist-session", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--workspace", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--log-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--state-path", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--secret-store-path", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--secret-backend", choices=["json", "keyring"], default=argparse.SUPPRESS)
    parser.add_argument("--skills-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--plugins-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--mcp-config", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--channels-config", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--enable-channel-delivery", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--channel-send-timeout-seconds", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--require-api-auth", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--api-auth-token-env", default=argparse.SUPPRESS)
    parser.add_argument("--allow-shell", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-file-write", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-policy-writes", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-codex-cli", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-plugin-install", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-git-commit", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-git-push", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-remote-mutation", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--git-write-mode", default=argparse.SUPPRESS)
    parser.add_argument("--protected-branches", default=argparse.SUPPRESS)
    parser.add_argument("--allow-memory-import", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-executable-skills", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-mcp-network-endpoints", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-web", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--allow-self-modification", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--web-backend", choices=["direct", "mock"], default=argparse.SUPPRESS)
    parser.add_argument("--web-timeout-seconds", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--web-max-results", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--web-max-bytes", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--enable-autonomous-scheduler", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--max-scheduler-tasks", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--max-scheduler-cycles", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--enable-worker-isolation", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--worker-worktree-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--worker-branch-prefix", default=argparse.SUPPRESS)
    parser.add_argument("--stream", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--max-tool-rounds", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--context-budget-chars", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--disable-task-capsules", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--enable-auto-consolidation", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--auto-consolidation-write", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--enable-auto-compact", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--auto-compact-apply", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--context-pack-token-budget", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--context-pack-expand-raw", action="store_true", default=argparse.SUPPRESS)


def main() -> None:
    parser = argparse.ArgumentParser(prog="nested-memvid")
    _add_common_args(parser, default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init")
    _add_common_args(init)

    put = sub.add_parser("put")
    _add_common_args(put)
    put.add_argument("--layer", choices=[layer.value for layer in MemoryLayer], required=True)
    put.add_argument("--kind", choices=[kind.value for kind in MemoryKind], default=MemoryKind.OBSERVATION.value)
    put.add_argument("--title", required=True)
    put.add_argument("--text", required=True)
    put.add_argument("--confidence", type=float, default=0.8)
    put.add_argument("--importance", type=float, default=0.5)

    search = sub.add_parser("search")
    _add_common_args(search)
    search.add_argument("--query", required=True)
    search.add_argument("--k", type=int, default=8)

    memory_cmd = sub.add_parser("memory")
    memory_sub = memory_cmd.add_subparsers(dest="memory_cmd", required=True)
    memory_search = memory_sub.add_parser("search")
    _add_common_args(memory_search)
    memory_search.add_argument("query")
    memory_search.add_argument("--k", type=int, default=8)
    memory_verify = memory_sub.add_parser("verify")
    _add_common_args(memory_verify)
    memory_doctor = memory_sub.add_parser("doctor")
    _add_common_args(memory_doctor)
    memory_doctor.add_argument("--repair", action="store_true", help="Allow backend-supported repair instead of dry-run.")
    memory_inspect = memory_sub.add_parser("inspect")
    _add_common_args(memory_inspect)
    memory_inspect.add_argument("query")
    memory_inspect.add_argument("--k", type=int, default=8)
    memory_inspect.add_argument("--include-inactive", action="store_true")
    memory_consolidate = memory_sub.add_parser("consolidate")
    _add_common_args(memory_consolidate)
    memory_consolidate.add_argument("query")
    memory_consolidate.add_argument("--source-layer", choices=[layer.value for layer in MemoryLayer])
    memory_consolidate.add_argument("--validation-score", type=float, default=0.7)
    memory_consolidate.add_argument("--repeat-count", type=int, default=1)
    memory_consolidate.add_argument("--explicit-instruction", action="store_true")
    memory_consolidate.add_argument("--dry-run", action="store_true")
    memory_correct = memory_sub.add_parser("correct")
    _add_common_args(memory_correct)
    memory_correct.add_argument("target_record_id")
    memory_correct.add_argument("correction_text")
    memory_correct.add_argument("--evidence-source", default="cli")
    memory_correct.add_argument("--evidence-locator", default="memory.correct")
    memory_correct.add_argument("--dry-run", action="store_true")
    memory_correct.add_argument("--allow-memory-import", action="store_true", default=argparse.SUPPRESS)
    memory_compact = memory_sub.add_parser("compact")
    _add_common_args(memory_compact)
    memory_compact.add_argument("--layer", choices=[layer.value for layer in MemoryLayer], default=MemoryLayer.WORKING.value)
    memory_compact.add_argument("--apply", action="store_true")
    memory_ledger = memory_sub.add_parser("ledger")
    memory_ledger.add_argument("--state-path", type=Path, default=Path(".nest/state/agent.db"))
    memory_ledger.add_argument("--since", default="30d")
    memory_ledger.add_argument("--layer", choices=[layer.value for layer in MemoryLayer])
    memory_ledger.add_argument("--outcome", choices=list(OUTCOME_KINDS))
    memory_ledger.add_argument("--json", action="store_true")
    memory_deltas = memory_sub.add_parser("deltas")
    deltas_sub = memory_deltas.add_subparsers(dest="deltas_cmd", required=True)
    deltas_propose = deltas_sub.add_parser("propose")
    deltas_propose.add_argument("--run-id", required=True)
    deltas_propose.add_argument("--runs-dir", type=Path, default=Path(".nest/runs"))
    deltas_propose.add_argument("--state-path", type=Path, default=Path(".nest/state/agent.db"))
    deltas_propose.add_argument("--backend", choices=["memory", "memvid"], default="memory")
    deltas_propose.add_argument("--dry-run", action="store_true")
    deltas_propose.add_argument("--json", action="store_true")

    compile_cmd = sub.add_parser("compile-context")
    _add_common_args(compile_cmd)
    compile_cmd.add_argument("--objective", required=True)
    compile_cmd.add_argument("--query")

    context_cmd = sub.add_parser("context")
    _add_common_args(context_cmd)
    context_cmd.add_argument("query")

    tools_cmd = sub.add_parser("tools")
    tools_cmd.add_argument("--json", action="store_true")

    plugins_cmd = sub.add_parser("plugins")
    plugins_sub = plugins_cmd.add_subparsers(dest="plugins_cmd", required=True)
    plugins_list = plugins_sub.add_parser("list")
    _add_agent_args(plugins_list)
    plugins_list.add_argument("--json", action="store_true")
    plugins_review = plugins_sub.add_parser("review")
    _add_agent_args(plugins_review)
    plugins_review.add_argument("source")
    plugins_review.add_argument("--ref")
    plugins_review.add_argument("--json", action="store_true")
    plugins_install = plugins_sub.add_parser("install")
    _add_agent_args(plugins_install)
    plugins_install.add_argument("source")
    plugins_install.add_argument("--ref")
    plugins_install.add_argument("--enable", action="store_true")
    plugins_install.add_argument("--overwrite", action="store_true")
    plugins_install.add_argument("--json", action="store_true")
    plugins_inspect = plugins_sub.add_parser("inspect")
    _add_agent_args(plugins_inspect)
    plugins_inspect.add_argument("plugin_id")
    plugins_inspect.add_argument("--json", action="store_true")
    plugins_enable = plugins_sub.add_parser("enable")
    _add_agent_args(plugins_enable)
    plugins_enable.add_argument("plugin_id")
    plugins_enable.add_argument("--json", action="store_true")
    plugins_disable = plugins_sub.add_parser("disable")
    _add_agent_args(plugins_disable)
    plugins_disable.add_argument("plugin_id")
    plugins_disable.add_argument("--json", action="store_true")
    plugins_update = plugins_sub.add_parser("update")
    _add_agent_args(plugins_update)
    plugins_update.add_argument("plugin_id")
    plugins_update.add_argument("--ref")
    plugins_update.add_argument("--json", action="store_true")
    plugins_remove = plugins_sub.add_parser("remove")
    _add_agent_args(plugins_remove)
    plugins_remove.add_argument("plugin_id")
    plugins_remove.add_argument("--json", action="store_true")

    chat = sub.add_parser("chat")
    _add_agent_args(chat)
    chat.add_argument("--message", help="Run one chat turn. If omitted, enter interactive mode.")
    chat.add_argument("--session-id", default="cli")

    run_cmd = sub.add_parser("run")
    _add_agent_args(run_cmd)
    run_cmd.add_argument("message")
    run_cmd.add_argument("--session-id", default="cli")
    run_cmd.add_argument("--json", action="store_true")
    run_cmd.add_argument("--no-wait", action="store_true")
    run_cmd.add_argument("--events", action="store_true")

    status_cmd = sub.add_parser("status")
    _add_agent_args(status_cmd)
    status_cmd.add_argument("run_id", nargs="?")
    status_cmd.add_argument("--json", action="store_true")
    status_cmd.add_argument("--events", action="store_true")

    approvals_cmd = sub.add_parser("approvals")
    _add_agent_args(approvals_cmd)
    approvals_cmd.add_argument("--status")
    approvals_cmd.add_argument("--json", action="store_true")

    approve_cmd = sub.add_parser("approve")
    _add_agent_args(approve_cmd)
    approve_cmd.add_argument("approval_id")
    approve_cmd.add_argument("--arguments", help="Optional JSON object replacing the originally requested arguments.")
    approve_cmd.add_argument("--json", action="store_true")
    approve_cmd.add_argument("--no-wait", action="store_true")

    deny_cmd = sub.add_parser("deny")
    _add_agent_args(deny_cmd)
    deny_cmd.add_argument("approval_id")
    deny_cmd.add_argument("--json", action="store_true")

    eval_cmd = sub.add_parser("eval")
    _add_common_args(eval_cmd)
    eval_cmd.add_argument(
        "--provider",
        choices=list(PROVIDER_OPTIONS),
        default=argparse.SUPPRESS,
    )
    eval_cmd.add_argument("--model", default=argparse.SUPPRESS)
    eval_cmd.add_argument("--workspace", type=Path, default=argparse.SUPPRESS)

    doctor = sub.add_parser("doctor")
    _add_agent_args(doctor)

    server = sub.add_parser("server")
    _add_agent_args(server)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)

    channel = sub.add_parser("channel")
    _add_agent_args(channel)
    channel.add_argument("channel_provider", help="Channel provider: telegram, discord, webhook, or custom.")
    channel.add_argument("--channel-id", help="Configured channel id. Defaults to provider.")
    channel.add_argument("--send", action="store_true", help="Request outbound delivery after the agent responds.")
    payload_group = channel.add_mutually_exclusive_group(required=True)
    payload_group.add_argument("--payload", help="Inbound channel payload as JSON.")
    payload_group.add_argument("--payload-file", type=Path, help="Path to inbound channel payload JSON, or '-' for stdin.")

    args = parser.parse_args()
    config = _agent_config_from_args(args)
    backend = config.backend
    memory_dir = config.memory_dir

    if args.cmd == "chat":
        manager = _build_run_manager(config)
        try:
            if args.message:
                if _handle_slash_command_for_manager(manager, config, args.message.strip(), args.session_id):
                    return
                _run_manager_chat_and_print(manager, args.message, session_id=args.session_id)
                return
            print("Nested MV2 Agent chat. Type /exit to quit.")
            while True:
                user_message = input("you> ").strip()
                if user_message in {"/exit", "/quit"}:
                    return
                if not user_message:
                    continue
                if _handle_slash_command_for_manager(manager, config, user_message, args.session_id):
                    continue
                _run_manager_chat_and_print(manager, user_message, session_id=args.session_id, prefix="agent> ")
        finally:
            manager.mcp.shutdown()
        return

    if args.cmd == "run":
        manager = _build_run_manager(config)
        try:
            _create_run_and_print(
                manager,
                args.message,
                session_id=args.session_id,
                json_output=args.json,
                wait=not args.no_wait,
                include_events=args.events,
            )
        finally:
            manager.mcp.shutdown()
        return

    if args.cmd == "status":
        manager = _build_run_manager(config)
        try:
            _print_status(manager, run_id=args.run_id, json_output=args.json, include_events=args.events)
        finally:
            manager.mcp.shutdown()
        return

    if args.cmd == "approvals":
        manager = _build_run_manager(config)
        try:
            _print_approvals(manager, status=args.status, json_output=args.json)
        finally:
            manager.mcp.shutdown()
        return

    if args.cmd in {"approve", "deny"}:
        manager = _build_run_manager(config)
        try:
            _decide_approval_and_print(
                manager,
                approval_id=args.approval_id,
                approved=args.cmd == "approve",
                arguments_json=getattr(args, "arguments", None),
                json_output=args.json,
                wait=not getattr(args, "no_wait", False),
            )
        finally:
            manager.mcp.shutdown()
        return

    if args.cmd == "server":
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError("Install server extras with `pip install -e '.[server]'`.") from exc
        from .server import create_app

        _validate_server_bind(args.host, config)
        uvicorn.run(create_app(config), host=args.host, port=args.port)
        return

    if args.cmd == "channel":
        channel_manager = ChannelManager(config)
        try:
            result = channel_manager.handle_payload(
                provider=args.channel_provider,
                channel_id=args.channel_id,
                payload=_load_channel_payload(args),
                send=args.send,
            )
        except ChannelPayloadError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result.to_public_dict(), indent=2))
        return

    if args.cmd == "tools":
        specs = [spec.to_public_dict() for spec in build_default_tools().specs()]
        if args.json:
            print(json.dumps(specs, indent=2))
        else:
            for spec in specs:
                approval = "approval required" if spec["requires_approval"] else "allowed"
                print(f"{spec['name']} [{spec['risk']}, {approval}] - {spec['description']}")
        return

    if args.cmd == "plugins":
        manager = _build_run_manager(config)
        try:
            _handle_plugins_command(args, manager, backend=backend, memory_dir=memory_dir)
        finally:
            manager.mcp.shutdown()
        return

    if args.cmd == "doctor":
        print(json.dumps(_doctor_runtime(config), indent=2))
        return

    if args.cmd == "eval":
        _run_eval_command(config)
        return

    if args.cmd == "memory" and args.memory_cmd == "ledger":
        _print_promotion_ledger(args)
        return

    if args.cmd == "memory" and args.memory_cmd == "deltas":
        _handle_behavior_deltas_command(args)
        return

    ledger = _ledger_for_memory_command(args)
    memory = build_memory_system(backend, memory_dir, specs=_specs_from_config(config), ledger=ledger)
    try:
        if args.cmd == "memory":
            if args.memory_cmd in {"search", "inspect"}:
                hits = memory.retrieve(
                    RetrievalQuery(
                        query=args.query,
                        k_per_layer=args.k,
                        include_inactive=bool(getattr(args, "include_inactive", False)),
                    )
                )
                for hit in hits:
                    memory_payload: dict[str, object] = {
                        "layer": hit.record.layer.value,
                        "kind": hit.record.kind.value,
                        "score": hit.score,
                        "title": hit.record.title,
                        "id": hit.record.id,
                        "snippet": hit.snippet or hit.record.content[:500],
                    }
                    if args.memory_cmd == "inspect":
                        memory_payload["metadata"] = hit.record.metadata
                        memory_payload["evidence"] = [
                            {"source": evidence.source, "locator": evidence.locator, "quote": evidence.quote}
                            for evidence in hit.record.evidence
                        ]
                    print(json.dumps(memory_payload, indent=2))
                if not hits:
                    print("No memory hits.")
                return
            if args.memory_cmd == "verify":
                _print_verify_results(memory.verify_all())
                return
            if args.memory_cmd == "doctor":
                print(json.dumps(_doctor_memory(memory, dry_run=not args.repair), indent=2))
                return
            if args.memory_cmd == "consolidate":
                execution = build_default_tools().execute(
                    ToolCall(
                        name="memory.consolidate",
                        arguments={
                            "query": args.query,
                            "source_layer": args.source_layer,
                            "validation_score": args.validation_score,
                            "repeat_count": args.repeat_count,
                            "explicit_instruction": args.explicit_instruction,
                            "dry_run": args.dry_run,
                        },
                    ),
                    ToolContext(
                        memory=memory,
                        config=config,
                        workspace=config.workspace,
                        session_id="cli",
                    ),
                )
                print(execution.content)
                if not execution.success:
                    raise SystemExit(1)
                return
            if args.memory_cmd == "correct":
                arguments = {
                    "target_record_id": args.target_record_id,
                    "correction_text": args.correction_text,
                    "evidence": [
                        {
                            "source": args.evidence_source,
                            "locator": args.evidence_locator,
                        }
                    ],
                    "dry_run": args.dry_run,
                }
                call = ToolCall(name="memory.correct", id="cli_memory_correct", arguments=arguments)
                execution = build_default_tools().execute(
                    call,
                    ToolContext(
                        memory=memory,
                        config=config,
                        workspace=config.workspace,
                        session_id="cli",
                        approved_tool_call_ids=frozenset({call.id}),
                        approved_tool_call_arguments={call.id: arguments},
                    ),
                )
                print(execution.content)
                if not execution.success:
                    raise SystemExit(1)
                return
            if args.memory_cmd == "compact":
                execution = build_default_tools().execute(
                    ToolCall(
                        name="memory.compact",
                        arguments={
                            "layer": args.layer,
                            "apply": args.apply,
                        },
                    ),
                    ToolContext(
                        memory=memory,
                        config=config,
                        workspace=config.workspace,
                        session_id="cli",
                    ),
                )
                print(execution.content)
                if not execution.success:
                    raise SystemExit(1)
                return

        if args.cmd == "init":
            memory.seal_all()
            print(f"Initialized {backend} memory at {memory_dir}")
            return

        if args.cmd == "put":
            record = MemoryRecord(
                layer=MemoryLayer(args.layer),
                kind=MemoryKind(args.kind),
                title=args.title,
                content=args.text,
                confidence=args.confidence,
                importance=args.importance,
            )
            record_id = memory.put(record)
            memory.seal_all()
            print(record_id)
            return

        if args.cmd == "search":
            hits = memory.retrieve(RetrievalQuery(query=args.query, k_per_layer=args.k))
            for hit in hits:
                print(f"[{hit.record.layer.value}] score={hit.score:.3f} {hit.record.title}")
                print(hit.snippet or hit.record.content[:500])
                print()
            return

        if args.cmd == "compile-context":
            compiler = ContextCompiler(memory)
            compiled = compiler.compile(objective=args.objective, query=args.query)
            print(compiled.prompt)
            return

        if args.cmd == "context":
            compiler = ContextCompiler(memory)
            compiled = compiler.compile(objective=args.query, query=args.query)
            print(compiled.prompt)
            return

    finally:
        memory.close_all()


_CONFIG_ARG_FIELDS = {
    "provider": "provider",
    "model": "model",
    "base_url": "base_url",
    "api_key_env": "api_key_env",
    "timeout_seconds": "timeout_seconds",
    "max_retries": "max_retries",
    "temperature": "temperature",
    "codex_sandbox": "codex_sandbox",
    "codex_profile": "codex_profile",
    "codex_skip_git_repo_check": "codex_skip_git_repo_check",
    "backend": "backend",
    "memory_dir": "memory_dir",
    "layer_config": "layer_config_path",
    "workspace": "workspace",
    "log_dir": "log_dir",
    "state_path": "state_path",
    "secret_store_path": "secret_store_path",  # nosec B105
    "secret_backend": "secret_backend",  # nosec B105
    "skills_dir": "skills_dir",
    "plugins_dir": "plugins_dir",
    "mcp_config": "mcp_config_path",
    "channels_config": "channel_config_path",
    "enable_channel_delivery": "enable_channel_delivery",
    "channel_send_timeout_seconds": "channel_send_timeout_seconds",
    "require_api_auth": "require_api_auth",
    "api_auth_token_env": "api_auth_token_env",  # nosec B105
    "allow_shell": "allow_shell",
    "allow_file_write": "allow_file_write",
    "allow_policy_writes": "allow_policy_writes",
    "allow_codex_cli": "allow_codex_cli",
    "allow_plugin_install": "allow_plugin_install",
    "allow_git_commit": "allow_git_commit",
    "allow_git_push": "allow_git_push",
    "allow_remote_mutation": "allow_remote_mutation",
    "git_write_mode": "git_write_mode",
    "allow_memory_import": "allow_memory_import",
    "allow_executable_skills": "allow_executable_skills",
    "allow_mcp_network_endpoints": "allow_mcp_network_endpoints",
    "allow_web": "allow_web",
    "allow_self_modification": "allow_self_modification",
    "web_backend": "web_backend",
    "web_timeout_seconds": "web_timeout_seconds",
    "web_max_results": "web_max_results",
    "web_max_bytes": "web_max_bytes",
    "enable_autonomous_scheduler": "enable_autonomous_scheduler",
    "max_scheduler_tasks": "max_scheduler_tasks",
    "max_scheduler_cycles": "max_scheduler_cycles",
    "enable_worker_isolation": "enable_worker_isolation",
    "worker_worktree_dir": "worker_worktree_dir",
    "worker_branch_prefix": "worker_branch_prefix",
    "stream": "stream",
    "max_tool_rounds": "max_tool_rounds",
    "context_budget_chars": "context_budget_chars",
    "enable_auto_consolidation": "enable_auto_consolidation",
    "enable_auto_compact": "enable_auto_compact",
    "auto_compact_apply": "auto_compact_apply",
    "context_pack_token_budget": "context_pack_token_budget",  # nosec B105
    "context_pack_expand_raw": "context_pack_expand_raw",
}


def _agent_config_from_args(args: argparse.Namespace) -> AgentConfig:
    config = AgentConfig.from_env()
    overrides: dict[str, Any] = {}
    for arg_name, field_name in _CONFIG_ARG_FIELDS.items():
        if hasattr(args, arg_name):
            overrides[field_name] = getattr(args, arg_name)
    if hasattr(args, "protected_branches"):
        overrides["protected_branches"] = tuple(part.strip() for part in args.protected_branches.split(",") if part.strip())
    if hasattr(args, "codex_persist_session"):
        overrides["codex_ephemeral"] = False
    if hasattr(args, "disable_task_capsules"):
        overrides["enable_task_capsules"] = False
    if hasattr(args, "auto_consolidation_write"):
        overrides["auto_consolidation_dry_run"] = False
    return replace(config, **overrides)


def _specs_from_config(config: AgentConfig) -> dict[MemoryLayer, Any] | None:
    layer_config = config.layer_config_path
    return load_layer_specs(layer_config) if layer_config else None


def _ledger_for_memory_command(args: argparse.Namespace) -> PromotionLedger | None:
    if getattr(args, "cmd", "") != "memory":
        return None
    if getattr(args, "memory_cmd", "") not in {"consolidate", "correct", "compact"}:
        return None
    state_path = Path(getattr(args, "state_path", Path(".nest/state/agent.db")))
    return PromotionLedger(AgentStateStore(state_path))


def _handle_behavior_deltas_command(args: argparse.Namespace) -> None:
    if args.deltas_cmd != "propose":
        raise SystemExit(f"Unknown behavior-delta command: {args.deltas_cmd}")
    summary = summarize_run_capsule(runs_dir=args.runs_dir, run_id=args.run_id, backend=args.backend)
    ledger = BehaviorDeltaLedger(AgentStateStore(args.state_path))
    extractor = BehaviorDeltaExtractor(ledger=ledger)
    proposals = extractor.propose_from_signals(
        summary.learning_signals,
        run_id=args.run_id,
        dry_run=bool(args.dry_run),
    )
    payload = {
        "run_id": args.run_id,
        "dry_run": bool(args.dry_run),
        "proposal_count": len(proposals),
        "proposals": [delta.to_metadata() for delta in proposals],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    action = "Would record" if args.dry_run else "Recorded"
    print(f"{action} {len(proposals)} behavior delta proposal(s) for run {args.run_id}.")
    for delta in proposals:
        print(f"- {delta.id} [{delta.kind.value}/{delta.risk.value}] {delta.title}")


def _print_promotion_ledger(args: argparse.Namespace) -> None:
    ledger = PromotionLedger(AgentStateStore(args.state_path))
    since = _parse_since(args.since)
    target_layer = MemoryLayer(args.layer) if args.layer else None
    summary = ledger.summarize(since=since, target_layer=target_layer, outcome=args.outcome)
    if args.json:
        print(json.dumps(summary.to_payload(), indent=2))
        return
    scope = f"last {args.since}" if since is not None else "all time"
    layer_label = args.layer or "all layers"
    print(f"Promotion ledger - {scope}, {layer_label}")
    if args.outcome:
        print(f"Outcome filter: {args.outcome}")
    print()
    if not summary.rows:
        print("No promotion ledger entries.")
        return
    header = (
        f"{'Layer':<18} {'Promoted':>8} {'Useful':>8} {'Corrected':>10} "
        f"{'Contradicted':>13} {'Tombstoned':>11} {'Never-Used':>11}"
    )
    print(header)
    for row in summary.rows:
        counts = row.outcome_counts
        print(
            f"{row.label:<18} {row.promoted:>8} {counts.get('useful', 0):>8} "
            f"{counts.get('corrected', 0):>10} {counts.get('contradicted', 0):>13} "
            f"{counts.get('tombstoned', 0):>11} {counts.get('never_retrieved', 0):>11}"
        )
    print()
    print("False-positive rate (corrected + contradicted) by gate:")
    for row in summary.rows:
        print(f"  {row.label}: {row.false_positive_rate * 100:.1f}%")
    if summary.recommendations:
        print()
        for recommendation in summary.recommendations:
            print(f"Recommendation: {recommendation}")


def _parse_since(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value or value.lower() in {"all", "all-time", "all_time"}:
        return None
    now = datetime.now(UTC)
    if value.endswith("d") and value[:-1].isdigit():
        return now - timedelta(days=int(value[:-1]))
    if value.endswith("h") and value[:-1].isdigit():
        return now - timedelta(hours=int(value[:-1]))
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_server_bind(host: str, config: AgentConfig) -> None:
    if _is_loopback_host(host):
        return
    if not config.require_api_auth or not _env_has_value(config.api_auth_token_env):
        raise SystemExit(
            "unsafe_bind: non-loopback server hosts require --require-api-auth "
            f"and a configured {config.api_auth_token_env} token."
        )


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _load_channel_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload:
        raw = args.payload
    elif str(args.payload_file) == "-":
        raw = sys.stdin.read()
    else:
        raw = args.payload_file.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise SystemExit("Channel payload must be a JSON object.")
    return payload


def _print_verify_results(results: dict[MemoryLayer, bool]) -> None:
    for layer, ok in results.items():
        print(f"{layer.value}: {'ok' if ok else 'failed'}")


def _doctor_memory(memory: object, *, dry_run: bool) -> dict[str, Any]:
    backends = getattr(memory, "backends", {})
    report: dict[str, Any] = {}
    if not isinstance(backends, dict):
        return {"ok": False, "error": "memory system does not expose backends"}
    for layer, backend in backends.items():
        layer_name = layer.value if isinstance(layer, MemoryLayer) else str(layer)
        doctor = getattr(backend, "doctor", None)
        try:
            if callable(doctor):
                report[layer_name] = doctor(dry_run=dry_run)
            else:
                verify = getattr(backend, "verify", None)
                report[layer_name] = {"ok": bool(verify()) if callable(verify) else False, "doctor_available": False}
        except Exception as exc:  # noqa: BLE001 - CLI doctor must report every layer honestly
            report[layer_name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return report


def _doctor_runtime(config: AgentConfig) -> dict[str, Any]:
    memory_dir_exists = config.memory_dir.exists()
    report: dict[str, Any] = {
        "python": _doctor_python(),
        "package": _doctor_package(),
        "optional_extras": _doctor_optional_extras(),
        "provider": _doctor_provider(config),
        "workspace": _doctor_workspace(config),
        "tool_config": _doctor_tool_config(config),
        "server": _doctor_server(),
        "tests": _doctor_tests(),
    }
    report["memory"] = _doctor_memory_runtime(config, existed_before=memory_dir_exists)
    report["ok"] = all(_section_ok(value) for value in report.values() if isinstance(value, dict))
    return report


def _doctor_python() -> dict[str, Any]:
    version = tuple(sys.version_info[:3])
    return {
        "ok": version >= (3, 11, 0),
        "version": platform.python_version(),
        "executable": sys.executable,
        "requires": ">=3.11",
    }


def _doctor_package() -> dict[str, Any]:
    try:
        version = importlib_metadata.version("nested-memvid-agent")
        installed = True
    except importlib_metadata.PackageNotFoundError:
        version = None
        installed = False
    return {
        "ok": importlib.util.find_spec("nested_memvid_agent") is not None,
        "distribution_installed": installed,
        "version": version,
        "module_importable": importlib.util.find_spec("nested_memvid_agent") is not None,
    }


def _doctor_optional_extras() -> dict[str, Any]:
    extras = {
        "memvid": "memvid_sdk",
        "openai": "openai",
        "server": "fastapi",
        "uvicorn": "uvicorn",
        "mcp": "mcp",
    }
    return {
        "ok": True,
        "extras": {
            name: {"available": importlib.util.find_spec(module_name) is not None}
            for name, module_name in extras.items()
        },
    }


def _doctor_provider(config: AgentConfig) -> dict[str, Any]:
    key_env = config.api_key_env
    if key_env is None and config.provider == "openai":
        key_env = "OPENAI_API_KEY"
    api_key_present = bool(key_env and _env_has_value(key_env))
    needs_key = config.provider == "openai"
    needs_base_url = config.provider == "openai-compatible"
    return {
        "ok": (not needs_key or api_key_present) and (not needs_base_url or bool(config.base_url)),
        "provider": config.provider,
        "model": config.model,
        "base_url_configured": bool(config.base_url),
        "api_key_env": key_env,
        "api_key_present": api_key_present,
        "timeout_seconds": config.timeout_seconds,
        "max_retries": config.max_retries,
        "stream": config.stream,
    }


def _doctor_workspace(config: AgentConfig) -> dict[str, Any]:
    return {
        "ok": config.workspace.exists() and config.workspace.is_dir(),
        "path": str(config.workspace),
        "exists": config.workspace.exists(),
        "is_dir": config.workspace.is_dir(),
    }


def _doctor_tool_config(config: AgentConfig) -> dict[str, Any]:
    return {
        "ok": True,
        "allow_shell": config.allow_shell,
        "allow_file_write": config.allow_file_write,
        "allow_policy_writes": config.allow_policy_writes,
        "allow_codex_cli": config.allow_codex_cli,
        "allow_plugin_install": config.allow_plugin_install,
        "allow_git_commit": config.allow_git_commit,
        "allow_git_push": config.allow_git_push,
        "allow_remote_mutation": config.allow_remote_mutation,
        "git_write_mode": config.git_write_mode,
        "protected_branches": list(config.protected_branches),
        "secret_store_path": str(config.secret_store_path),
        "secret_backend": config.secret_backend,
        "allow_memory_import": config.allow_memory_import,
        "allow_executable_skills": config.allow_executable_skills,
        "allow_mcp_network_endpoints": config.allow_mcp_network_endpoints,
        "allow_web": config.allow_web,
        "allow_self_modification": config.allow_self_modification,
        "require_approval_for_high_risk_tools": config.require_approval_for_high_risk_tools,
        "web_backend": config.web_backend,
        "max_tool_rounds": config.max_tool_rounds,
        "context_budget_chars": config.context_budget_chars,
    }


def _doctor_server() -> dict[str, Any]:
    fastapi_available = importlib.util.find_spec("fastapi") is not None
    uvicorn_available = importlib.util.find_spec("uvicorn") is not None
    return {
        "ok": True,
        "fastapi_available": fastapi_available,
        "uvicorn_available": uvicorn_available,
    }


def _doctor_tests() -> dict[str, Any]:
    return {
        "ok": True,
        "pytest_available": importlib.util.find_spec("pytest") is not None,
        "default_command": "pytest -q",
    }


def _doctor_memory_runtime(config: AgentConfig, *, existed_before: bool) -> dict[str, Any]:
    memvid_available = importlib.util.find_spec("memvid_sdk") is not None
    report: dict[str, Any] = {
        "backend": config.backend,
        "path": str(config.memory_dir),
        "directory_existed_before_doctor": existed_before,
        "memvid_required": config.backend == "memvid",
        "memvid_available": memvid_available,
    }
    if config.backend == "memvid" and not memvid_available:
        report["ok"] = False
        report["error"] = "memvid-sdk is not installed"
        return report

    memory = None
    try:
        specs = load_layer_specs(config.layer_config_path) if config.layer_config_path else None
        memory = build_memory_system(config.backend, config.memory_dir, specs=specs)
        verify = {layer.value: ok for layer, ok in memory.verify_all().items()}
        report["verify"] = verify
        report["ok"] = all(verify.values())
        if config.backend == "memvid":
            report["expected_files"] = {
                layer.value: str(config.memory_dir / f"{layer.value}.mv2") for layer in MemoryLayer
            }
    except Exception as exc:  # noqa: BLE001 - doctor should report readiness failures
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if memory is not None:
            memory.close_all()
    return report


def _section_ok(value: dict[str, Any]) -> bool:
    ok = value.get("ok")
    if isinstance(ok, bool):
        return ok
    return True


def _env_has_value(name: str) -> bool:
    import os

    return bool(os.getenv(name, "").strip())


def _build_run_manager(config: AgentConfig) -> RunManager:
    state = AgentStateStore(config.state_path)
    events = RunEventBus(state)
    mcp = MCPManager(state, allow_network_endpoints=config.allow_mcp_network_endpoints)
    skills = SkillManager(config.skills_dir, state)
    plugins = PluginManager(config.plugins_dir, state)
    return RunManager(config=config, state=state, events=events, mcp=mcp, skills=skills, plugins=plugins)


def _create_run_and_print(
    manager: RunManager,
    message: str,
    *,
    session_id: str,
    json_output: bool,
    wait: bool,
    include_events: bool,
) -> None:
    run = manager.create_run(message=message, session_id=session_id)
    if wait:
        _wait_for_run(manager, run.run_id)
    payload = _run_payload(manager, run.run_id, include_events=include_events)
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    _print_run_payload(payload)


def _run_manager_chat_and_print(
    manager: RunManager,
    message: str,
    *,
    session_id: str,
    prefix: str = "",
) -> None:
    run = manager.create_run(message=message, session_id=session_id)
    _wait_for_run(manager, run.run_id)
    payload = _run_payload(manager, run.run_id, include_events=False)
    assistant_message = str(payload.get("assistant_message") or "")
    print(f"{prefix}{assistant_message}" if prefix else assistant_message)
    print(f"run_id: {payload['run_id']}")
    print(f"status: {payload['status']}")
    if payload.get("stop_reason"):
        print(f"stop_reason: {payload['stop_reason']}")
    _print_pending_approvals(payload)


def _print_status(
    manager: RunManager,
    *,
    run_id: str | None,
    json_output: bool,
    include_events: bool,
) -> None:
    if run_id:
        payload = _run_payload(manager, run_id, include_events=include_events)
        if json_output:
            print(json.dumps(payload, indent=2))
        else:
            _print_run_payload(payload)
        return

    payload = {"runs": manager.list_runs(), "sessions": manager.list_sessions()}
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    if not payload["runs"]:
        print("No runs found.")
        return
    for run in payload["runs"]:
        print(
            f"{run['run_id']} [{run['status']}] "
            f"session={run['session_id']} stop={run['stop_reason'] or '-'}"
        )


def _print_approvals(manager: RunManager, *, status: str | None, json_output: bool) -> None:
    approvals = manager.state.list_approvals(status=status)
    if json_output:
        print(json.dumps({"approvals": approvals}, indent=2))
        return
    if not approvals:
        print("No approvals found.")
        return
    for approval in approvals:
        print(
            f"{approval['approval_id']} [{approval['status']}] "
            f"run={approval['run_id']} tool={approval['tool_name']} risk={approval['risk']}"
        )


def _decide_approval_and_print(
    manager: RunManager,
    *,
    approval_id: str,
    approved: bool,
    arguments_json: str | None,
    json_output: bool,
    wait: bool,
) -> None:
    arguments = _json_object_or_none(arguments_json)
    decision = manager.decide_approval(approval_id, approved=approved, arguments=arguments)
    run_payload: dict[str, Any] | None = None
    if approved and wait:
        _wait_for_run(manager, str(decision["run_id"]))
        run_payload = _run_payload(manager, str(decision["run_id"]), include_events=False)
    elif not approved:
        run_payload = _run_payload(manager, str(decision["run_id"]), include_events=False)

    payload = {"approval": decision, "run": run_payload}
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    print(f"{approval_id}: {'approved' if approved else 'denied'}")
    if run_payload is not None:
        print(f"run_id: {run_payload['run_id']}")
        print(f"status: {run_payload['status']}")
        if run_payload.get("stop_reason"):
            print(f"stop_reason: {run_payload['stop_reason']}")


def _run_payload(manager: RunManager, run_id: str, *, include_events: bool) -> dict[str, Any]:
    payload = manager.get_run(run_id)
    if include_events:
        payload["events"] = manager.state.list_run_steps(run_id)
    return payload


def _print_run_payload(payload: dict[str, Any]) -> None:
    assistant_message = str(payload.get("assistant_message") or "")
    if assistant_message:
        print(assistant_message)
    print(f"run_id: {payload['run_id']}")
    print(f"session_id: {payload['session_id']}")
    print(f"status: {payload['status']}")
    print(f"stop_reason: {payload.get('stop_reason') or '-'}")
    print(f"context_chars: {payload.get('context_chars', 0)}")
    print(f"tool_count: {payload.get('tool_count', 0)}")
    if payload.get("error"):
        print(f"error: {payload['error']}")
    _print_pending_approvals(payload)


def _print_pending_approvals(payload: dict[str, Any]) -> None:
    approvals = [item for item in payload.get("approvals", []) if isinstance(item, dict) and item.get("status") == "pending"]
    if not approvals:
        return
    print("pending_approvals:")
    for approval in approvals:
        print(f"- {approval['approval_id']} tool={approval['tool_name']} risk={approval['risk']}")


def _handle_plugins_command(
    args: argparse.Namespace,
    manager: RunManager,
    *,
    backend: str,
    memory_dir: Path,
) -> None:
    try:
        if args.plugins_cmd == "list":
            _print_plugins(manager.plugins.list_plugins(), json_output=args.json)
            return
        if args.plugins_cmd == "review":
            _require_plugin_install_enabled(manager.config)
            review = manager.plugins.review(args.source, ref=args.ref)
            _print_plugin_review(review, json_output=args.json)
            return
        if args.plugins_cmd == "install":
            _require_plugin_install_enabled(manager.config)
            plugin = manager.plugins.install(
                args.source,
                ref=args.ref,
                enable=args.enable,
                overwrite=args.overwrite,
            )
            _write_plugin_audit(manager, backend=backend, memory_dir=memory_dir, action="install", plugin=plugin)
            _print_plugin(plugin, json_output=args.json)
            return
        if args.plugins_cmd == "inspect":
            _print_plugin(manager.plugins.get_plugin(args.plugin_id), json_output=args.json)
            return
        if args.plugins_cmd == "enable":
            _require_plugin_install_enabled(manager.config)
            plugin = manager.plugins.set_enabled(args.plugin_id, True)
            _write_plugin_audit(manager, backend=backend, memory_dir=memory_dir, action="enable", plugin=plugin)
            _print_plugin(plugin, json_output=args.json)
            return
        if args.plugins_cmd == "disable":
            plugin = manager.plugins.set_enabled(args.plugin_id, False)
            _write_plugin_audit(manager, backend=backend, memory_dir=memory_dir, action="disable", plugin=plugin)
            _print_plugin(plugin, json_output=args.json)
            return
        if args.plugins_cmd == "update":
            _require_plugin_install_enabled(manager.config)
            plugin = manager.plugins.update(args.plugin_id, ref=args.ref)
            _write_plugin_audit(manager, backend=backend, memory_dir=memory_dir, action="update", plugin=plugin)
            _print_plugin(plugin, json_output=args.json)
            return
        if args.plugins_cmd == "remove":
            result = manager.plugins.remove(args.plugin_id)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"removed: {result['plugin_id']}")
            return
    except (PluginError, FileExistsError, KeyError) as exc:
        raise SystemExit(str(exc)) from exc


def _require_plugin_install_enabled(config: AgentConfig) -> None:
    if not config.allow_plugin_install:
        raise SystemExit("plugin_install_disabled")


def _write_plugin_audit(
    manager: RunManager,
    *,
    backend: str,
    memory_dir: Path,
    action: str,
    plugin: dict[str, Any],
) -> None:
    memory = build_memory_system(backend, memory_dir)
    try:
        manager.plugins.write_audit_memory(memory, action=action, plugin=plugin)
    finally:
        memory.close_all()


def _print_plugins(plugins: list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"plugins": plugins}, indent=2))
        return
    if not plugins:
        print("No plugins installed.")
        return
    for plugin in plugins:
        state = "enabled" if plugin["enabled"] else "not enabled"
        capabilities = ", ".join(str(item) for item in plugin.get("capabilities", [])) or "none"
        print(f"{plugin['id']} [{state}] {plugin['source_url']} @ {plugin['commit_sha'][:12]} capabilities={capabilities}")


def _print_plugin(plugin: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(plugin, indent=2))
        return
    state = "enabled" if plugin["enabled"] else "not enabled"
    print(f"{plugin['id']} [{state}]")
    print(f"name: {plugin['name']}")
    print(f"source: {plugin['source_url']}")
    print(f"commit: {plugin['commit_sha']}")
    print(f"format: {plugin['format']}")
    print(f"capabilities: {', '.join(str(item) for item in plugin.get('capabilities', [])) or 'none'}")
    warnings = plugin.get("risk_report", {}).get("warnings", [])
    unsupported = plugin.get("risk_report", {}).get("unsupported_features", [])
    if warnings:
        print(f"warnings: {', '.join(str(item) for item in warnings)}")
    if unsupported:
        print(f"unsupported: {', '.join(str(item) for item in unsupported)}")


def _print_plugin_review(review: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(review, indent=2))
        return
    manifest = review.get("manifest", {})
    plugin_id = manifest.get("id", "unknown") if isinstance(manifest, dict) else "unknown"
    print(f"{plugin_id} [review]")
    print(f"source: {review['source_url']}")
    print(f"commit: {review['commit_sha']}")
    blockers = [str(item) for item in review.get("enable_blockers", [])]
    print(f"enable blockers: {', '.join(blockers) if blockers else 'none'}")


def _wait_for_run(manager: RunManager, run_id: str) -> dict[str, Any]:
    deadline = monotonic() + max(manager.config.timeout_seconds + 15, 15)
    terminal = {"completed", "failed", "blocked", "cancelled"}
    thread = manager._threads.get(run_id)  # CLI wait is in-process; yield to the worker before polling state.
    while monotonic() < deadline:
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.25)
        run = manager.get_run(run_id)
        if str(run["status"]) in terminal:
            return run
        sleep(0.25)
    raise SystemExit(f"Run {run_id} did not finish within the CLI wait timeout.")


def _json_object_or_none(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Approval arguments must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("Approval arguments must be a JSON object.")
    return {str(key): val for key, val in value.items()}


def _run_eval_command(config: AgentConfig) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "run_golden_evals.py"
    command = [
        sys.executable,
        str(script),
        "--backend",
        config.backend,
        "--memory-dir",
        str(config.memory_dir),
        "--provider",
        config.provider,
        "--model",
        config.model,
        "--workspace",
        str(config.workspace),
    ]
    completed = subprocess.run(command, check=False)  # nosec B603
    raise SystemExit(completed.returncode)


def _chat_and_print(agent: NestedMV2Agent, user_message: str, *, session_id: str, prefix: str = "") -> None:
    streamed = False
    prefix_printed = False

    def stream_handler(event: LLMStreamEvent) -> None:
        nonlocal streamed, prefix_printed
        if event.type != "token":
            return
        content = event.content
        if not content:
            return
        if prefix and not prefix_printed:
            print(prefix, end="", flush=True)
            prefix_printed = True
        print(content, end="", flush=True)
        streamed = True

    result = agent.chat(user_message, session_id=session_id, stream_handler=stream_handler)
    if streamed:
        print()
        return
    if prefix_printed:
        print(result.assistant_message)
        return
    print(f"{prefix}{result.assistant_message}" if prefix else result.assistant_message)


def _handle_slash_command_for_manager(
    manager: RunManager,
    config: AgentConfig,
    command: str,
    session_id: str,
) -> bool:
    if not command.startswith("/"):
        return False
    agent = build_agent(config, tools=manager.build_registry(), state=manager.state)
    try:
        return _handle_slash_command(agent, command, session_id, manager=manager)
    finally:
        agent.close()


def _handle_slash_command(
    agent: NestedMV2Agent,
    command: str,
    session_id: str,
    *,
    manager: RunManager | None = None,
) -> bool:
    if not command.startswith("/"):
        return False
    name, _, rest = command.partition(" ")
    query = rest.strip()

    if name in {"/exit", "/quit"}:
        return True

    if name == "/help":
        print(_slash_help())
        return True

    if name in {"/self", "/soul"}:
        execution = agent.tools.execute(
            ToolCall(name="self.inspect", arguments={"include_tools": True}),
            ToolContext(
                memory=agent.memory,
                config=agent.config,
                workspace=agent.config.workspace,
                event_log=agent.event_log,
                session_id=session_id,
            ),
        )
        if not execution.success:
            print(execution.content)
            return True
        data = execution.data
        identity = data.get("identity", {}) if isinstance(data.get("identity"), dict) else {}
        print(f"{identity.get('display_name', 'Soul')} ({identity.get('name', 'Kestrel')})")
        print(identity.get("description", ""))
        print("Memory layers:")
        for layer in data.get("memory_layers", []):
            if isinstance(layer, dict):
                print(f"- {layer.get('layer')}: {layer.get('mv2_file')}")
        print(f"Tools: {len(data.get('tools', []))}")
        return True

    if name == "/capabilities":
        execution = agent.tools.execute(
            ToolCall(name="self.inspect", arguments={"include_tools": True}),
            ToolContext(
                memory=agent.memory,
                config=agent.config,
                workspace=agent.config.workspace,
                event_log=agent.event_log,
                session_id=session_id,
            ),
        )
        if not execution.success:
            print(execution.content)
            return True
        for spec in execution.data.get("tools", []):
            if isinstance(spec, dict):
                approval = "approval required" if spec.get("requires_approval") else "allowed"
                print(f"{spec.get('name')} [{spec.get('risk')}, {approval}] - {spec.get('description')}")
        return True

    if name == "/web":
        if not query:
            print("Usage: /web <query>")
            return True
        execution = agent.tools.execute(
            ToolCall(name="web.search", arguments={"query": query, "max_results": agent.config.web_max_results}),
            ToolContext(
                memory=agent.memory,
                config=agent.config,
                workspace=agent.config.workspace,
                event_log=agent.event_log,
                session_id=session_id,
            ),
        )
        if not execution.success:
            print(execution.content)
            return True
        for item in execution.data.get("results", []):
            if isinstance(item, dict):
                print(f"{item.get('title')}\n{item.get('url')}\n{item.get('snippet')}\n")
        return True

    if name == "/tools":
        for spec in agent.tools.specs():
            approval = "approval required" if spec.requires_approval else "allowed"
            print(f"{spec.name} [{spec.risk}, {approval}] - {spec.description}")
        return True

    if name == "/plugins":
        if manager is None:
            print("Plugin status requires CLI run-manager mode.")
            return True
        _print_plugins(manager.plugins.list_plugins(), json_output=False)
        return True

    if name == "/context":
        if not query:
            print("Usage: /context <query>")
            return True
        compiled = agent.compiler.compile(objective=query, query=query)
        print(compiled.prompt)
        return True

    if name == "/pack":
        if not query:
            print("Usage: /pack <query>")
            return True
        packed = ContextPacker(agent.memory).pack(
            ContextPackRequest(
                objective=query,
                query=query,
                token_budget=agent.config.context_pack_token_budget,
                expand_raw=agent.config.context_pack_expand_raw,
            )
        )
        print(packed.prompt)
        return True

    if name == "/conflicts":
        if not query:
            print("Usage: /conflicts <query>")
            return True
        execution = agent.tools.execute(
            ToolCall(name="memory.conflicts", arguments={"query": query, "k": 8}),
            ToolContext(
                memory=agent.memory,
                config=agent.config,
                workspace=agent.config.workspace,
                event_log=agent.event_log,
                session_id=session_id,
            ),
        )
        print(execution.content)
        return True

    if name == "/capsule":
        run_id = query or session_id
        summary = summarize_run_capsule(
            runs_dir=agent.config.memory_dir.parent / "runs",
            run_id=run_id,
            backend=agent.config.backend,
        )
        print(json.dumps(summary.to_payload(), indent=2))
        return True

    if name == "/memory":
        if not query:
            print("Usage: /memory <query>")
            return True
        hits = agent.memory.retrieve(RetrievalQuery(query=query))
        for hit in hits:
            print(f"[{hit.record.layer.value}] score={hit.score:.3f} {hit.record.title}")
            print(hit.snippet or hit.record.content[:500])
            print()
        if not hits:
            print("No memory hits.")
        return True

    if name == "/doctor":
        results = agent.memory.verify_all()
        for layer, ok in results.items():
            print(f"{layer.value}: {'ok' if ok else 'failed'}")
        return True

    if name == "/status":
        if manager is not None:
            _print_status(manager, run_id=query or None, json_output=False, include_events=False)
        else:
            print(f"session_id: {session_id}")
            print(f"provider: {agent.config.provider}")
            print(f"model: {agent.config.model}")
            print(f"backend: {agent.config.backend}")
            print(f"memory_dir: {agent.config.memory_dir}")
            print(f"tools: {len(agent.tools.specs())}")
        return True

    if name in {"/approve", "/deny"}:
        if manager is None:
            print("Approval decisions require CLI run-manager mode.")
            print("Use `nest-agent approve <approval_id>` or `nest-agent deny <approval_id>`.")
            return True
        approval_id, _, raw_arguments = query.partition(" ")
        if not approval_id:
            print(f"Usage: {name} <approval_id> [arguments-json]")
            return True
        _decide_approval_and_print(
            manager,
            approval_id=approval_id,
            approved=name == "/approve",
            arguments_json=raw_arguments.strip() or None,
            json_output=False,
            wait=True,
        )
        return True

    if name == "/session":
        print(f"session_id: {session_id}")
        if agent.event_log is not None:
            events = agent.event_log.tail(limit=5)
            print(f"recent_events: {len(events)}")
            for event in events:
                print(f"- {event.created_at} {event.type}")
        return True

    print(f"Unknown slash command: {name}")
    return True


def _slash_help() -> str:
    return "\n".join(
        [
            "Available slash commands:",
            "/help",
            "/self",
            "/soul",
            "/capabilities",
            "/web <query>",
            "/tools",
            "/plugins",
            "/context <query>",
            "/pack <query>",
            "/conflicts <query>",
            "/memory <query>",
            "/doctor",
            "/status",
            "/session",
            "/capsule [run_id]",
            "/approve <approval_id>",
            "/deny <approval_id>",
            "/exit",
        ]
    )


if __name__ == "__main__":
    main()
