# Kestrel → Agent OS: Research, Gap Analysis & System Design

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

---

## Agent Instructions

Ask the user questions when anything is unclear or needs their input. This includes:
- Ambiguous or incomplete requirements
- Technical decisions that affect architecture or user experience
- Trade-offs that require business context

Do not make assumptions on important decisions — get clarification first.

---

## Workflow Steps

### [x] Step: Research & System Design
<!-- chat-id: 0fcd8d13-8e68-4b80-b089-dc24b2472718 -->
- Research OpenClaw architecture in depth (heartbeat, memory, daemon, OS integration)
- Audit Kestrel's current codebase against OpenClaw's design principles
- Identify gaps between Kestrel and the "machine as sandbox / living OS" vision
- Produce full system design document
- Update `{@artifacts_path}/plan.md`.

---

## Research Output

### OpenClaw Core Architecture (source: clawdocs.org)

OpenClaw uses a **Gateway-centric, single-process architecture**:

| Component | Role |
|---|---|
| Gateway | Central process — WebSocket server, daemon, process manager |
| Brain | LLM reasoning (Anthropic, OpenAI, xAI, local models) |
| Hands | Shell, filesystem, browser automation |
| Memory | Persistent local Markdown files at `~/.openclaw/memory/` |
| Heartbeat | Autonomous task loop — fires every N minutes (default 30) |
| Channels | Messaging platform bridges (Telegram, WhatsApp, etc.) |
| Skills | YAML + Markdown skill definitions from ClawHub |

**Key design principles:**
1. **Single process** — No microservices. One gateway owns everything.
2. **Local-first** — All data stays on disk. No cloud required beyond LLM API.
3. **Transparent memory** — Memory IS human-readable Markdown files. Editable by hand.
4. **Heartbeat autonomy** — Defined in a plain `HEARTBEAT.md` file the user writes.
5. **OS daemon** — Installed via `launchd` (macOS) or `systemd` (Linux). Starts on boot.
6. **The machine is the workspace** — Direct host filesystem access, no containerization overhead.

---

## Gap Analysis: Kestrel vs. OpenClaw "Agent OS"

### GAP 1 — No OS-Level Daemon Integration (CRITICAL)
**What OpenClaw does:** Installs as a `launchd` agent on macOS / `systemd` service on Linux. Starts automatically on boot, runs forever in background.

**What Kestrel does:** Requires `docker compose up` to start. Is a collection of microservices (brain, gateway, hands, postgres, redis). Has no OS-level persistence mechanism.

**Impact:** Kestrel cannot be a "living, breathing OS for the agent" if it requires manual startup and cannot survive reboots. It is a tool you launch, not an agent that lives on the machine.

---

### GAP 2 — Wrong Sandbox Paradigm (CRITICAL)
**What OpenClaw does:** The local machine IS the workspace. Hands execute shell commands directly on the host. Security comes from workspace allowlists and user approval, not container isolation.

**What Kestrel does:** Routes all execution through Docker containers (`hands/executor.py`). This is the correct approach for cloud deployments, but fights against the "my computer as the sandbox" vision. Docker spin-up latency (~2s per container) kills the "living OS" feel.

**Impact:** The user wants to say "my machine is Kestrel's sandbox." Containerization is the opposite philosophy — it deliberately hides the machine from the agent.

---

### GAP 3 — No HEARTBEAT.md Equivalent (HIGH)
**What OpenClaw does:** A single Markdown file (`~/.openclaw/HEARTBEAT.md`) defines what the agent checks on every heartbeat cycle in plain English. User-editable, versioned, transparent.

**What Kestrel does:** Has `daemon.py`, `daemon_observers.py`, and `proactive.py` — but they require database records and code changes to configure. There is no human-friendly configuration file for heartbeat tasks. The daemon types are hardcoded enums.

**Impact:** The agent is not steerable by the user without writing code. OpenClaw's HEARTBEAT.md makes the agent's autonomous behavior explicit and controllable.

---

### GAP 4 — Markdown Memory is One-Directional (HIGH)
**What OpenClaw does:** Memory IS the Markdown files. The agent reads from and writes to `~/.openclaw/memory/*.md`. These files are the primary truth. Manual edits immediately affect the agent's behavior.

**What Kestrel does:** `LocalMarkdownMemoryManager` exists (`core/markdown_memory.py`) but `ingest_from_disk()` is an empty stub (`pass`). The Markdown layer is a secondary export of the PostgreSQL/ChromaDB truth. Bidirectional sync is unimplemented.

**Impact:** The agent's memory is opaque. Users cannot easily inspect or edit what the agent knows. The "transparent memory" principle is violated.

---

### GAP 5 — No File System Event Triggers (HIGH)
**What OpenClaw does:** The agent can observe and react to filesystem changes (new files, downloads, code changes) as OS-level triggers. The machine's file activity feeds into the agent's awareness.

**What Kestrel does:** No integration with macOS FSEvents, Linux inotify, or any filesystem watcher. The agent is blind to what is happening on the machine between conversations.

**Impact:** The "living OS" requires the agent to notice things happening autonomously — a new file downloaded, a test failing, a git push — without being explicitly told.

---

### GAP 6 — No Native OS Notification Integration (MEDIUM)
**What OpenClaw does:** Can surface notifications through native OS mechanisms (macOS Notification Center, system tray presence).

**What Kestrel does:** Notifications go through Telegram or the web UI. No integration with macOS native notification APIs or menu bar presence.

**Impact:** The agent feels remote and web-based rather than native to the machine.

---

### GAP 7 — No Quiet Hours / Resource Awareness (MEDIUM)
**What OpenClaw does:** Heartbeat respects `quiet_hours` (e.g., 23:00–07:00) and uses cheap models (Haiku) for background polling to keep costs low. Can detect if the machine is idle.

**What Kestrel does:** Heartbeat and daemon systems have no quiet hours configuration. No awareness of machine sleep state, user activity, or cost optimization for background tasks.

**Impact:** An always-on agent running expensive models in the background at 3am is not a "living OS," it's a runaway process.

---

### GAP 8 — Microservices Architecture Overhead (MEDIUM)
**What OpenClaw does:** Single process. Boots in <1 second. No external dependencies required for basic operation.

**What Kestrel does:** Requires PostgreSQL, Redis, 4+ Docker containers, and configured environment variables just to start. The system is heavy for a background agent.

**Impact:** A "living OS agent" should boot as fast as the OS itself. The microservices model is right for a team product but wrong for a personal machine agent.

---

### GAP 9 — No Agent "Home Directory" Concept (MEDIUM)
**What OpenClaw does:** All agent state lives in `~/.openclaw/` — config, memory, skills, heartbeat tasks, installed tools. One directory = the agent's entire world. Portable and inspectable.

**What Kestrel does:** State is split across PostgreSQL (tasks, memory, users), Redis (caching), ChromaDB (vectors), `/tmp` (proposals), Docker volumes (code execution). No single "home" for the agent.

**Impact:** You cannot back up Kestrel's state by copying a folder. There is no simple way to see "everything Kestrel knows and is doing."

---

### GAP 10 — Screen/Computer Use Not Wired Into Agent Loop (LOW)
**What OpenClaw does:** Screen and computer control is a first-class heartbeat skill — the agent can visually observe the desktop.

**What Kestrel does:** `screen_agent.py` and `computer_use.py` exist as tools but are not integrated into the proactive daemon loop. The agent doesn't "watch" the screen autonomously.

**Impact:** The agent cannot proactively notice what is on screen without being explicitly asked.

---

## System Design: Kestrel as a Living Agent OS

### Vision Statement
Transform Kestrel from a **request-response AI assistant** backed by cloud-style microservices into a **persistent autonomous agent process** that lives on the user's macOS machine, using the local filesystem as its primary state store and memory, and the host OS as its native execution environment.

---

### Design Pillars

| Pillar | Description |
|---|---|
| **Resident** | Runs as a launchd daemon, starts on boot, never needs to be manually launched |
| **Observant** | Monitors the machine (filesystem, processes, git, emails, system metrics) via daemons |
| **Transparent** | All state, memory, and configuration lives in `~/.kestrel/` as human-readable files |
| **Proactive** | Heartbeat loop fires on schedule, decides what to do without user prompting |
| **Native** | Surfaces through macOS Notification Center, menu bar icon, and voice wake word |
| **Safe** | Host access governed by per-action approval, allowlist, and audit log — not Docker |

---

### New Directory Structure: `~/.kestrel/`

```
~/.kestrel/
├── config.yml              # Master config (heartbeat interval, model routing, quiet hours)
├── HEARTBEAT.md            # User-defined heartbeat tasks (plain English)
├── WORKSPACE.md            # Active context: current projects, goals, focus areas
├── memory/
│   ├── preferences.md      # User communication style, tools, habits
│   ├── projects.md         # Active and recent projects with context
│   ├── learnings.md        # Discoveries, solutions, pitfalls
│   ├── people.md           # People and contacts the agent knows
│   ├── procedures.md       # Reusable workflows and macros (named sequences)
│   └── custom/             # User-created memory categories
├── tasks/
│   ├── active/             # Currently running task state (JSON)
│   └── history/            # Completed task records
├── skills/                 # User-installed or custom skills (YAML + Python)
├── watchlist/
│   ├── paths.yml           # Filesystem paths to watch for changes
│   ├── repos.yml           # Git repos to monitor
│   └── services.yml        # External services to poll
├── audit/
│   └── YYYY-MM-DD.log      # Daily append-only audit logs of agent actions
└── state/
    ├── daemon_states.json  # Current state of all running daemons
    └── heartbeat.json      # Last heartbeat timestamp and result
```

---

### Component Architecture

#### 1. Kestrel OS Daemon (New: `kestrel-daemon`)
The agent runs as a single persistent Python process registered with macOS `launchd`.

- **Entry point:** `packages/cli/kestrel_daemon.py`
- **Registered at:** `~/Library/LaunchAgents/ai.kestrel.daemon.plist`
- **Startup:** `launchctl load ~/Library/LaunchAgents/ai.kestrel.daemon.plist`
- **Responsibilities:** Owns the heartbeat scheduler, filesystem watchers, daemon agent runners, notification dispatch, and the async event loop. Bridges to the Brain service for LLM calls.

#### 2. HEARTBEAT.md — User-Defined Autonomy
Replace programmatic daemon configuration with a plain Markdown file at `~/.kestrel/HEARTBEAT.md`. The agent reads this on every heartbeat cycle and executes the tasks described.

**Format:**
```markdown
# Kestrel Heartbeat Tasks

## Every heartbeat (every 30 min)
- Check if any GitHub Actions in my repos have failed
- Monitor CPU/memory — alert if >85% for >5 min

## Hourly
- Summarize new emails from my boss or marked urgent
- Check git status of active projects for uncommitted changes

## Daily (9am)
- Weather briefing for my location
- Summarize today's calendar events
- Run self-improvement scan on the codebase

## Weekly (Monday 8am)
- Generate a summary of last week's git activity across all repos
- Clean up ~/Downloads of files older than 30 days
```

**Implementation:** `HeartbeatParser` reads this file, categorizes tasks by cadence, and schedules them. The Brain loop processes each task using its full Plan→Execute→Reflect capability with cheap model routing.

#### 3. Host Execution Layer (Replace Docker sandbox for local use)
For the "machine as sandbox" model, Docker isolation is replaced by a **host executor** with fine-grained OS-level controls:

- **Allowlist-governed shell execution** — Commands vetted against `~/.kestrel/config.yml` allowlist before execution
- **Approval gate** — Destructive or high-risk operations require macOS native approval dialog or Telegram confirmation
- **Audit trail** — Every command, its output, and approval decision appended to `~/.kestrel/audit/YYYY-MM-DD.log`
- **Resource limits** — CPU/memory throttling via macOS `nice`/`cpulimit` for background tasks

> **Note:** Docker-based execution is kept for tasks explicitly requesting isolation (e.g., "run this untrusted script"). The distinction is: **trusted host tasks → native host executor; untrusted/risky tasks → Docker sandbox.** This is a hybrid model.

#### 4. FSEvents Watcher (New: `KestrelFSWatcher`)
Uses macOS `FSEvents` (via `watchdog` Python library) or Linux `inotify` to create a live feed of filesystem changes into the agent's observation pipeline.

- Configured via `~/.kestrel/watchlist/paths.yml`
- Emits `FileChangeObservation` signals to the existing `ProactiveInterruptEngine`
- Agent decides whether the change warrants action (e.g., new file in Downloads → classify and tag; test file changed → trigger re-run)

#### 5. Bidirectional Markdown Memory Sync
Complete the implementation of `LocalMarkdownMemoryManager.ingest_from_disk()`:

- On heartbeat start, scan `~/.kestrel/memory/*.md` for changes (compare file hash)
- Parse changed sections and upsert new/modified entities into the memory graph
- On heartbeat end, sync updated memory graph back to Markdown
- Result: Users can edit `preferences.md` and the agent will know about it on the next cycle

#### 6. WORKSPACE.md — Active Context File
A single file at `~/.kestrel/WORKSPACE.md` that the user maintains (and the agent updates) to describe current focus:

```markdown
# Active Workspace Context

## Current Projects
- **Kestrel**: Implementing Agent OS features. Main repo at ~/code/kestrel.
- **Client work**: React dashboard at ~/code/acme-dashboard. Deadline: March 15.

## Current Goals
- Ship the launchd daemon integration this week
- Fix the memory sync bug in markdown_memory.py

## Do Not Disturb
- Don't interrupt me for informational items between 10am-1pm (deep work hours)
```

This file is injected into every agent loop context, replacing the need for the agent to rediscover the user's current state each time.

#### 7. macOS Native Notification Integration
Add a native notification layer using `pync` or the macOS `osascript` bridge:

- Low-priority findings → macOS banner notification (dismissible)
- High-priority alerts → Persistent macOS notification with action buttons ("Approve" / "Dismiss")
- Menu bar icon via `rumps` or `PyObjC` to show agent status (idle / thinking / acting) and quick access to recent activity

#### 8. Quiet Hours & Resource Awareness
Add to `~/.kestrel/config.yml`:

```yaml
heartbeat:
  interval: 1800          # 30 minutes
  quiet_hours:
    start: "23:00"
    end: "07:00"
    timezone: "America/Los_Angeles"
  deep_work_hours:         # No proactive interrupts, still runs tasks silently
    start: "10:00"
    end: "13:00"
  idle_detection: true     # Only run expensive tasks when machine is idle (no keyboard/mouse for >5 min)
  background_model: "claude-haiku-4-5"  # Cheap model for heartbeat polling
  reasoning_model: "claude-sonnet-4-5"  # Full model for complex tasks
```

#### 9. Model Routing Aware of Context
Extend `model_router.py` to understand heartbeat context vs. interactive context:

- **Heartbeat / background / daemon tasks** → fast cheap model (Haiku / Gemini Flash / local Ollama)
- **Interactive user conversation** → preferred full model
- **Council deliberation / planning** → flagship model
- **Verification gate** → small fast model with structured output

#### 10. Unified `kestrel status` HUD
A CLI command that shows the agent's current live state in the terminal, pulling from `~/.kestrel/state/`:

```
$ kestrel status

🦅 Kestrel Agent OS — Running (uptime: 3d 14h)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HEARTBEAT     Last: 4 min ago | Next: 26 min | Tasks: 3 completed, 0 failed
DAEMONS       repo_watcher (active) | system_monitor (active) | ci_monitor (active)
MEMORY        1,247 entities | Last sync: 2 min ago | Markdown: ✓ in sync
TASKS         0 active | 142 completed today
WATCHLIST     ~/code/kestrel (watching) | ~/code/acme-dashboard (watching)

Recent Activity:
  09:14 ✓ Checked GitHub Actions — all green
  09:14 ✓ Sent email summary (3 urgent emails from Sarah)
  08:00 ✓ Daily briefing — weather: sunny 68°F | 4 calendar events
```

---

### Implementation Phases

#### Phase 1 — The Resident (Foundation)
Priority: Make Kestrel a persistent OS-level process.

- [ ] Create `kestrel_daemon.py` entry point (pure async event loop, no web server required)
- [ ] Generate and install `~/Library/LaunchAgents/ai.kestrel.daemon.plist`
- [ ] `kestrel install` CLI command to register the daemon
- [ ] `kestrel status` CLI command reading from `~/.kestrel/state/`
- [ ] Migrate configuration from environment variables to `~/.kestrel/config.yml`

#### Phase 2 — The Observer (Filesystem & OS Awareness)
Priority: The agent sees what's happening on the machine.

- [ ] Implement `KestrelFSWatcher` using `watchdog` + `~/.kestrel/watchlist/paths.yml`
- [ ] Complete `ingest_from_disk()` in `markdown_memory.py` for bidirectional sync
- [ ] Add `WORKSPACE.md` injection into every agent loop context
- [ ] macOS native notifications via `pync` / `osascript`

#### Phase 3 — The Heartbeat (Autonomous Action)
Priority: User-defined proactive behavior in plain English.

- [ ] `HeartbeatParser` — reads and categorizes `HEARTBEAT.md`
- [ ] Wire `HeartbeatParser` output into the existing `proactive.py` scheduler
- [ ] Add quiet hours and idle detection to heartbeat scheduler
- [ ] Add per-task model routing (cheap model for heartbeat, full model for complex tasks)

#### Phase 4 — The Host (Native Execution)
Priority: Replace Docker-for-everything with a native host executor.

- [ ] `HostExecutor` — allowlist-governed shell/python execution directly on the host
- [ ] Hybrid dispatch: route to `HostExecutor` for trusted tasks, `SandboxExecutor` for untrusted
- [ ] Native approval dialog (macOS) for high-risk host commands
- [ ] Daily audit log appended to `~/.kestrel/audit/`

#### Phase 5 — The Memory (Transparent Knowledge)
Priority: Make the agent's knowledge inspectable and editable.

- [ ] Full bidirectional sync: Markdown → Memory Graph on heartbeat start
- [ ] Categorize memory into themed files (`preferences.md`, `projects.md`, etc.)
- [ ] `kestrel memory show [category]` CLI command
- [ ] `kestrel memory edit` opens `~/.kestrel/memory/` in default editor

#### Phase 6 — The Presence (Native OS Integration)
Priority: The agent feels native, not remote.

- [ ] Menu bar icon (status indicator + quick actions) via `rumps`
- [ ] Voice wake word re-integration ("Hey Kestrel") wired to daemon
- [ ] `kestrel status` rich terminal HUD
- [ ] macOS Spotlight plugin for searching agent memory

---

### What Kestrel Already Has (Strengths to Preserve)

| Capability | Status |
|---|---|
| Multi-agent council system | ✅ Fully implemented |
| Evidence-bound verification | ✅ Fully implemented |
| DAG-based parallel step scheduler | ✅ Fully implemented |
| Semantic memory graph with temporal decay | ✅ Fully implemented |
| MCP auto-expansion (tool marketplace) | ✅ Fully implemented |
| Macro / workflow reuse system | ✅ Fully implemented |
| Model router (cost-aware dispatch) | ✅ Fully implemented |
| Daemon agent framework (daemon.py) | ✅ Architecture exists, needs HEARTBEAT.md config |
| Proactive interrupt engine | ✅ Architecture exists, needs FSEvents input |
| State machine enforcement | ✅ Fully implemented |
| Markdown memory manager | ⚠️ One-directional — needs ingest_from_disk() |
| Screen agent / computer use | ⚠️ Exists as tools, not wired into daemon loop |
| OS daemon (launchd) | ❌ Not implemented |
| HEARTBEAT.md config | ❌ Not implemented |
| FSEvents / filesystem watcher | ❌ Not implemented |
| Host-native executor | ❌ Not implemented (Docker-only today) |
| `~/.kestrel/` home directory | ❌ Not implemented |
| macOS native notifications | ❌ Not implemented |
| Quiet hours / idle detection | ❌ Not implemented |
| WORKSPACE.md context file | ❌ Not implemented |

---

### Key Architectural Principle

> OpenClaw chose simplicity (single process, Markdown files, no database) to maximize transparency and portability.
> Kestrel's existing sophistication (council, DAG scheduler, evidence chains, MCP marketplace) is **more powerful** than OpenClaw.
> The goal is NOT to replace Kestrel's architecture with OpenClaw's — it is to **add the "living OS" layer ON TOP** of Kestrel's intelligence.
>
> The microservices (brain, gateway) continue to exist for web/team use.
> The new `kestrel-daemon` is a **lightweight sidecar** that connects to brain via gRPC and provides the OS-native experience: launchd registration, FSEvents, HEARTBEAT.md, native notifications, `~/.kestrel/` home directory.

---
