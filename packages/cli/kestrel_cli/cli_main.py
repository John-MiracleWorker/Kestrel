from __future__ import annotations

from . import cli_memory as _cli_memory
from .cli_tui import launch_tui

globals().update({name: value for name, value in vars(_cli_memory).items() if not name.startswith("__")})

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="kestrel",
        description="🦅 Kestrel CLI — Autonomous Agent Platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  kestrel                              Launch the full-screen TUI
  kestrel repl                         Launch the classic REPL
  kestrel tui                          Launch the full-screen TUI
  kestrel task "review auth module"    Start an autonomous task
  kestrel tasks                        List all tasks
  kestrel workflows                    Browse workflow templates
  kestrel cron                         Manage scheduled jobs
  kestrel webhooks                     Manage webhook endpoints
  kestrel status                       Show system status
  kestrel config api_url http://...    Set configuration
        """,
    )

    subparsers = parser.add_subparsers(dest="command")

    # task
    task_p = subparsers.add_parser("task", help="Start an autonomous agent task")
    task_p.add_argument("goal", nargs="+", help="Task goal")

    # tasks
    tasks_p = subparsers.add_parser("tasks", help="List agent tasks")
    tasks_p.add_argument("--status", help="Filter by status")

    # workflows
    subparsers.add_parser("workflows", help="List workflow templates")

    # cron
    subparsers.add_parser("cron", help="List cron jobs")

    # webhooks
    subparsers.add_parser("webhooks", help="List webhook endpoints")

    # status
    subparsers.add_parser("status", help="Show system status")

    # tui / repl
    subparsers.add_parser("tui", help="Launch the full-screen terminal UI")
    subparsers.add_parser("repl", help="Launch the classic interactive REPL")

    # doctor
    doctor_p = subparsers.add_parser("doctor", help="Run local daemon diagnostics")
    doctor_p.add_argument("--repair", action="store_true", help="Apply safe local repair steps")

    # onboard
    subparsers.add_parser("onboard", help="Prepare the local Telegram-first Kestrel home")

    # channels
    subparsers.add_parser("channels", help="Show configured companion channels")

    # monitor
    subparsers.add_parser("monitor", help="Show a local Flight Deck snapshot")

    # runtime
    subparsers.add_parser("runtime", help="Show native runtime profile")

    # paired-nodes
    subparsers.add_parser("paired-nodes", help="Show registered paired nodes")

    # skill
    skill_p = subparsers.add_parser("skill", help="Manage skill packs")
    skill_subparsers = skill_p.add_subparsers(dest="skill_cmd", help="Skill subcommand")

    skill_list_p = skill_subparsers.add_parser("list", help="List skill packs")
    skill_list_p.add_argument("--include-synthetic", action=argparse.BooleanOptionalAction, default=True)
    skill_list_p.add_argument("--include-marketplace", action=argparse.BooleanOptionalAction, default=True)

    skill_search_p = skill_subparsers.add_parser("search", help="Search skill packs")
    skill_search_p.add_argument("query", nargs="+", help="Search query")
    skill_search_p.add_argument("--include-marketplace", action=argparse.BooleanOptionalAction, default=True)

    skill_inspect_p = skill_subparsers.add_parser("inspect", help="Inspect a skill pack")
    skill_inspect_p.add_argument("pack_id", help="Skill pack id")

    skill_install_p = skill_subparsers.add_parser("install", help="Install a skill pack")
    skill_install_p.add_argument("pack_id", nargs="?", default="", help="Marketplace or bundled skill pack id")
    skill_install_p.add_argument("--source-path", default="", help="Local pack directory or archive path")
    skill_install_p.add_argument("--source-url", default="", help="Remote archive URL")
    skill_install_p.add_argument("--scope", choices=["user", "workspace"], default="user")

    skill_import_p = skill_subparsers.add_parser("import", help="Import a local skill pack folder or archive")
    skill_import_p.add_argument("source_path", help="Local pack directory or archive path")
    skill_import_p.add_argument("--scope", choices=["user", "workspace"], default="user")

    skill_enable_p = skill_subparsers.add_parser("enable", help="Enable an installed skill pack")
    skill_enable_p.add_argument("pack_id", help="Skill pack id")

    skill_disable_p = skill_subparsers.add_parser("disable", help="Disable an installed skill pack")
    skill_disable_p.add_argument("pack_id", help="Skill pack id")

    skill_remove_p = skill_subparsers.add_parser("remove", help="Remove an installed skill pack")
    skill_remove_p.add_argument("pack_id", help="Skill pack id")

    # shutdown
    subparsers.add_parser("shutdown", help="Stop the local daemon")

    # install
    subparsers.add_parser("install", help="Install Kestrel as a background macOS daemon")

    # config
    config_p = subparsers.add_parser("config", help="Configure settings")
    config_p.add_argument("key", nargs="?", help="Config key")
    config_p.add_argument("value", nargs="?", help="Config value")

    # memory
    memory_parser = subparsers.add_parser("memory", help="Manage Kestrel transparent knowledge memory")
    memory_subparsers = memory_parser.add_subparsers(dest="memory_cmd", help="Memory subcommand")
    
    mem_show_parser = memory_subparsers.add_parser("show", help="Show memory contents")
    mem_show_parser.add_argument("category", nargs="?", help="Specific memory category to show (e.g. preferences)")
    
    mem_edit_parser = memory_subparsers.add_parser("edit", help="Open memory directory in default editor")

    memory_subparsers.add_parser("sync", help="Sync markdown memory into the native index")

    return parser


def main():
    """Main entry point."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args()

    config = load_config()
    client = KestrelClient(config)

    command_map = {
        "task": cmd_task,
        "tasks": cmd_tasks,
        "workflows": cmd_workflows,
        "cron": cmd_cron,
        "webhooks": cmd_webhooks,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "onboard": cmd_onboard,
        "channels": cmd_channels,
        "monitor": cmd_monitor,
        "runtime": cmd_runtime,
        "paired-nodes": cmd_paired_nodes,
        "skill": cmd_skill,
        "shutdown": cmd_shutdown,
        "config": cmd_config,
        "install": cmd_install,
    }

    if args.command == "memory":
        if args.memory_cmd == "show":
            cmd_memory_show(args, config)
        elif args.memory_cmd == "edit":
            cmd_memory_edit(args, config)
        elif args.memory_cmd == "sync":
            result = asyncio.run(client.sync_memory())
            print_success(f"Indexed {result.get('indexed_files', 0)} markdown files.")
            namespaces = result.get("namespaces", [])
            if namespaces:
                print_info(f"Namespaces: {', '.join(namespaces)}")
        else:
            parser.parse_args(["memory", "--help"])
        return

    if args.command == "tui":
        if not launch_tui(client, config):
            print_error("TUI launch failed. Falling back requires `kestrel repl`.")
            raise SystemExit(1)
        return

    if args.command == "repl":
        asyncio.run(interactive_repl(client, config))
        return

    if args.command and args.command in command_map:
        asyncio.run(command_map[args.command](client, args))
    else:
        if sys.stdin.isatty() and sys.stdout.isatty() and launch_tui(client, config):
            return
        asyncio.run(interactive_repl(client, config))


if __name__ == "__main__":
    main()
