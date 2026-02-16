# Product Requirements Document: Libre Bird → OpenClaw-like Platform

## Executive Summary

Transform **Libre Bird** from a personal macOS AI assistant into a **full-blown multi-platform, multi-channel AI agent platform** similar to OpenClaw. The goal is to maintain Libre Bird's privacy-first philosophy while adding enterprise-grade architecture, multi-user support, channel integrations, mobile apps, and a thriving plugin ecosystem.

## Current State Analysis

### Libre Bird (Current)
- **Architecture**: Monolithic FastAPI backend + Vite frontend
- **Deployment**: Single-user macOS desktop app (pywebview)
- **LLM**: Local-only (llama-cpp-python with GGUF models)
- **Skills**: 26 modular skills with 101 tools
- **Storage**: SQLite with FTS5 full-text search
- **Interfaces**: Desktop GUI, REST API
- **Context**: macOS screen context tracking, voice input, TTS
- **Agent Modes**: 6 personas (general, coder, researcher, creative, sysadmin, productivity)

### Target State: OpenClaw-like Platform

**Key architectural patterns from OpenClaw:**
- **Gateway**: Long-lived Node.js process managing connections, sessions, and routing
- **Brain**: LLM-powered reasoning engine (multi-provider support)
- **Hands**: Sandboxed execution environments for system actions
- **Multi-channel**: WhatsApp, Telegram, Discord, SMS, web, mobile
- **Multi-user**: Authentication, user isolation, team workspaces
- **Cloud-native**: Docker deployment, horizontal scaling, cloud storage
- **Marketplace**: Community plugin ecosystem

---

## Product Vision

**"The privacy-first, self-hostable AI agent platform that works everywhere you do."**

Libre Bird will become:
1. **Platform-agnostic**: Run on macOS, Linux, Windows, cloud, containers
2. **Channel-agnostic**: Chat via web, mobile, WhatsApp, Telegram, Discord, Slack, SMS
3. **Multi-user**: Teams, workspaces, role-based access control
4. **Developer-friendly**: Plugin SDK, marketplace, webhooks, extensive APIs
5. **Cloud-optional**: Self-host or use managed cloud with end-to-end encryption
6. **Model-agnostic**: Local GGUF models + cloud providers (OpenAI, Anthropic, Google)

---

## Core Requirements

### 1. Architecture Transformation

#### 1.1 Gateway Service (Node.js/TypeScript)
**Purpose**: Long-lived coordination hub managing all connections and routing

**Requirements**:
- WebSocket server for real-time bidirectional communication
- Multi-channel adapter system (WhatsApp, Telegram, Discord, etc.)
- Session management with persistent state
- Message queue (Redis/RabbitMQ) for async processing
- Rate limiting, authentication, and security enforcement
- Health checks and metrics endpoints
- Graceful degradation when Brain or Hands are unavailable

**Technical Stack**:
- TypeScript/Node.js with Express or Fastify
- WebSocket (ws library) + Socket.io for fallback
- Redis for session storage and pub/sub
- Bull or BullMQ for job queues
- Passport.js for multi-strategy authentication
- Prometheus metrics

#### 1.2 Brain Service (Python)
**Purpose**: LLM-powered reasoning, planning, and decision-making

**Requirements**:
- Keep existing `llm_engine.py` architecture with enhancements:
  - Multi-provider support (local + OpenAI + Anthropic + Google + Azure)
  - Streaming responses via WebSocket
  - Tool/function calling with better error handling
  - Memory management with vector embeddings (pgvector or ChromaDB)
  - Agent modes with dynamic prompt construction
  - Chain-of-thought and planning capabilities
- gRPC or HTTP/2 API for low-latency Gateway ↔ Brain communication
- Horizontal scaling: multiple Brain workers behind load balancer
- Model hot-swapping without downtime

**Technical Stack**:
- Keep Python (FastAPI or gRPC)
- LangChain or LlamaIndex for orchestration
- Sentence transformers for embeddings
- PostgreSQL with pgvector OR ChromaDB/Qdrant for vector storage

#### 1.3 Hands Service (Python/Bash/Docker)
**Purpose**: Isolated execution environment for system-level actions

**Requirements**:
- **Sandboxed execution**: Docker containers or gVisor for security
- **Capabilities**:
  - File operations (read/write/search with permission checks)
  - Shell command execution (with allowlist/denylist)
  - Web automation (Playwright/Selenium)
  - API calls (HTTP client with auth)
  - Code execution (Python, Node, bash in isolated env)
  - Screen capture and OCR (platform-specific)
  - System monitoring (CPU, memory, disk, processes)
- **Security**:
  - Resource limits (CPU, memory, disk, network)
  - Timeout enforcement
  - Audit logging of all actions
  - User consent for destructive operations
- gRPC interface for Brain → Hands communication
- Multiple Hands instances for parallel execution

**Technical Stack**:
- Docker or gVisor for sandboxing
- gRPC Python service
- Existing tool implementations from `skills/`

---

### 2. Multi-Channel Support

#### 2.1 Channel Adapters (Simplified for v1)
Implement bidirectional adapters in Gateway for:

| Channel | Library/API | Priority | Features |
|---------|-------------|----------|----------|
| **Web** | WebSocket | P0 | Real-time chat, file upload, rich UI |
| **Telegram** | python-telegram-bot | P0 | **Easiest integration**, inline buttons, file sharing |
| **Android App** | Native client + API | P1 | Push notifications, voice, camera |
| **Discord** | discord.js | P2 | Slash commands, embeds (Phase 2) |
| **Slack** | @slack/bolt | P2 | App mentions, modals (Phase 2) |

**Phase 1 Focus**: Web + Telegram
**Phase 2**: Add Discord, Slack, WhatsApp based on community demand

**Unified Message Format**:
```typescript
interface Message {
  id: string;
  channel: string;  // 'web' | 'whatsapp' | 'telegram' | ...
  userId: string;
  conversationId: string;
  content: string;
  attachments?: Attachment[];
  timestamp: Date;
  metadata: Record<string, any>;
}
```

#### 2.2 Channel-Specific Features
- **Rich UI** (web/mobile): Markdown, code blocks, tool progress indicators
- **Voice** (WhatsApp/Telegram): Voice message → Whisper STT
- **Media** (all): Image analysis, file processing
- **Buttons/Actions** (Telegram/Discord): Quick replies, confirmations

---

### 3. Multi-User System

#### 3.1 Authentication & Authorization (Simplified for v1)
**Requirements**:
- **Primary user**: Owner account (email/password, bcrypt hashing)
- **Guest users**: Optional read-only access with API keys
- JWT tokens for stateless authentication
- Simple role system:
  - `owner`: Full admin access
  - `guest`: Read-only, can send messages but limited tool access

**Database Schema** (SQLite):
```sql
CREATE TABLE users (
  id TEXT PRIMARY KEY,  -- UUID
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT,
  display_name TEXT,
  role TEXT NOT NULL DEFAULT 'owner',  -- 'owner' or 'guest'
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE api_keys (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  key_hash TEXT UNIQUE NOT NULL,
  name TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
```

**Note**: Multi-tenancy (workspaces) is deferred to Phase 2. v1 = single-instance, single-owner model.

#### 3.2 User Isolation (Simplified)
- **Conversations**: Per-user isolation
- **Context/Memory**: Per-user, guest users have no access to owner's context
- **Skills**: Owner configures globally, guests inherit permissions
- **Files**: Per-user directories with filesystem isolation
- **API quotas**: Optional rate limiting per user

---

### 4. Data Storage & Memory

#### 4.1 Database Architecture (SQLite Primary)
**Why SQLite**: Zero-config, single-file, perfect for self-hosted single-user

**Design**:
- SQLite as default and primary database
- Database adapter interface to support PostgreSQL later
- Schema updates:
  - Add `user_id` to conversations, messages, tasks, context tables
  - Keep existing tables, add new ones for users and API keys
  - Audit logs optional (lightweight version for file operations)

**PostgreSQL Support** (Phase 2):
- For users who need horizontal scaling
- Same schema via adapter pattern
- Migration tool: `libre-bird migrate sqlite-to-postgres`

#### 4.2 Vector Memory (RAG) with ChromaDB
**Requirements**:
- Embed conversations, context, and documents using sentence-transformers
- Store in **ChromaDB** (self-hosted, Python-native)
- Semantic search for "recall" queries
- Time-weighted retrieval (recent context + relevant historical)
- Privacy: User vectors isolated, stored locally

**Architecture**:
```python
# ChromaDB collection structure
collection = chroma_client.create_collection(
    name="memories",
    metadata={"user_id": "owner"},  # Per-user collections
)

collection.add(
    documents=["User was working on FastAPI project..."],
    metadatas=[{
        "timestamp": "2024-01-15T10:30:00",
        "source": "context",
        "user_id": "owner"
    }],
    ids=["memory_uuid_123"]
)
```

**Benefits**:
- No separate database server needed
- Persistent storage in `./chroma_db/` directory
- Easy backup (just copy the directory)

#### 4.3 File Storage
- **Local**: Keep filesystem for self-hosted
- **Cloud**: S3-compatible storage (AWS S3, MinIO, Backblaze)
- **Attachments**: User uploads, OCR results, generated files
- **Encryption**: At-rest encryption for cloud storage

---

### 5. Mobile Applications

#### 5.1 Android App (Kotlin/Jetpack Compose) - Phase 1 Priority
**Features**:
- Material Design 3 UI
- Push notifications (FCM)
- Voice input (Android Speech API)
- Camera and gallery access
- Quick tiles/widgets
- Biometric auth
- Offline mode with sync

**Technical Stack**:
- Jetpack Compose + Kotlin Coroutines
- Retrofit + OkHttp for networking
- Room for local database
- WorkManager for background sync
- Firebase Cloud Messaging for push notifications (self-hosted alternative: UnifiedPush)

#### 5.2 iOS App (Swift/SwiftUI) - Phase 2
**Deferred to Phase 2** based on Android-first priority. Will follow same architecture as Android app.

**Planned Features**:
- Native SwiftUI interface
- Push notifications (APNs)
- Voice input, Face ID/Touch ID
- Siri shortcuts

#### 5.3 Shared API Client
- Unified TypeScript/Kotlin/Swift SDK
- WebSocket + REST fallback
- Auto-reconnect with exponential backoff
- Message queue for offline mode
- End-to-end encryption option

---

### 6. Plugin Ecosystem & Marketplace

#### 6.1 Plugin SDK
**Goals**:
- Make skill development accessible to non-core contributors
- Provide templates, CLI tools, and thorough docs
- Sandboxed execution to prevent malicious plugins

**Developer Experience**:
```bash
# CLI for scaffolding
$ libre-bird create-skill my-custom-skill
# Generates:
# - skill.json (manifest)
# - __init__.py (Python) or index.ts (TypeScript)
# - tests/
# - README.md

# Local testing
$ libre-bird test-skill ./my-custom-skill

# Publish to marketplace
$ libre-bird publish-skill ./my-custom-skill
```

**Skill Manifest** (enhanced):
```json
{
  "name": "github_advanced",
  "display_name": "GitHub Advanced",
  "version": "2.0.0",
  "author": "community",
  "description": "Advanced GitHub operations: PRs, issues, CI/CD",
  "icon": "🐙",
  "category": "developer_tools",
  "runtime": "python",  // or "typescript", "docker"
  "dependencies": ["requests", "PyGithub"],
  "permissions": ["network", "env_vars"],
  "tools": [...],
  "settings": {
    "github_token": {
      "type": "secret",
      "description": "GitHub Personal Access Token",
      "required": true
    }
  }
}
```

#### 6.2 Marketplace
**Features**:
- Web UI for browsing/installing skills
- Search and filter by category, popularity, ratings
- User reviews and ratings
- Automated security scanning (static analysis, sandbox tests)
- Versioning and changelogs
- **All plugins free and open source**

**Backend**:
- Skill registry API (SQLite locally, optional GitHub-based registry)
- GitHub Actions for CI/CD (auto-test on publish)
- Community-driven moderation via GitHub PRs

---

### 7. Deployment & Infrastructure

#### 7.1 Docker Compose (Self-Hosted)
**Target**: Single-machine deployment for individuals/small teams

```yaml
services:
  gateway:
    image: librebird/gateway:latest
    ports:
      - "8741:8741"  # Web UI + API
      - "8742:8742"  # WebSocket
    environment:
      - REDIS_URL=redis://redis:6379
      - DATABASE_URL=postgresql://postgres:5432/librebird
    depends_on:
      - redis
      - postgres
      - brain

  brain:
    image: librebird/brain:latest
    volumes:
      - ./models:/models
    environment:
      - DATABASE_URL=postgresql://postgres:5432/librebird
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 16G

  hands:
    image: librebird/hands:latest
    volumes:
      - ./sandbox:/workspace
    security_opt:
      - seccomp=unconfined
    cap_add:
      - SYS_ADMIN

  redis:
    image: redis:7-alpine

  postgres:
    image: pgvector/pgvector:pg16
    volumes:
      - pgdata:/var/lib/postgresql/data
```

#### 7.2 Kubernetes (Optional)
**Target**: Advanced users who want horizontal scaling

**Components**:
- Gateway: Stateless, horizontal autoscaling (HPA)
- Brain: Worker pool with queue-based autoscaling
- Hands: On-demand containers (Kubernetes Jobs)
- Redis: Sentinel cluster for HA
- SQLite replaced with PostgreSQL for multi-instance deployments

**Helm Chart**:
```bash
$ helm install librebird ./charts/librebird \
  --set brain.replicas=3 \
  --set gateway.autoscaling.enabled=true \
  --set database.type=postgresql
```

**Note**: Kubernetes support is optional and targets advanced self-hosting scenarios. Docker Compose is the primary deployment method.

---

### 8. Security & Privacy Enhancements

#### 8.1 Sandboxing (Hands)
- Execute all tools in isolated Docker containers
- Restrict network access (allowlist for specific domains)
- Filesystem isolation (read-only mounts except `/workspace`)
- Resource limits (prevent DoS)

#### 8.2 Data Encryption
- **At-rest**: Encrypt SQLite/PostgreSQL with LUKS or application-level encryption
- **In-transit**: TLS 1.3 for all HTTP/WebSocket connections
- **End-to-end** (optional): Client-side encryption for messages (Signal Protocol)

#### 8.3 Audit Logging
- Log all sensitive actions (file access, command execution, API calls)
- Immutable append-only logs (write to S3 or WORM storage)
- User-facing activity dashboard

#### 8.4 Compliance
- **GDPR**: Data export, right to be forgotten (self-hosted = user owns data)
- **Privacy Policy**: Clear disclosure of what data is stored locally
- **No telemetry**: Zero analytics, zero phone-home (true local-first)

---

### 9. Developer Experience

#### 9.1 APIs
**REST API** (existing, enhanced):
- OpenAPI 3.1 spec with auto-generated clients
- Versioning (`/api/v2/...`)
- Pagination, filtering, sorting for all list endpoints
- Webhooks for events (new message, task completed, etc.)

**GraphQL API** (new):
- Single endpoint for flexible queries
- Real-time subscriptions via WebSocket
- Type-safe schema

**WebSocket API** (new):
- Bidirectional real-time communication
- Events: `message`, `typing`, `tool_progress`, `context_update`
- Reduced latency for chat

#### 9.2 SDKs
Official client libraries:
- **JavaScript/TypeScript** (npm)
- **Python** (PyPI)
- **Go** (Go modules)
- **Swift** (Swift Package Manager)
- **Kotlin/Java** (Maven Central)

Example:
```python
from librebird import Client

client = Client(api_key="lb_...")
response = client.chat("What's on my calendar today?")
for chunk in response:
    print(chunk.content, end="")
```

#### 9.3 CLI
```bash
# Install
$ npm install -g @librebird/cli

# Authenticate
$ lb login

# Chat from terminal
$ lb chat "Summarize my emails from today"

# Deploy a skill
$ lb skill deploy ./my-skill

# Manage conversations
$ lb conversations list
$ lb conversations export <id> --format json
```

---

### 10. Enhanced Features

#### 10.1 Proactive Suggestions (Enhanced)
- Keep existing `proactive.py` but make it configurable per-channel
- Smart notifications based on context:
  - "You have a meeting in 15 minutes"
  - "Your task is overdue"
  - "GitHub PR has new comments"
- Do Not Disturb mode (respect system focus modes)

#### 10.2 Collaboration (Phase 2)
**Deferred to Phase 2** based on single-user focus for v1.

Planned features:
- Shared conversations between owner and guests
- Conversation handoff
- Team workspaces

#### 10.3 Advanced Memory
- **Semantic search**: "What was that restaurant John recommended last month?"
- **Time-series analysis**: "How much time did I spend coding this week?"
- **Automatic summarization**: Condense old conversations into memories
- **Forgetting**: Automatic deletion of old data (configurable retention)

#### 10.4 Multimodal
- **Vision**: Analyze images (already partially supported via Gemini)
- **Audio**: Transcribe and analyze audio files
- **Video**: Extract frames, transcribe with timestamps
- **PDF/Documents**: Enhanced OCR and layout understanding

---

## Non-Functional Requirements

### Performance
- **Latency**: 
  - Gateway → Brain: <50ms (p95)
  - Brain → LLM first token: <500ms (p95) for local, <1s for cloud
  - Tool execution: <2s (p95)
- **Throughput**: Support 1000+ concurrent users per Gateway instance
- **Scaling**: Horizontal scaling for all services

### Reliability
- **Uptime**: 99.9% for cloud offering
- **Data durability**: No message loss (persistent queue)
- **Graceful degradation**: Read-only mode if Brain is down

### Observability
- **Metrics**: Prometheus + Grafana dashboards
- **Logging**: Structured JSON logs, centralized (ELK or Loki)
- **Tracing**: Distributed tracing (OpenTelemetry + Jaeger)
- **Alerts**: PagerDuty/Slack for critical issues

---

## Migration Strategy (Existing Users)

### Backward Compatibility
1. **Desktop app**: Keep as first-class citizen, continue as "Libre Bird Desktop"
2. **Data migration**: SQLite remains default, seamless upgrade path
3. **Skills**: Ensure all 26 existing skills work unchanged
4. **API**: Keep `/api/*` endpoints, add `/api/v2/*` for new features

### Rollout Plan
1. **Phase 1** (Month 1-3): Architecture refactor (Gateway/Brain/Hands separation)
2. **Phase 2** (Month 4-6): Telegram bot integration, web dashboard improvements
3. **Phase 3** (Month 7-9): Android app, enhanced marketplace
4. **Phase 4** (Month 10-12): Additional channels (Discord, Slack), advanced features

---

## Success Metrics

### Adoption
- **Installations**: 10k Docker deployments in first 6 months
- **GitHub Stars**: 5k+ stars within first year
- **Channels**: 40% of users connect Telegram or other messaging apps

### Engagement
- **Active instances**: >5k weekly active deployments
- **Messages per instance**: >100/week average
- **Retention**: >60% month-over-month active instances

### Developer Ecosystem
- **Community skills**: 50+ published in first year
- **Contributors**: 30+ GitHub contributors
- **Marketplace activity**: 10k+ skill installs across all users

---

## Confirmed Requirements & Technical Decisions

Based on stakeholder input, the following decisions have been confirmed:

### Scope & Prioritization
1. ✅ **Mobile apps**: Android first (iOS deferred to Phase 2)
2. ✅ **Channels**: Telegram priority (easiest integration with python-telegram-bot)
3. ✅ **Cloud offering**: Self-hosted only, no managed SaaS
4. ✅ **Team features**: Not critical for v1 (basic single-user auth only)

### Technical Stack Decisions
5. ✅ **Gateway**: **Node.js/TypeScript**
   - Better WebSocket ecosystem, easier community contributions
   - Performance sufficient for self-hosted use cases
   
6. ✅ **Database**: **SQLite as primary, PostgreSQL optional**
   - SQLite simpler for self-hosted, zero-config
   - Add PostgreSQL support later for power users
   - Architecture designed to support both via adapter pattern
   
7. ✅ **Vector DB**: **ChromaDB**
   - Self-hosted, no external service needed
   - Excellent Python integration
   - Lightweight for single-user deployments
   
8. ✅ **Message Queue**: **Redis**
   - Single dependency for queue + cache + session storage
   - Battle-tested, simple setup
   - Sufficient for self-hosted scale

### Infrastructure
9. ✅ **Deployment**: Docker Compose (Kubernetes optional for advanced users)
10. ✅ **Hosting**: Cloud-agnostic, self-hosted on user hardware
11. ✅ **LLM Providers**: Local (llama.cpp) + OpenAI + Anthropic for v1

### Business Model
12. ✅ **License**: Fully open source (MIT)
13. ✅ **Marketplace**: Free only, community-driven
14. ✅ **Pricing**: N/A (no SaaS offering)

### Simplified Architecture for v1

Given self-hosted + single-user focus, we can simplify:

- **No multi-tenancy**: Single workspace per instance
- **Basic auth**: Email/password + API keys (skip OAuth, magic links)
- **No RBAC**: Owner + guest roles only
- **Simplified database**: SQLite with migration path to PostgreSQL
- **Lightweight deployment**: docker-compose.yml as primary target
- **Single channel priority**: Telegram bot (easiest API)

---

## Appendix: Comparison with OpenClaw

| Feature | OpenClaw | Libre Bird (Current) | Libre Bird (Target) |
|---------|----------|----------------------|---------------------|
| **Architecture** | Gateway + Brain + Hands | Monolithic FastAPI | Gateway + Brain + Hands |
| **Multi-user** | ✅ Yes (cloud-first) | ❌ No | ✅ Yes (self-hosted) |
| **Channels** | WhatsApp, Telegram, Discord, Web | Desktop GUI only | Telegram, Web, Android |
| **Mobile apps** | ✅ iOS + Android | ❌ No | ✅ Android (iOS Phase 2) |
| **Self-hosted** | ✅ Docker | ✅ macOS binary | ✅ Docker (primary) |
| **Cloud hosting** | ✅ Managed service | ❌ No | ❌ No (self-hosted only) |
| **Plugin system** | ✅ Marketplace | ✅ Skill loader | ✅ Free marketplace |
| **Privacy** | ⚠️ Cloud-first | ✅ Local-only | ✅ Local-first, cloud optional |
| **LLM support** | OpenAI, Anthropic, local | Local only | Local + all major providers |
| **Voice** | ✅ STT/TTS | ✅ Whisper + macOS TTS | ✅ Enhanced multi-platform |
| **Screen context** | ⚠️ Limited | ✅ macOS only | ✅ Cross-platform |
| **Database** | PostgreSQL | SQLite | PostgreSQL + SQLite |
| **License** | Proprietary/Open Core | Open Source (MIT) | Open Source (MIT) |

**Libre Bird's Competitive Advantage**:
1. **Privacy-first**: True local-first option (OpenClaw is cloud-first)
2. **Screen context**: Deep macOS integration (expandable to Windows/Linux)
3. **26 pre-built skills**: More batteries-included than OpenClaw
4. **Open source**: Fully transparent, community-driven
5. **Voice-first**: Built-in wake word, hands-free operation
