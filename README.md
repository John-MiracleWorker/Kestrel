# Kestrel

**Privacy-first, self-hostable AI agent platform.**

Kestrel is a distributed microservices platform that provides a personal AI assistant with tool execution, multi-channel support, and long-term memory — all running on your own infrastructure.

---

## Architecture

```
┌─────────────┐     WebSocket / REST
│   Clients   │◄──────────────────────►┌──────────────┐
│  (Web, iOS, │                        │   Gateway    │
│   Android)  │                        │  (Node.js)   │
└─────────────┘                        └──────┬───────┘
                                              │ gRPC
                              ┌───────────────┼───────────────┐
                              ▼                               ▼
                       ┌──────────────┐              ┌──────────────┐
                       │    Brain     │              │    Hands     │
                       │  (Python)    │              │  (Python)    │
                       └──────┬───────┘              └──────┬───────┘
                              │                             │
                    ┌─────────┼─────────┐           ┌───────┴───────┐
                    ▼         ▼         ▼           ▼               ▼
              ┌──────┐  ┌──────────┐ ┌─────┐  ┌──────────┐   ┌──────────┐
              │Postgr│  │ LLM      │ │Redis│  │ Docker   │   │  Skills  │
              │  SQL  │  │Providers │ │     │  │ Sandbox  │   │          │
              └──────┘  └──────────┘ └─────┘  └──────────┘   └──────────┘
```

### Services

| Service | Language | Port | Role |
|---------|----------|------|------|
| **Gateway** | Node.js / TypeScript | 3000 | Authentication, WebSocket sessions, request routing |
| **Brain** | Python | 50051 (gRPC) | LLM orchestration, conversation management, vector memory |
| **Hands** | Python | 50052 (gRPC) | Sandboxed tool/skill execution via Docker containers |

### Infrastructure

| Component | Purpose |
|-----------|---------|
| **PostgreSQL** (pgvector) | Persistent storage with vector search for semantic memory |
| **Redis** | Session management, caching, pub/sub |
| **Docker** | Sandboxed execution environment for skills |

---

## Quick Start

### Prerequisites

- Node.js ≥ 18
- Python ≥ 3.10
- Docker & Docker Compose
- PostgreSQL 16+ with pgvector extension

### Setup

```bash
# Clone the repository
git clone https://github.com/John-MiracleWorker/Kestrel.git
cd Kestrel

# Copy environment config
cp .env.example .env
# Edit .env with your settings (API keys, DB passwords, etc.)

# Install Node.js dependencies
npm install

# Install Python dependencies
cd packages/brain && pip install -r requirements.txt && cd ../..
cd packages/hands && pip install -r requirements.txt && cd ../..

# Start all services
docker compose up -d
```

### Development

```bash
# Run Gateway in dev mode
cd packages/gateway && npm run dev

# Run Brain service
cd packages/brain && python server.py

# Run Hands service
cd packages/hands && python server.py

# Run tests
npm test                    # Gateway tests
cd packages/brain && pytest # Brain tests
cd packages/hands && pytest # Hands tests
```

---

## Project Structure

```
kestrel/
├── packages/
│   ├── gateway/          # Node.js API gateway
│   │   ├── src/
│   │   │   ├── server.ts       # Fastify server, auth, routes
│   │   │   ├── brain/          # gRPC client to Brain service
│   │   │   ├── channels/       # WebSocket & channel adapters
│   │   │   └── utils/          # Logger, metrics, session mgmt
│   │   └── tests/              # Jest unit tests
│   ├── brain/            # Python AI service
│   │   ├── server.py           # gRPC server (11 RPCs)
│   │   ├── providers/          # LLM provider adapters (local, cloud)
│   │   ├── memory/             # Vector store for semantic memory
│   │   ├── migrations/         # PostgreSQL schema + RLS policies
│   │   └── tests/              # Pytest unit tests
│   ├── hands/            # Python tool execution service
│   │   ├── server.py           # gRPC server (3 RPCs)
│   │   ├── executor.py         # Docker sandbox executor
│   │   ├── security/           # Allowlist & audit logging
│   │   └── tests/              # Pytest unit tests
│   └── shared/           # Shared definitions
│       └── proto/              # Protobuf service contracts
│           ├── brain.proto
│           └── hands.proto
├── skills/               # Built-in skills (web, github, translate, etc.)
├── docker-compose.yml    # Full stack orchestration
├── .env.example          # Environment variable template
└── README.md
```

---

## Security

- **JWT authentication** with configurable expiry
- **Row-Level Security (RLS)** on PostgreSQL for workspace data isolation
- **Sandboxed execution** — all skills run in Docker containers with resource limits
- **Module allowlisting** — only approved Python modules available in sandboxes
- **Audit logging** — all tool executions are logged with full context

---

## Skills

Built-in skills include:

| Skill | Description |
|-------|-------------|
| `web` | Web search (DuckDuckGo) and page fetching |
| `github` | GitHub API integration (repos, issues, PRs) |
| `translate` | Language translation via MyMemory API |
| `wikipedia` | Wikipedia & Wolfram Alpha lookups |
| `digest` | RSS/Atom feed aggregation |
| `api_caller` | Generic REST API client |
| `core` | Weather, file management, math utilities |

---

## License

Private — all rights reserved.
