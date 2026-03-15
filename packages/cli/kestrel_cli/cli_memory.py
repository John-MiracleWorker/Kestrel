from __future__ import annotations

from . import cli_output as _cli_output

globals().update({name: value for name, value in vars(_cli_output).items() if not name.startswith("__")})

def load_channel_state(paths) -> dict:
    """Load shared Gateway channel state from the local Kestrel home."""
    state_path = paths.state_dir / "gateway-channels.json"
    if not state_path.exists():
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


async def cmd_task(client: KestrelClient, args: argparse.Namespace):
    """Start an autonomous agent task."""
    goal = " ".join(args.goal)
    print_header("Starting Task")
    print(c(f"  Goal: {goal}", Colors.WHITE))
    print()

    start_time = time.time()
    async for event in client.start_task(goal):
        print_event(event)

    elapsed = time.time() - start_time
    print()
    print(c(f"  ⏱  Completed in {elapsed:.1f}s", Colors.MUTED))


async def cmd_tasks(client: KestrelClient, args: argparse.Namespace):
    """List agent tasks."""
    print_header("Tasks")
    result = await client.list_tasks(args.status if hasattr(args, "status") else None)

    tasks = result.get("tasks", [])
    if not tasks:
        print_info("No tasks found")
        return

    headers = ["ID", "Goal", "Status", "Created"]
    rows = []
    for t in tasks:
        rows.append([
            t.get("id", "")[:8],
            (t.get("goal", ""))[:40],
            t.get("status", ""),
            t.get("created_at", "")[:16],
        ])

    print_table(headers, rows, [10, 42, 12, 18])


async def cmd_workflows(client: KestrelClient, args: argparse.Namespace):
    """List workflow templates."""
    print_header("Workflow Templates")
    result = await client.list_workflows()

    workflows = result.get("workflows", [])
    if not workflows:
        print_info("No workflows available")
        return

    for wf in workflows:
        icon = wf.get("icon", "📋")
        name = wf.get("name", "")
        desc = wf.get("description", "")[:60]
        category = wf.get("category", "")
        print(f"  {icon} {c(name, Colors.BOLD + Colors.WHITE)}  {c(f'[{category}]', Colors.MUTED)}")
        print(c(f"     {desc}", Colors.DIM))
        print()


async def cmd_cron(client: KestrelClient, args: argparse.Namespace):
    """List cron jobs."""
    print_header("Cron Jobs")
    result = await client.list_cron_jobs()

    jobs = result.get("jobs", [])
    if not jobs:
        print_info("No cron jobs configured")
        return

    headers = ["Name", "Schedule", "Status", "Runs", "Last Run"]
    rows = []
    for j in jobs:
        rows.append([
            j.get("name", "")[:20],
            j.get("cron_expression", ""),
            j.get("status", ""),
            str(j.get("run_count", 0)),
            (j.get("last_run", "never") or "never")[:16],
        ])

    print_table(headers, rows, [22, 16, 10, 6, 18])


async def cmd_webhooks(client: KestrelClient, args: argparse.Namespace):
    """List webhook endpoints."""
    print_header("Webhook Endpoints")
    result = await client.list_webhooks()

    webhooks = result.get("webhooks", [])
    if not webhooks:
        print_info("No webhooks configured")
        return

    headers = ["Name", "Status", "Triggers", "Has Secret"]
    rows = []
    for w in webhooks:
        rows.append([
            w.get("name", "")[:25],
            w.get("status", ""),
            str(w.get("trigger_count", 0)),
            "✓" if w.get("has_secret") else "✗",
        ])

    print_table(headers, rows, [27, 10, 10, 12])


async def cmd_status(client: KestrelClient, args: argparse.Namespace):
    """Show system status."""
    config = load_config()
    print_header("System Status")
    try:
        status = await client.status()
    except Exception:
        status = {}

    if status:
        uptime = int(status.get("uptime_seconds", 0))
        days, rem = divmod(uptime, 86400)
        hrs, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        uptime_str = f"{days}d {hrs}h {mins}m" if days > 0 else f"{hrs}h {mins}m"
        print(c(f"  Native daemon running (uptime: {uptime_str})", Colors.SUCCESS))
        runtime = status.get("runtime_profile", {})
        local_models = runtime.get("local_models", {})
        default_provider = local_models.get("default_provider") or "none"
        default_model = local_models.get("default_model") or "none"
        print(f"  {c('Control:', Colors.MUTED)}    {c(status.get('control_socket', 'unknown'), Colors.PRIMARY)}")
        print(f"  {c('Runtime:', Colors.MUTED)}    {c(runtime.get('runtime_mode', 'native'), Colors.WHITE)}")
        print(f"  {c('Model:', Colors.MUTED)}      {c(f'{default_provider}:{default_model}', Colors.WHITE)}")
        print(f"  {c('Approvals:', Colors.MUTED)}  {c(str(len(status.get('pending_approvals', []))), Colors.WHITE)}")
        print(f"  {c('API:', Colors.MUTED)}        {c(config.get('api_url', 'not set'), Colors.PRIMARY)}")
        print(f"  {c('Workspace:', Colors.MUTED)}  {c(config.get('workspace_id', 'not set'), Colors.WHITE)}")
        print(f"  {c('Model pref:', Colors.MUTED)} {c(config.get('model', 'default'), Colors.WHITE)}")
        print(f"  {c('Thinking:', Colors.MUTED)}   {c(config.get('thinking_level', 'medium'), Colors.WHITE)}")
        print(f"  {c('Usage:', Colors.MUTED)}      {c(config.get('usage_mode', 'tokens'), Colors.WHITE)}")
        return
    
    state_file = os.path.expanduser("~/.kestrel/state/heartbeat.json")
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
            ago = int(time.time() - state.get("last_heartbeat", 0))
            uptime = int(state.get("uptime", 0))
            days, rem = divmod(uptime, 86400)
            hrs, rem = divmod(rem, 3600)
            mins, seq = divmod(rem, 60)
            uptime_str = f"{days}d {hrs}h {mins}m" if days > 0 else f"{hrs}h {mins}m"
            print(c(f"  🦅 Kestrel Agent OS — Running (uptime: {uptime_str})", Colors.SUCCESS))
            print(c("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.MUTED))
            print(f"  {c('HEARTBEAT', Colors.KESTREL)}     Last: {ago}s ago | State: {state.get('status')}")
            print()
        except Exception:
            print(c("  🦅 Kestrel Agent OS — Offline or state unreadable", Colors.ERROR))
            print()
    else:
        print(c("  🦅 Kestrel Agent OS — Offline (daemon not running)", Colors.MUTED))
        print()

    print(f"  {c('API:', Colors.MUTED)}        {c(config.get('api_url', 'not set'), Colors.PRIMARY)}")
    print(f"  {c('Workspace:', Colors.MUTED)}  {c(config.get('workspace_id', 'not set'), Colors.WHITE)}")
    print(f"  {c('Model:', Colors.MUTED)}      {c(config.get('model', 'default'), Colors.WHITE)}")
    print(f"  {c('Thinking:', Colors.MUTED)}   {c(config.get('thinking_level', 'medium'), Colors.WHITE)}")
    print(f"  {c('Usage:', Colors.MUTED)}      {c(config.get('usage_mode', 'tokens'), Colors.WHITE)}")


async def cmd_doctor(client: KestrelClient, args: argparse.Namespace):
    """Run local runtime diagnostics."""
    print_header("Kestrel Doctor")
    report = await client.doctor()
    summary = report.get("summary", {})
    health_color = Colors.SUCCESS if summary.get("healthy") else Colors.WARNING
    print(f"  {c('Healthy:', Colors.MUTED)}   {c(str(bool(summary.get('healthy'))), health_color)}")
    print(f"  {c('Warnings:', Colors.MUTED)}  {c(str(summary.get('warnings', 0)), Colors.WHITE)}")
    print(f"  {c('Errors:', Colors.MUTED)}    {c(str(summary.get('errors', 0)), Colors.WHITE)}")
    print()

    for check in report.get("checks", []):
        status = check.get("status", "unknown")
        color = Colors.SUCCESS if status == "ok" else Colors.WARNING if status == "warning" else Colors.ERROR
        print(f"  {c(status.upper().ljust(7), color)} {check.get('name', 'check')}: {check.get('detail', '')}")

    paths = report.get("paths", {})
    if paths:
        print()
        print(f"  {c('Home:', Colors.MUTED)}      {c(paths.get('home', ''), Colors.WHITE)}")
        print(f"  {c('Socket:', Colors.MUTED)}    {c(paths.get('control_socket', ''), Colors.WHITE)}")
        print(f"  {c('SQLite:', Colors.MUTED)}    {c(paths.get('sqlite_db', ''), Colors.WHITE)}")

    if getattr(args, "repair", False):
        print()
        print_header("Repair Actions")
        repaired = []
        ensure_home_layout()
        repaired.append("Ensured local Kestrel home layout exists")
        if client._use_local_control():
            memory_result = await client.sync_memory()
            repaired.append(f"Synced markdown memory ({memory_result.get('indexed_files', 0)} files)")
        else:
            repaired.append("Daemon unavailable; skipped live memory sync")
        for item in repaired:
            print_success(item)


async def cmd_onboard(client: KestrelClient, args: argparse.Namespace):
    """Prepare the local Kestrel home and summarize the Telegram-first setup."""
    print_header("Kestrel Onboard")
    paths = ensure_home_layout()
    print_success(f"Prepared local home at {paths.home}")

    state = load_channel_state(paths)
    telegram = state.get("telegram") or {}
    telegram_config = telegram.get("config") or {}
    if telegram_config.get("token"):
        workspace_id = telegram_config.get("workspaceId", "default")
        mode = telegram_config.get("mode", "polling")
        print_info(f"Telegram bot configured for workspace {workspace_id} ({mode})")
    else:
        print_warning("Telegram bot is not configured yet. Use the desktop settings or Gateway integration route.")

    if client._use_local_control():
        doctor = await client.doctor()
        summary = doctor.get("summary", {})
        print_info(
            f"Doctor summary: healthy={summary.get('healthy')} "
            f"warnings={summary.get('warnings', 0)} errors={summary.get('errors', 0)}"
        )
    else:
        print_warning("Local daemon is not connected. Run `kestrel install` to enable background startup.")


async def cmd_channels(client: KestrelClient, args: argparse.Namespace):
    """Show configured companion channels from the shared local store."""
    print_header("Channels")
    state = load_channel_state(client.paths)
    telegram = state.get("telegram") or {}
    config = telegram.get("config") or {}
    session = telegram.get("state") or {}

    if not config:
        print_info("No companion channels configured")
        return

    print(f"  {c('Telegram:', Colors.MUTED)}  {c('configured', Colors.SUCCESS)}")
    print(f"  {c('Workspace:', Colors.MUTED)} {c(config.get('workspaceId', 'default'), Colors.WHITE)}")
    print(f"  {c('Mode:', Colors.MUTED)}      {c(config.get('mode', 'polling'), Colors.WHITE)}")
    mappings = session.get("mappings", [])
    print(f"  {c('Pairings:', Colors.MUTED)}  {c(str(len(mappings)), Colors.WHITE)}")
    if mappings:
        latest = mappings[-1]
        print(
            f"  {c('Latest:', Colors.MUTED)}    "
            f"{c(str(latest.get('chatId')), Colors.WHITE)} -> {c(str(latest.get('userId')), Colors.WHITE)}"
        )


async def cmd_monitor(client: KestrelClient, args: argparse.Namespace):
    """Show a local Telegram-first operator snapshot."""
    print_header("Flight Deck")
    status = await client.status()
    runtime = status.get("runtime_profile", {})
    channels = load_channel_state(client.paths)
    telegram = ((channels.get("telegram") or {}).get("config") or {})

    print(f"  {c('Runtime:', Colors.MUTED)}   {c(runtime.get('runtime_mode', 'unknown'), Colors.WHITE)}")
    print(f"  {c('Model:', Colors.MUTED)}     {c(runtime.get('local_models', {}).get('default_model', 'none'), Colors.WHITE)}")
    print(f"  {c('Approvals:', Colors.MUTED)} {c(str(len(status.get('pending_approvals', []))), Colors.WHITE)}")
    print(f"  {c('Tasks:', Colors.MUTED)}     {c(str(len(status.get('recent_tasks', []))), Colors.WHITE)}")
    print(
        f"  {c('Telegram:', Colors.MUTED)}  "
        f"{c('configured' if telegram.get('token') else 'not configured', Colors.WHITE)}"
    )

    recent_tasks = status.get("recent_tasks", [])
    if recent_tasks:
        print()
        headers = ["ID", "Goal", "Status"]
        rows = [
            [task.get("id", "")[:8], task.get("goal", "")[:42], task.get("status", "")]
            for task in recent_tasks[:5]
        ]
        print_table(headers, rows, [10, 44, 14])


async def cmd_runtime(client: KestrelClient, args: argparse.Namespace):
    """Show native runtime profile."""
    print_header("Runtime Profile")
    profile = await client.runtime_profile()
    if not profile:
        print_error("Native runtime profile unavailable")
        return

    print(f"  {c('Mode:', Colors.MUTED)}      {c(profile.get('runtime_mode', 'unknown'), Colors.WHITE)}")
    print(f"  {c('Policy:', Colors.MUTED)}    {c(profile.get('policy_name', 'unknown'), Colors.WHITE)}")
    print(f"  {c('Updated:', Colors.MUTED)}   {c(profile.get('updated_at', 'unknown'), Colors.WHITE)}")

    local_models = profile.get("local_models", {})
    print(f"  {c('Provider:', Colors.MUTED)}  {c(local_models.get('default_provider', 'none'), Colors.WHITE)}")
    print(f"  {c('Model:', Colors.MUTED)}     {c(local_models.get('default_model', 'none'), Colors.WHITE)}")

    capabilities = profile.get("runtime_capabilities", {})
    if capabilities:
        print()
        for name, value in capabilities.items():
            print(f"  {c(name + ':', Colors.MUTED):<34}{c(str(value), Colors.WHITE)}")


async def cmd_paired_nodes(client: KestrelClient, args: argparse.Namespace):
    """Show registered paired nodes."""
    print_header("Paired Nodes")
    payload = await client.paired_nodes()
    nodes = payload.get("nodes", [])
    if not nodes:
        print_info("No paired nodes registered")
        return

    rows = [
        [
            node.get("node_id", ""),
            node.get("node_type", ""),
            node.get("platform", ""),
            node.get("health", ""),
            ",".join((node.get("capabilities", []) or [])[:3]),
        ]
        for node in nodes
    ]
    print_table(["Node", "Type", "Platform", "Health", "Capabilities"], rows)


async def cmd_shutdown(client: KestrelClient, args: argparse.Namespace):
    """Stop the local daemon."""
    print_header("Stopping Kestrel Daemon")
    result = await client.shutdown()
    status = result.get("status", "unknown")
    if status == "stopping":
        print_success("Daemon shutdown requested.")
    else:
        print_error(f"Shutdown failed: {status}")


async def cmd_config(client: KestrelClient, args: argparse.Namespace):
    """Configure Kestrel CLI settings."""
    config = load_config()

    if hasattr(args, "key") and args.key:
        key = args.key
        if hasattr(args, "value") and args.value:
            config[key] = args.value
            save_config(config)
            print_success(f"{key} = {args.value}")
        else:
            val = config.get(key, "(not set)")
            print(f"  {c(key, Colors.PRIMARY)} = {c(str(val), Colors.WHITE)}")
    else:
        print_header("Configuration")
        for k, v in config.items():
            if k == "api_key" and v:
                v = v[:8] + "..." + v[-4:]
            print(f"  {c(k, Colors.PRIMARY):>30} = {c(str(v), Colors.WHITE)}")
        print()
        print(c("  Set a value: kestrel config <key> <value>", Colors.DIM))


async def cmd_install(client: KestrelClient, args: argparse.Namespace):
    """Install Kestrel as a persistent background daemon (macOS, Linux, Windows)."""
    print_header("Installing Kestrel Daemon")
    cli_dir = os.path.abspath(os.path.dirname(__file__))
    daemon_path = os.path.join(cli_dir, "kestrel_daemon.py")

    if not os.path.exists(daemon_path):
        print_error(f"Daemon script not found at {daemon_path}")
        return
    try:
        paths = ensure_home_layout()
        result = install_daemon_service(
            daemon_path=daemon_path,
            python_executable=sys.executable,
            paths=paths,
        )
        print_success(f"Daemon installed via {result['manager']}.")
        print_info(f"Service file: {result['service_path']}")
        print_info(f"State directory: {paths.home}")
    except Exception as exc:
        print_error(str(exc))


# ── Memory CLI Commands ──────────────────────────────────────────────

def cmd_memory_show(args, config: dict):
    """Show contents of Kestrel Dual Memory markdown files."""
    import glob
    import os
    memory_base = os.path.expanduser(config.get("memory_dir", "~/.kestrel/memory"))
    
    # Check if a category was provided (e.g. "preferences")
    category = args.category.lower() if hasattr(args, "category") and args.category else None
    
    # We look inside the first workspace folder we find, or default
    ws_dirs = [d for d in glob.glob(os.path.join(memory_base, "*")) if os.path.isdir(d)]
    if not ws_dirs:
        print_info("No memory synchronized yet. The daemon will sync memory shortly.")
        return
        
    ws_dir = ws_dirs[0]  # Just use the first one for CLI
    print_info(f"Showing memory for workspace: {os.path.basename(ws_dir)}\n")
    
    if category:
        filename = f"{category}.md" if category.endswith('s') else f"{category}s.md"
        filepaths = [os.path.join(ws_dir, filename)]
        if not os.path.exists(filepaths[0]):
            filepaths = [os.path.join(ws_dir, f"{category}.md")] # Fallback to singular
            if not os.path.exists(filepaths[0]):
                 print_error(f"No memory found for category: {category}")
                 return
    else:
        filepaths = glob.glob(os.path.join(ws_dir, "*.md"))
        
    for fp in filepaths:
        if not os.path.exists(fp):
            continue
        print(c(f"--- {os.path.basename(fp)} ---", Colors.KESTREL))
        try:
            with open(fp, "r") as f:
                print(f.read())
        except Exception as e:
            print_error(f"Could not read {fp}: {e}")
        print()


def cmd_memory_edit(args, config: dict):
    """Open Kestrel memory directory in the default editor."""
    import subprocess
    import platform
    memory_base = os.path.expanduser(config.get("memory_dir", "~/.kestrel/memory"))

    print_info(f"Opening memory directory: {memory_base}")
    if not os.path.exists(memory_base):
        os.makedirs(memory_base, exist_ok=True)
        print_info("Created new memory directory.")

    editor = os.environ.get("EDITOR", "")
    plat = platform.system()

    def open_folder_native():
        """Open the folder in the OS file manager."""
        try:
            if plat == "Darwin":
                subprocess.run(["open", memory_base])
            elif plat == "Windows":
                os.startfile(memory_base)  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", memory_base])
        except Exception as e:
            print_error(f"Could not open folder: {e}")
            print_info(f"Memory files are at: {memory_base}")

    terminal_editors = ("nano", "vim", "vi", "emacs", "pico", "micro")
    if editor and editor not in terminal_editors:
        try:
            subprocess.run([editor, memory_base])
            return
        except Exception as e:
            print_error(f"Failed to launch editor ({editor}): {e}")

    # Fall back to native folder opener for terminal editors or when EDITOR is unset
    open_folder_native()


# ── Interactive REPL ─────────────────────────────────────────────────

async def interactive_repl(client: KestrelClient, config: dict):
    """Run the interactive Kestrel REPL."""
    from agent.commands import CommandParser  # noqa: delayed import

    parser = CommandParser()
    print_logo()
    print(c("  Type a message to chat, /help for commands, or Ctrl+C to exit.", Colors.DIM))
    print()

    context = {
        "model": config.get("model", ""),
        "total_tokens": 0,
        "cost_usd": 0,
        "task_status": "idle",
        "session_type": "main",
        "thinking_level": config.get("thinking_level", "medium"),
        "usage_mode": config.get("usage_mode", "tokens"),
    }

    while True:
        try:
            prompt = c("🦅 kestrel", Colors.KESTREL) + c(" ❯ ", Colors.MUTED)
            user_input = input(prompt).strip()

            if not user_input:
                continue

            # Check for /commands
            if parser.is_command(user_input):
                result = parser.parse(user_input, context)
                if result:
                    print(f"\n{result.response}\n")

                    # Apply side effects
                    se = result.side_effects
                    if se.get("action") == "set_thinking_level":
                        context["thinking_level"] = se["value"]
                        config["thinking_level"] = se["value"]
                        save_config(config)
                    elif se.get("action") == "set_usage_mode":
                        context["usage_mode"] = se["value"]
                        config["usage_mode"] = se["value"]
                        save_config(config)
                    elif se.get("action") == "set_model":
                        context["model"] = se["value"]
                        config["model"] = se["value"]
                        save_config(config)
                    elif se.get("action") == "reset_session":
                        context["total_tokens"] = 0
                        context["cost_usd"] = 0
                continue

            # Check for task prefix
            if user_input.startswith("!"):
                # Direct task: !goal launches an autonomous task
                goal = user_input[1:].strip()
                if goal:
                    print()
                    start_time = time.time()
                    async for event in client.start_task(goal):
                        print_event(event)
                    elapsed = time.time() - start_time
                    print(c(f"\n  ⏱  Task completed in {elapsed:.1f}s\n", Colors.MUTED))
                continue

            # Regular chat message — stream via SSE
            print()
            if client._use_local_control():
                response = await client.chat(user_input)
                if response.get("error"):
                    print_error(response["error"])
                else:
                    print(c(response.get("message", ""), Colors.WHITE))
                    if response.get("plan"):
                        plan = response["plan"] or {}
                        steps = plan.get("steps") or []
                        summary = plan.get("summary") or "Plan created"
                        print(c(f"\n  plan: {summary} ({len(steps)} step{'s' if len(steps) != 1 else ''})", Colors.MUTED))
                    if response.get("approval"):
                        approval = response["approval"] or {}
                        print_warning(approval.get("summary") or "Approval required")
                        if response.get("task_id"):
                            print(c(f"  task: {response['task_id']}", Colors.DIM))
                    elif response.get("artifacts"):
                        artifact_count = len(response.get("artifacts") or [])
                        print(c(f"\n  artifacts: {artifact_count}", Colors.MUTED))
                    provider = response.get("provider") or "unknown"
                    model = response.get("model") or "unknown"
                    print(c(f"\n  {provider}:{model}\n", Colors.MUTED))
                continue
            async for event in client.start_task(user_input):
                print_event(event)
            print()

        except KeyboardInterrupt:
            print(c("\n\n  Goodbye! 🦅\n", Colors.KESTREL))
            break
        except EOFError:
            break
        except Exception as e:
            print_error(str(e))


# ── Main Entry Point ─────────────────────────────────────────────────

