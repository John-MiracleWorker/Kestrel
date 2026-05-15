from __future__ import annotations

import argparse
from pathlib import Path

from .agent import NestedMV2Agent
from .app_factory import build_agent
from .config import AgentConfig
from .context_compiler import ContextCompiler
from .models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from .orchestrator import build_memory_system
from .runtime_models import LLMStreamEvent


def _add_common_args(parser: argparse.ArgumentParser, *, default: object = argparse.SUPPRESS) -> None:
    parser.add_argument("--backend", choices=["memory", "memvid"], default=default)
    parser.add_argument("--memory-dir", type=Path, default=default)


def _add_agent_args(parser: argparse.ArgumentParser) -> None:
    _add_common_args(parser)
    parser.add_argument("--provider", choices=["mock", "openai", "openai-compatible"], default="mock")
    parser.add_argument("--model", default="mock")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--log-dir", type=Path, default=Path(".nest/logs"))
    parser.add_argument("--allow-shell", action="store_true")
    parser.add_argument("--allow-file-write", action="store_true")
    parser.add_argument("--allow-policy-writes", action="store_true")
    parser.add_argument("--allow-codex-cli", action="store_true")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--max-tool-rounds", type=int, default=6)
    parser.add_argument("--context-budget-chars", type=int, default=18_000)


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

    compile_cmd = sub.add_parser("compile-context")
    _add_common_args(compile_cmd)
    compile_cmd.add_argument("--objective", required=True)
    compile_cmd.add_argument("--query")

    chat = sub.add_parser("chat")
    _add_agent_args(chat)
    chat.add_argument("--message", help="Run one chat turn. If omitted, enter interactive mode.")
    chat.add_argument("--session-id", default="cli")

    doctor = sub.add_parser("doctor")
    _add_common_args(doctor)

    server = sub.add_parser("server")
    _add_agent_args(server)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)

    args = parser.parse_args()
    backend = getattr(args, "backend", "memory")
    memory_dir = getattr(args, "memory_dir", Path("./memory"))

    if args.cmd == "chat":
        config = _agent_config_from_args(args, backend=backend, memory_dir=memory_dir)
        agent = build_agent(config)
        try:
            if args.message:
                if _handle_slash_command(agent, args.message.strip(), args.session_id):
                    return
                _chat_and_print(agent, args.message, session_id=args.session_id)
                return
            print("Nested MV2 Agent chat. Type /exit to quit.")
            while True:
                user_message = input("you> ").strip()
                if user_message in {"/exit", "/quit"}:
                    return
                if not user_message:
                    continue
                if _handle_slash_command(agent, user_message, args.session_id):
                    continue
                _chat_and_print(agent, user_message, session_id=args.session_id, prefix="agent> ")
        finally:
            agent.close()
        return

    if args.cmd == "server":
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError("Install server extras with `pip install -e '.[server]'`.") from exc
        from .server import create_app

        config = _agent_config_from_args(args, backend=backend, memory_dir=memory_dir)
        uvicorn.run(create_app(config), host=args.host, port=args.port)
        return

    memory = build_memory_system(backend, memory_dir)
    try:
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

        if args.cmd == "doctor":
            results = memory.verify_all()
            for layer, ok in results.items():
                print(f"{layer.value}: {'ok' if ok else 'failed'}")
            return
    finally:
        memory.close_all()


def _agent_config_from_args(args: argparse.Namespace, *, backend: str, memory_dir: Path) -> AgentConfig:
    return AgentConfig(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        temperature=args.temperature,
        backend=backend,
        memory_dir=memory_dir,
        workspace=args.workspace,
        log_dir=args.log_dir,
        allow_shell=args.allow_shell,
        allow_file_write=args.allow_file_write,
        allow_policy_writes=args.allow_policy_writes,
        allow_codex_cli=args.allow_codex_cli,
        stream=args.stream,
        max_tool_rounds=args.max_tool_rounds,
        context_budget_chars=args.context_budget_chars,
    )


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


def _handle_slash_command(agent: NestedMV2Agent, command: str, session_id: str) -> bool:
    if not command.startswith("/"):
        return False
    name, _, rest = command.partition(" ")
    query = rest.strip()

    if name in {"/exit", "/quit"}:
        return True

    if name == "/tools":
        for spec in agent.tools.specs():
            approval = "approval required" if spec.requires_approval else "allowed"
            print(f"{spec.name} [{spec.risk}, {approval}] - {spec.description}")
        return True

    if name == "/context":
        if not query:
            print("Usage: /context <query>")
            return True
        compiled = agent.compiler.compile(objective=query, query=query)
        print(compiled.prompt)
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
