<p align="center">
  <h1 align="center">Kestrel</h1>
  <p align="center"><strong>Autonomous AI agent platform that thinks, plans, and acts — on your own infrastructure.</strong></p>
  <p align="center">
    <a href="#quick-start">Quick Start</a> •
    <a href="#the-kestrel-agent">Agent Engine</a> •
    <a href="#features">Features</a> •
    <a href="#architecture">Architecture</a> •
    <a href="#tools--skills">Tools</a> •
    <a href="#development">Development</a>
  </p>
</p>

---

Kestrel is a privacy-first, self-hostable AI platform that goes far beyond simple chat. At its core is an **autonomous agent engine** that plans, reasons, self-reflects, coordinates multi-agent debates, and executes real-world actions through 30+ sandboxed tools — all while showing you exactly how it thinks.

> **Private by default** — runs entirely on your infrastructure. Your data never leaves your machines. Supports local models via MLX (Apple Silicon) or any cloud LLM (Gemini, OpenAI, Anthropic).

---

## ✨ Features

| Category                     | Highlights                                                                                     |
| ---------------------------- | ---------------------------------------------------------------------------------------------- |
| 🧠 **Autonomous Agent**      | Plan → Execute → Reflect loop with multi-step reasoning and self-correction                    |
| 🤔 **Multi-Agent Debates**   | Council of specialists (Architect, Security, Implementer, Devil's Advocate) vote on decisions  |
| 👁️ **Transparent Thinking**  | KestrelProcessBar shows every step: memory recall → planning → tool use → council → confidence |
| 🔧 **30+ Built-in Skills**   | Web search, code execution, file management, GitHub, email, home automation, and more          |
| 🧩 **Custom Skills**         | Create workspace-specific tools that Kestrel loads dynamically                                 |
| 💾 **Persistent Memory**     | Knowledge graph + vector memory that persists across restarts and conversations                |
| 🔒 **Sandboxed Execution**   | All tool execution runs in isolated Docker containers with resource limits                     |
| 📱 **Multi-Channel**         | Web, Telegram, Discord, WhatsApp — same agent, different interfaces                            |
| 🗣️ **Voice Input**           | Wake word detection ("Hey Libre") with real-time audio streaming                               |
| 🖥️ **Native macOS App**      | Run as a native `.app` bundle via pywebview                                                    |
| 📊 **Guardrails & Auditing** | Token budgets, wall-time limits, evidence chains, and full audit trails                        |

---

## The Kestrel Agent

Kestrel isn't a chatbot wrapper — it's a full autonomous agent engine.

### How It Thinks

```
User Message
    │
    ▼
┌──────────────────────────────────────────────────────┐
│  Phase 0: Context Gathering                          │
│  • Query Memory Graph for relevant past knowledge    │
│  • Load lessons from previous tasks                  │
│  • Activate workspace-specific skills                │
│  • Inject persona & conversation history             │
└──────────────────────┬───────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────┐
│  Phase 1: Planning                                   │
│  • Analyze goal and available tools                  │
│  • Generate multi-step execution plan                │
│  • Record plan decision in evidence chain            │
└──────────────────────┬───────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────┐
│  Phase 2: Execution Loop                             │
│  • Execute plan steps with tool calls                │
│  • Request human approval for risky actions           │
│  • Checkpoint progress for recovery                  │
│  • Coordinate sub-agents via Coordinator             │
└──────────────────────┬───────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────┐
│  Phase 3: Reflection & Learning                      │
│  • Self-critique via ReflectionEngine                │
│  • Council debate for complex decisions              │
│  • Extract lessons for future tasks                  │
│  • Persist evidence chain for auditability           │
└──────────────────────────────────────────────────────┘
```

### Real-Time Visibility

Every step of Kestrel's thinking process is streamed to the UI as a compact **KestrelProcessBar**:

```
🧠 3 recalled → 📖 2 lessons → 📋 4 steps → ⚡ web_search → 🤔 consensus → 🎯 92% → 💰 1.2k
```

Each phase is a clickable pill that expands to show full details — council votes, plan steps, evidence decisions, token costs, and more. No black boxes.

### Multi-Agent Council

For complex or risky decisions, Kestrel convenes a **Council** of specialists:

| Role                | Perspective                                  |
| ------------------- | -------------------------------------------- |
| 🏗️ Architect        | System design, scalability, maintainability  |
| ⚙️ Implementer      | Practical feasibility, effort, edge cases    |
| 🔒 Security         | Vulnerabilities, data safety, access control |
| 😈 Devil's Advocate | Challenges assumptions, finds weaknesses     |
| 👤 User Advocate    | User experience, clarity, communication      |

Members vote independently, debate each other's positions, and reach a consensus (or flag disagreement). If unsure, Kestrel escalates to the user rather than guessing.

---

## Architecture

```
┌─────────────────┐     WebSocket / REST
│    Clients      │◄────────────────────────►┌──────────────────┐
│  (Web, iOS,     │                          │     Gateway      │
│   Telegram,     │                          │   (Node.js)      │
│   Discord)      │                          └────────┬─────────┘
└─────────────────┘                                   │ gRPC
                                         ┌────────────┼────────────┐
                                         ▼                         ▼
                                  ┌──────────────┐         ┌──────────────┐
                                  │    Brain     │         │    Hands     │
                                  │  (Python)    │         │  (Python)    │
                                  └──────┬───────┘         └──────┬───────┘
                                         │                        │
                               ┌─────────┼─────────┐      ┌──────┴───────┐
                               ▼         ▼         ▼      ▼              ▼
                         ┌──────┐  ┌──────────┐ ┌─────┐ ┌────────┐ ┌────────┐
                         │Postgr│  │   LLM    │ │Redis│ │ Docker │ │ Skills │
                         │ SQL  │  │Providers │ │     │ │Sandbox │ │        │
                         └──────┘  └──────────┘ └─────┘ └────────┘ └────────┘
```

### Services

| Service      | Language             | Port         | Responsibility                                                                    |
| ------------ | -------------------- | ------------ | --------------------------------------------------------------------------------- |
| **Gateway**  | Node.js / TypeScript | 8741         | Authentication (JWT), WebSocket sessions, multi-channel adapters, request routing |
| **Brain**    | Python               | 50051 (gRPC) | Agent loop, LLM orchestration, memory graph, task planning, reflection, council   |
| **Hands**    | Python               | 50052 (gRPC) | Sandboxed tool/skill execution in Docker containers                               |
| **Frontend** | React / Vite         | 5173         | Aurora design system, real-time chat UI, KestrelProcessBar                        |

### Infrastructure

| Component                    | Purpose                                               |
| ---------------------------- | ----------------------------------------------------- |
| **PostgreSQL 16** (pgvector) | Persistent storage, vector search, Row-Level Security |
| **Redis 7**                  | Session management, caching, pub/sub                  |
| **Docker**                   | Sandboxed execution environment for skills            |

### Brain Subsystems

The Brain service initializes a deep stack of subsystems at startup:

| Module            | File               | Purpose                                                     |
| ----------------- | ------------------ | ----------------------------------------------------------- |
| Agent Loop        | `loop.py`          | Plan → Execute → Reflect cycle with budgets and checkpoints |
| Task Planner      | `planner.py`       | LLM-powered multi-step plan generation                      |
| Council           | `council.py`       | Multi-agent debate with role-based voting                   |
| Coordinator       | `coordinator.py`   | Sub-agent delegation and progress tracking                  |
| Reflection Engine | `reflection.py`    | Self-critique with severity-graded feedback                 |
| Memory Graph      | `memory_graph.py`  | Persistent knowledge graph for entities and relationships   |
| Evidence Chain    | `evidence.py`      | Auditable decision trail with citations                     |
| Persona Learner   | `persona.py`       | Learns user preferences over time                           |
| Task Learner      | `learner.py`       | Extracts lessons from completed tasks                       |
| Guardrails        | `guardrails.py`    | Token budgets, iteration limits, wall-time caps             |
| Checkpoints       | `checkpoints.py`   | Task state snapshots for crash recovery                     |
| Command Parser    | `commands.py`      | Slash commands (`/status`, `/model`) without LLM calls      |
| Skill Manager     | `skills.py`        | Dynamic workspace-specific tool loading                     |
| Workflow Registry | `workflows.py`     | Built-in task templates                                     |
| Automation        | `automation.py`    | Cron scheduling and webhook handlers                        |
| Predictions       | `predictions.py`   | Proactive intent prediction                                 |
| Observability     | `observability.py` | Metrics, tracing, and performance monitoring                |

---

## Tools & Skills

### 30 Built-in Skills

Kestrel ships with an extensive toolkit, each running in a sandboxed Docker container:

| Category              | Skills                                                                       | Description                                                  |
| --------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------ |
| 🌐 **Web**            | `web`, `browser_automation`                                                  | Search (DuckDuckGo), page fetching, headless browser control |
| 💻 **Code Execution** | `python_executor`, `node_executor`, `shell_executor`                         | Run Python, Node.js, and bash in sandboxes                   |
| 📁 **Files**          | `documents`, `notes`                                                         | Read, write, search filesystem; note management              |
| 🔍 **Knowledge**      | `knowledge`, `wikipedia`                                                     | RAG via ChromaDB, Wikipedia/Wolfram Alpha                    |
| 🐙 **Dev Tools**      | `github`                                                                     | Repos, issues, PRs, code review                              |
| 📧 **Communication**  | `email`, `contacts`                                                          | Email send/receive, contact management                       |
| 📅 **Productivity**   | `calendar`, `scheduler`, `focus_timer`, `meeting_summarizer`, `productivity` | Calendar events, cron jobs, pomodoro timer, meeting notes    |
| 🎨 **Media**          | `media`                                                                      | Image generation via MLX Stable Diffusion                    |
| 🌍 **Translation**    | `translate`, `text_transform`                                                | Multi-language translation, text manipulation                |
| 📡 **Integration**    | `api_caller`, `serial_usb`, `ssh_ftp`, `home_automation`, `digest`           | REST APIs, serial/USB, SSH/FTP, smart home, RSS feeds        |
| 🖥️ **System**         | `system_monitor`, `screen`, `computer_use`, `core`                           | CPU/memory stats, screenshots, full computer control         |

### Built-in Agent Tools

Beyond sandboxed skills, Kestrel has direct-access agent tools:

| Tool                         | Purpose                           |
| ---------------------------- | --------------------------------- |
| `read_web`                   | Parse web content via trafilatura |
| `execute_python`             | Run Python code directly          |
| `execute_bash`               | Run shell commands                |
| `read_file` / `search_files` | Filesystem access                 |
| `search_knowledge`           | Semantic search via ChromaDB      |
| `generate_image`             | Local image gen via mflux         |
| `text_to_speech`             | TTS output                        |
| `remember` / `recall`        | Memory graph read/write           |
| `ask_human`                  | Request user input when stuck     |
| `create_schedule`            | Set up cron-based automated tasks |
| `moltbook`                   | Log structured activity entries   |

---

## Quick Start

### Prerequisites

- Docker & Docker Compose
- At least one LLM API key (Google, OpenAI, or Anthropic) — or a local model

### One-Command Deploy

```bash
# Clone
git clone https://github.com/John-MiracleWorker/Kestrel.git
cd Kestrel

# Configure
cp .env.example .env
# Edit .env — set your API keys and passwords

# Launch everything
docker compose up -d
```

Then open **http://localhost:5173** — that's it.

### Environment Variables

Key variables in `.env`:

| Variable               | Default                | Description                                 |
| ---------------------- | ---------------------- | ------------------------------------------- |
| `GOOGLE_API_KEY`       | —                      | Gemini API key                              |
| `OPENAI_API_KEY`       | —                      | OpenAI API key                              |
| `ANTHROPIC_API_KEY`    | —                      | Anthropic API key                           |
| `DEFAULT_LLM_PROVIDER` | `local`                | `google`, `openai`, `anthropic`, or `local` |
| `JWT_SECRET`           | `dev-secret-change-me` | Auth secret (change in production!)         |
| `POSTGRES_PASSWORD`    | `changeme`             | Database password                           |

---

## Development

### Run Services Individually

```bash
# Gateway (Node.js)
cd packages/gateway && npm run dev

# Brain (Python gRPC)
cd packages/brain && python server.py

# Hands (Python gRPC)
cd packages/hands && python server.py

# Frontend (Vite dev server)
cd packages/web && npm run dev
```

### Run Tests

```bash
npm test                      # Gateway tests
cd packages/brain && pytest   # Brain tests
cd packages/hands && pytest   # Hands tests
```

### Project Structure

```
kestrel/
├── packages/
│   ├── brain/               # Python AI service (agent engine)
│   │   ├── server.py              # gRPC server, 11+ RPCs
│   │   ├── agent/                 # Full agent stack (24 modules)
│   │   │   ├── loop.py            # Core plan-execute-reflect loop
│   │   │   ├── council.py         # Multi-agent debate system
│   │   │   ├── coordinator.py     # Sub-agent delegation
│   │   │   ├── reflection.py      # Self-critique engine
│   │   │   ├── memory_graph.py    # Persistent knowledge graph
│   │   │   ├── evidence.py        # Auditable decision chain
│   │   │   ├── planner.py         # LLM-powered task planning
│   │   │   ├── guardrails.py      # Budget & safety limits
│   │   │   └── ...                # 16 more modules
│   │   ├── providers/             # LLM adapters (local, cloud)
│   │   └── migrations/            # PostgreSQL schema + RLS
│   ├── gateway/             # Node.js API gateway
│   │   ├── src/server.ts          # Fastify + WebSocket
│   │   ├── src/channels/          # Web, Telegram, Discord, WhatsApp
│   │   └── src/brain/             # gRPC client
│   ├── hands/               # Python tool execution service
│   │   ├── executor.py            # Docker sandbox runner
│   │   └── security/              # Allowlist & audit
│   ├── web/                 # React frontend (Vite)
│   │   └── src/components/Chat/   # ChatView + KestrelProcessBar
│   └── shared/proto/        # Protobuf service contracts
├── skills/                  # 30 built-in sandboxed skills
├── docker-compose.yml       # Full stack orchestration
└── .env.example             # Configuration template
```

---

## Security

| Layer                   | Implementation                                               |
| ----------------------- | ------------------------------------------------------------ |
| **Authentication**      | JWT tokens with configurable expiry                          |
| **Data Isolation**      | Row-Level Security (RLS) on PostgreSQL per workspace         |
| **Sandboxed Execution** | All skills run in Docker containers with CPU/memory limits   |
| **Module Allowlisting** | Only approved Python modules available in sandboxes          |
| **Audit Logging**       | Every tool execution logged with full context                |
| **Risk-Based Approval** | High-risk actions require explicit user confirmation         |
| **Guardrails**          | Token budgets, iteration caps, and wall-time limits per task |
| **Evidence Chain**      | Cryptographically auditable decision trail                   |

---

## Channels

Kestrel is accessible from multiple interfaces, all routing through the same agent:

| Channel         | Status    | Protocol                    |
| --------------- | --------- | --------------------------- |
| 🌐 Web UI       | ✅ Active | WebSocket                   |
| 📱 Telegram     | ✅ Active | Bot API                     |
| 💬 Discord      | ✅ Active | Bot API                     |
| 📲 WhatsApp     | ✅ Active | Cloud API                   |
| 🖥️ macOS Native | ✅ Active | pywebview                   |
| 🗣️ Voice        | ✅ Active | Wake word + audio streaming |

---

## LLM Providers

| Provider    | Models                                         | Status |
| ----------- | ---------------------------------------------- | ------ |
| Google      | Gemini 3 Flash, Gemini 3 Pro, Gemini 2.5 Flash | ✅     |
| OpenAI      | GPT-5 series                                   | ✅     |
| Anthropic   | Claude Sonnet 4.5, Haiku 4.5                   | ✅     |
| Local (MLX) | Any GGUF model via llama.cpp                   | ✅     |

Switch providers on the fly with `/model google` or via the UI settings.

---

## License

Private — all rights reserved.
