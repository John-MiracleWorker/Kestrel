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

#### 2.1 Channel Adapters
Implement bidirectional adapters in Gateway for:

| Channel | Library/API | Priority | Features |
|---------|-------------|----------|----------|
| **Web** | WebSocket | P0 | Real-time chat, file upload, rich UI |
| **Mobile App** | Native clients + API | P0 | Push notifications, voice, camera |
| **WhatsApp** | Twilio API or WhatsApp Business API | P1 | Text, media, voice messages |
| **Telegram** | python-telegram-bot | P1 | Inline buttons, file sharing |
| **Discord** | discord.js | P1 | Slash commands, embeds |
| **Slack** | @slack/bolt | P2 | App mentions, modals, workflows |
| **SMS** | Twilio | P2 | Fallback for low-bandwidth |
| **Email** | SMTP/IMAP | P2 | Async requests via email |

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

#### 3.1 Authentication & Authorization
**Requirements**:
- Multiple auth strategies:
  - Email/password (bcrypt hashing)
  - OAuth2 (Google, GitHub, Microsoft)
  - Magic links (passwordless)
  - API keys for programmatic access
- JWT tokens for stateless authentication
- Role-based access control (RBAC):
  - `owner`: Full admin access
  - `admin`: User management, billing
  - `member`: Regular user
  - `guest`: Read-only, limited features
- Multi-tenancy: Workspaces/organizations
- Per-user skill permissions and quotas

**Database Schema** (PostgreSQL):
```sql
CREATE TABLE users (
  id UUID PRIMARY KEY,
  email VARCHAR(255) UNIQUE NOT NULL,
  password_hash VARCHAR(255),
  display_name VARCHAR(255),
  avatar_url TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE workspaces (
  id UUID PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  owner_id UUID REFERENCES users(id),
  plan VARCHAR(50) DEFAULT 'free',  -- free, pro, enterprise
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE workspace_members (
  workspace_id UUID REFERENCES workspaces(id),
  user_id UUID REFERENCES users(id),
  role VARCHAR(50) NOT NULL,  -- owner, admin, member, guest
  PRIMARY KEY (workspace_id, user_id)
);
```

#### 3.2 User Isolation
- **Conversations**: Scoped to workspace + user
- **Context/Memory**: Per-user, never shared unless explicitly granted
- **Skills**: Per-user enable/disable + workspace-level policies
- **Files**: User-owned storage with workspace-level sharing
- **API quotas**: Rate limiting per user + workspace

---

### 4. Data Storage & Memory

#### 4.1 Database Migration (SQLite → PostgreSQL)
**Why**: Multi-user support, horizontal scaling, better concurrency

**Migration Plan**:
- Keep SQLite option for single-user self-hosted setups
- Add PostgreSQL adapter with same interface
- Migrate schema:
  - Add `user_id` and `workspace_id` to all tables
  - Add `memories` vector embeddings table (pgvector)
  - Add `skills_config` for per-user settings
  - Add audit logs for security

#### 4.2 Vector Memory (RAG)
**Requirements**:
- Embed conversations, context, and documents using sentence-transformers
- Store in pgvector (PostgreSQL) or dedicated vector DB (ChromaDB, Qdrant, Pinecone)
- Semantic search for "recall" queries
- Time-weighted retrieval (recent context + relevant historical)
- Privacy: User vectors isolated, never shared

**Schema**:
```sql
CREATE TABLE memory_embeddings (
  id UUID PRIMARY KEY,
  user_id UUID REFERENCES users(id),
  content TEXT NOT NULL,
  embedding VECTOR(384),  -- or 768 depending on model
  metadata JSONB,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX ON memory_embeddings USING ivfflat (embedding vector_cosine_ops);
```

#### 4.3 File Storage
- **Local**: Keep filesystem for self-hosted
- **Cloud**: S3-compatible storage (AWS S3, MinIO, Backblaze)
- **Attachments**: User uploads, OCR results, generated files
- **Encryption**: At-rest encryption for cloud storage

---

### 5. Mobile Applications

#### 5.1 iOS App (Swift/SwiftUI)
**Features**:
- Native chat UI with dark mode
- Push notifications (APNs)
- Voice input (iOS Speech Framework)
- Camera integration for image analysis
- Siri shortcuts
- Face ID/Touch ID for auth
- Offline mode with sync queue

**Technical Stack**:
- SwiftUI + Combine
- URLSession for networking (WebSocket + REST)
- CoreData for local persistence
- Firebase Cloud Messaging (optional) for notifications

#### 5.2 Android App (Kotlin/Jetpack Compose)
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
- Paid plugins (revenue share model)

**Backend**:
- Skill registry API (PostgreSQL + S3 for package storage)
- GitHub Actions for CI/CD (auto-test on publish)
- Moderation queue for new skills

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

#### 7.2 Kubernetes (Cloud/Enterprise)
**Target**: Horizontal scaling for large deployments

**Components**:
- Gateway: Stateless, horizontal autoscaling (HPA)
- Brain: Worker pool with queue-based autoscaling
- Hands: On-demand containers (Kubernetes Jobs)
- Redis: Sentinel cluster for HA
- PostgreSQL: Managed service (RDS, Cloud SQL) or Patroni cluster

**Helm Chart**:
```bash
$ helm install librebird ./charts/librebird \
  --set brain.replicas=3 \
  --set gateway.autoscaling.enabled=true \
  --set postgres.host=my-postgres.rds.amazonaws.com
```

#### 7.3 Managed Cloud (SaaS)
**Optional**: Hosted version at `cloud.librebird.ai`

**Features**:
- Zero-setup onboarding
- Auto-scaling based on usage
- Managed backups and updates
- Enhanced security (SOC2, GDPR compliance)
- Premium features: Advanced analytics, team collaboration

**Pricing Tiers**:
- **Free**: 100 messages/month, local models only, 1 workspace
- **Pro** ($15/mo): 10k messages/month, cloud models, 5 workspaces, priority support
- **Enterprise**: Custom pricing, dedicated instances, SLA, SSO

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
- **GDPR**: Data export, right to be forgotten, consent management
- **SOC 2**: For enterprise cloud offering
- **Privacy Policy**: Clear disclosure of what data is stored/processed

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

#### 10.2 Collaboration
- **Shared conversations**: Multiple users in one chat
- **Handoff**: Transfer conversation to another user/agent
- **Mentions**: `@john can you review this?`
- **Workspaces**: Team-wide skills, shared knowledge base

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
1. **Desktop app**: Keep as first-class citizen, rebrand as "Libre Bird Desktop"
2. **Data migration**: Auto-migrate SQLite → PostgreSQL on first run (with backup)
3. **Skills**: Ensure all 26 existing skills work unchanged
4. **API**: Keep `/api/*` endpoints, add `/api/v2/*` for new features

### Rollout Plan
1. **Phase 1** (Month 1-3): Architecture refactor (Gateway/Brain/Hands)
2. **Phase 2** (Month 4-6): Multi-user, PostgreSQL, web dashboard
3. **Phase 3** (Month 7-9): Channel integrations (WhatsApp, Telegram, Discord)
4. **Phase 4** (Month 10-12): Mobile apps, marketplace, cloud offering

---

## Success Metrics

### Adoption
- **Users**: 10k users in first 6 months
- **Workspaces**: 1k team workspaces created
- **Channels**: 50% of users connect non-web channels

### Engagement
- **DAU/MAU**: >40% ratio
- **Messages per user**: >50/month average
- **Retention**: >70% month-1 retention

### Developer Ecosystem
- **Community skills**: 100+ published in first year
- **Contributors**: 50+ GitHub contributors
- **SDK downloads**: 10k+ downloads across all languages

### Revenue (Cloud SaaS)
- **Conversions**: 10% free → pro conversion
- **MRR**: $50k+ within first year of cloud launch

---

## Open Questions for User Clarification

### Scope & Prioritization
1. **Mobile apps**: Are iOS and Android both equally important, or should we focus on one platform first?
2. **Channels**: Which messaging platforms are highest priority? (WhatsApp, Telegram, Discord, Slack, SMS)
3. **Cloud offering**: Do you want to build a managed SaaS, or focus purely on self-hosted?
4. **Enterprise features**: Are team collaboration features (workspaces, RBAC) critical for v1?

### Technical Decisions
5. **Programming language**:
   - Keep Python for Brain + Hands?
   - Use Node.js/TypeScript for Gateway, or Go for better performance?
6. **Database**: PostgreSQL mandatory, or support SQLite for single-user setups?
7. **Vector DB**: Managed (Pinecone) vs self-hosted (ChromaDB/Qdrant) vs PostgreSQL pgvector?
8. **Message queue**: Redis Pub/Sub, RabbitMQ, or cloud-native (AWS SQS, Google Pub/Sub)?

### Infrastructure
9. **Deployment**: Docker Compose as minimum viable, or Kubernetes from day one?
10. **Hosting**: AWS, GCP, Azure, or cloud-agnostic (via Terraform)?
11. **LLM providers**: Which cloud LLM APIs to support first? (OpenAI, Anthropic, Google, Azure, Cohere, etc.)

### Business Model
12. **Open source**: Keep fully open source, or open-core with premium features?
13. **Marketplace**: Free only, or support paid plugins with revenue sharing?
14. **Pricing**: For cloud SaaS, confirm pricing tiers and limits?

---

## Assumptions

Since this PRD is created without full clarification, these assumptions are made:

1. **Target audience**: Developers, power users, small teams (similar to OpenClaw)
2. **Platform priority**: Web + Desktop first, mobile later
3. **Deployment**: Self-hosted Docker Compose as primary, Kubernetes optional
4. **Database**: PostgreSQL for multi-user, SQLite fallback for single-user
5. **Gateway language**: Node.js/TypeScript (aligns with OpenClaw, good for real-time)
6. **Brain language**: Keep Python (existing codebase, ML ecosystem)
7. **Channels**: Start with web, WhatsApp, Telegram (most requested)
8. **Business model**: Open source (MIT/Apache 2.0) with optional managed cloud offering
9. **LLM support**: Local (llama.cpp) + OpenAI + Anthropic for v1
10. **Mobile apps**: Phase 2 priority (after core platform is stable)

**These assumptions should be validated with stakeholders before proceeding to technical specification.**

---

## Appendix: Comparison with OpenClaw

| Feature | OpenClaw | Libre Bird (Current) | Libre Bird (Target) |
|---------|----------|----------------------|---------------------|
| **Architecture** | Gateway + Brain + Hands | Monolithic FastAPI | Gateway + Brain + Hands |
| **Multi-user** | ✅ Yes | ❌ No | ✅ Yes |
| **Channels** | WhatsApp, Telegram, Discord, Web | Desktop GUI only | All channels |
| **Mobile apps** | ✅ iOS + Android | ❌ No | ✅ iOS + Android |
| **Self-hosted** | ✅ Docker | ✅ macOS binary | ✅ Docker + K8s |
| **Cloud hosting** | ✅ Managed service | ❌ No | ✅ Optional SaaS |
| **Plugin system** | ✅ Marketplace | ✅ Skill loader | ✅ Enhanced marketplace |
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
