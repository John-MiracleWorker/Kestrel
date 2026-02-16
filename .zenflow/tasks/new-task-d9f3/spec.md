# Technical Specification: Libre Bird → OpenClaw-like Platform

## 1. Executive Summary

This specification outlines the technical approach for transforming **Libre Bird** from a single-user macOS desktop AI assistant into a **multi-platform, multi-channel, multi-user AI agent platform** while maintaining its privacy-first philosophy.

**Architecture Pattern**: Gateway/Brain/Hands (distributed microservices)  
**Timeline**: 12 months, delivered in 4 phases  
**Backward Compatibility**: Yes, existing desktop app and skills remain fully functional

---

## 2. Technical Context

### 2.1 Current Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Backend** | Python 3.9+ / FastAPI | Monolithic API server |
| **LLM** | llama-cpp-python + Metal | Local GGUF model inference |
| **Database** | SQLite + FTS5 | Conversations, context, tasks, settings |
| **Frontend** | Vanilla JS + Vite | Desktop web UI |
| **Desktop** | pywebview | Native macOS wrapper |
| **Skills** | 26 modular Python packages | 101 tools (file ops, APIs, system control) |
| **Voice** | OpenAI Whisper Small | Local STT with wake word detection |
| **TTS** | macOS NSSpeechSynthesizer | Neural voice output |

**Dependencies** (requirements.txt):
```
fastapi==0.115.6
uvicorn[standard]==0.34.0
llama-cpp-python==0.3.4
pyobjc-framework-* (macOS bindings)
aiosqlite==0.20.0
sse-starlette==2.1.3
chromadb
openai-whisper==20240930
```

### 2.2 Target Architecture

**Gateway/Brain/Hands** distributed system with:

```
┌─────────────────────────────────────────────────────────────┐
│                         Gateway (Node.js)                    │
│  WebSocket • HTTP • Multi-channel adapters • Auth • Router  │
└───────────┬─────────────────────────────────────────────────┘
            │
    ┌───────┴───────┐
    │               │
┌───▼────┐      ┌───▼────┐
│ Brain  │◄────►│ Hands  │
│(Python)│      │(Python)│
└────────┘      └────────┘
  │                 │
  │  PostgreSQL    │  Docker
  │  + pgvector    │  Sandbox
  └────────────────┘
```

**Communication**:
- Gateway ↔ Clients: WebSocket (primary), HTTP (fallback)
- Gateway ↔ Brain: gRPC (low-latency RPC)
- Brain ↔ Hands: gRPC (sandboxed execution)
- Async Jobs: Redis Streams + Bull queues

---

## 3. Implementation Approach

### 3.1 Service Decomposition

#### 3.1.1 Gateway Service (NEW - Node.js/TypeScript)

**Purpose**: Long-lived coordination hub managing connections, routing, and channels

**Responsibilities**:
- WebSocket server for real-time bidirectional communication
- Multi-channel adapters (WhatsApp, Telegram, Discord, web, mobile)
- Session management (Redis-backed)
- Authentication & authorization (JWT + OAuth2)
- Rate limiting & security enforcement
- Message queue orchestration
- Health checks & metrics

**Tech Stack**:
```json
{
  "runtime": "Node.js 20+ / TypeScript 5+",
  "framework": "Fastify",
  "websocket": "ws + Socket.io fallback",
  "session": "Redis + ioredis",
  "queue": "BullMQ (Redis Streams)",
  "auth": "Passport.js (JWT + OAuth2)",
  "rpc": "@grpc/grpc-js",
  "metrics": "prom-client (Prometheus)",
  "validation": "Zod"
}
```

**File Structure**:
```
gateway/
├── src/
│   ├── server.ts                 # Fastify app + WebSocket server
│   ├── auth/
│   │   ├── strategies/          # JWT, OAuth2, magic link
│   │   └── middleware.ts         # Auth guards
│   ├── channels/
│   │   ├── base.ts              # BaseChannelAdapter interface
│   │   ├── web.ts               # WebSocket channel
│   │   ├── whatsapp.ts          # Twilio/WhatsApp Business API
│   │   ├── telegram.ts          # Telegram Bot API
│   │   └── discord.ts           # Discord.js adapter
│   ├── brain/
│   │   ├── client.ts            # gRPC Brain client
│   │   └── brain.proto          # Proto definition
│   ├── session/
│   │   └── manager.ts           # Redis-backed sessions
│   ├── queue/
│   │   └── workers.ts           # BullMQ job processors
│   └── utils/
│       ├── logger.ts            # Structured logging (Winston)
│       └── metrics.ts           # Prometheus metrics
├── package.json
├── tsconfig.json
└── Dockerfile
```

**Key Interfaces**:
```typescript
// Unified message format
interface Message {
  id: string;
  channel: 'web' | 'whatsapp' | 'telegram' | 'discord' | 'mobile';
  userId: string;
  workspaceId: string;
  conversationId: string;
  content: string;
  attachments?: Attachment[];
  timestamp: Date;
  metadata: Record<string, any>;
}

// Channel adapter contract
interface ChannelAdapter {
  connect(): Promise<void>;
  disconnect(): Promise<void>;
  send(message: Message): Promise<void>;
  onMessage(handler: (msg: Message) => void): void;
}

// gRPC Brain service
service Brain {
  rpc StreamChat(ChatRequest) returns (stream ChatChunk);
  rpc ExecuteTool(ToolRequest) returns (ToolResponse);
  rpc GetMemory(MemoryQuery) returns (MemoryResponse);
}
```

#### 3.1.2 Brain Service (REFACTOR - Python)

**Purpose**: LLM-powered reasoning, planning, and decision-making

**Migration Strategy**:
- **Keep** existing `llm_engine.py`, `skill_loader.py`, `agent_modes.py`
- **Add** gRPC server wrapper
- **Enhance** with multi-provider support (OpenAI, Anthropic, Google)
- **Add** pgvector for RAG memory
- **Refactor** for stateless operation (session state in Redis)

**Changes**:
```python
# NEW: brain/grpc_server.py
import grpc
from concurrent import futures
from brain_pb2_grpc import BrainServicer
from llm_engine import engine

class BrainService(BrainServicer):
    async def StreamChat(self, request, context):
        async for chunk in engine.stream_chat(
            messages=request.messages,
            tools=request.tools,
            model=request.model,
            user_id=request.user_id
        ):
            yield ChatChunk(content=chunk)

# ENHANCE: llm_engine.py
class LLMEngine:
    def __init__(self):
        self._local_model: Optional[Llama] = None
        self._cloud_clients = {
            'openai': OpenAI(api_key=...),
            'anthropic': Anthropic(api_key=...),
            'google': genai.Client(api_key=...)
        }
    
    async def stream_chat(
        self, 
        messages: list[dict],
        provider: str = 'local',  # NEW
        model: Optional[str] = None,
        user_id: str,  # NEW - for RAG lookup
        workspace_id: str,  # NEW
        **kwargs
    ):
        # Add RAG context retrieval
        context = await self.memory.retrieve(user_id, query=messages[-1])
        
        # Route to appropriate provider
        if provider == 'local':
            async for chunk in self._stream_local(messages, context):
                yield chunk
        elif provider == 'openai':
            async for chunk in self._stream_openai(messages, context, model):
                yield chunk
        # ... other providers
```

**New File Structure**:
```
brain/
├── server.py                    # gRPC server (NEW)
├── llm_engine.py                # Enhanced with multi-provider (REFACTOR)
├── skill_loader.py              # Keep as-is
├── agent_modes.py               # Keep as-is
├── memory/
│   ├── vector_store.py          # pgvector RAG (NEW)
│   └── embeddings.py            # sentence-transformers (NEW)
├── providers/
│   ├── local.py                 # llama-cpp wrapper (EXTRACT from llm_engine)
│   ├── openai.py                # OpenAI API (NEW)
│   ├── anthropic.py             # Anthropic API (NEW)
│   └── google.py                # Google Gemini API (NEW)
├── protos/
│   └── brain.proto              # gRPC definitions (NEW)
├── tools.py → skills/           # Already modular
└── requirements.txt             # Add grpcio, openai, anthropic, google-genai
```

**Database Changes** (PostgreSQL migration):
```sql
-- NEW: Multi-user schema
CREATE TABLE users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email VARCHAR(255) UNIQUE NOT NULL,
  password_hash VARCHAR(255),
  display_name VARCHAR(255),
  avatar_url TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE workspaces (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(255) NOT NULL,
  owner_id UUID REFERENCES users(id),
  plan VARCHAR(50) DEFAULT 'free',
  settings JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE workspace_members (
  workspace_id UUID REFERENCES workspaces(id),
  user_id UUID REFERENCES users(id),
  role VARCHAR(50) NOT NULL, -- 'owner', 'admin', 'member', 'guest'
  joined_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (workspace_id, user_id)
);

-- MIGRATE: Add user/workspace columns to existing tables
ALTER TABLE conversations ADD COLUMN user_id UUID REFERENCES users(id);
ALTER TABLE conversations ADD COLUMN workspace_id UUID REFERENCES workspaces(id);
ALTER TABLE context_snapshots ADD COLUMN user_id UUID REFERENCES users(id);
ALTER TABLE tasks ADD COLUMN user_id UUID REFERENCES users(id);

-- NEW: Vector memory (RAG)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE memory_embeddings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id),
  workspace_id UUID REFERENCES workspaces(id),
  content TEXT NOT NULL,
  embedding VECTOR(384),  -- all-MiniLM-L6-v2
  source_type VARCHAR(50),  -- 'conversation', 'document', 'context'
  source_id TEXT,
  metadata JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON memory_embeddings USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);
```

#### 3.1.3 Hands Service (REFACTOR - Python + Docker)

**Purpose**: Sandboxed execution environment for tools

**Migration Strategy**:
- **Extract** tool execution from Brain into separate service
- **Add** Docker-based sandboxing
- **Keep** existing skill implementations (26 skills / 101 tools)
- **Add** gRPC interface for Brain → Hands RPC

**Architecture**:
```
┌──────────────────────────────────────┐
│         Hands Coordinator            │
│  gRPC server, auth, rate limiting   │
└────────────┬─────────────────────────┘
             │
   ┌─────────┴─────────┐
   │                   │
┌──▼───────┐    ┌──▼───────┐
│ Worker 1 │    │ Worker 2 │
│ (Docker) │    │ (Docker) │
│ sandbox  │    │ sandbox  │
└──────────┘    └──────────┘
```

**File Structure**:
```
hands/
├── server.py                    # gRPC coordinator (NEW)
├── executor.py                  # Docker container manager (NEW)
├── sandbox/
│   ├── Dockerfile              # Base sandbox image (NEW)
│   └── entrypoint.py           # Tool runner (NEW)
├── skills/                     # MOVE from brain/ (26 skills)
│   ├── core/
│   ├── productivity/
│   ├── ...
│   └── __init__.py
├── security/
│   ├── allowlist.py            # Domain/command allowlists (NEW)
│   └── audit.py                # Action logging (NEW)
└── requirements.txt
```

**Tool Execution Flow**:
```python
# hands/server.py
class HandsService(HandsServicer):
    async def ExecuteTool(self, request, context):
        # Validate permissions
        if not await self.check_permission(request.user_id, request.tool_name):
            raise PermissionError(f"User lacks permission for {request.tool_name}")
        
        # Spawn sandboxed container
        container_id = await self.executor.spawn(
            image='librebird/hands-sandbox:latest',
            env={
                'TOOL_NAME': request.tool_name,
                'TOOL_ARGS': json.dumps(request.arguments),
                'USER_ID': request.user_id  # For audit logs
            },
            limits={'cpu': 1.0, 'memory': '512M'},
            timeout=30  # 30 second max
        )
        
        # Stream output
        async for log in self.executor.logs(container_id):
            yield ToolOutput(content=log)
        
        # Get result and cleanup
        result = await self.executor.wait(container_id)
        await self.executor.remove(container_id)
        
        return ToolResponse(
            success=result.exit_code == 0,
            output=result.stdout,
            error=result.stderr
        )
```

**Sandbox Security** (Dockerfile):
```dockerfile
FROM python:3.11-slim

# Non-root user
RUN useradd -m -u 1000 sandbox
WORKDIR /workspace
RUN chown sandbox:sandbox /workspace

# Install minimal deps (no network after build)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy skill modules
COPY skills/ /app/skills/
COPY sandbox/entrypoint.py /app/

USER sandbox

# Resource limits enforced by Docker
# --memory=512m --cpus=1 --network=none (or custom net)

ENTRYPOINT ["python", "/app/entrypoint.py"]
```

---

### 3.2 Multi-Channel Support

#### 3.2.1 Channel Adapter Pattern

**Base Interface** (TypeScript):
```typescript
// gateway/src/channels/base.ts
export abstract class BaseChannelAdapter {
  abstract readonly channelType: ChannelType;
  
  abstract connect(): Promise<void>;
  abstract disconnect(): Promise<void>;
  abstract send(message: OutgoingMessage): Promise<void>;
  
  // Event emitter for incoming messages
  on(event: 'message', handler: (msg: IncomingMessage) => void): void;
  on(event: 'error', handler: (err: Error) => void): void;
}

// Unified message types
interface IncomingMessage {
  id: string;
  channel: ChannelType;
  userId: string;  // Mapped from channel-specific user ID
  content: string;
  attachments?: Attachment[];
  metadata: {
    channelUserId: string;  // Original WhatsApp/Telegram ID
    channelMessageId: string;
    timestamp: Date;
  };
}

interface OutgoingMessage {
  conversationId: string;
  content: string;
  attachments?: Attachment[];
  options?: {
    buttons?: Button[];  // For Telegram/Discord
    markdown?: boolean;
    mentions?: string[];
  };
}
```

#### 3.2.2 Implementation Priority

**Phase 1** (P0): Web + Mobile API  
**Phase 2** (P1): WhatsApp + Telegram  
**Phase 3** (P2): Discord + Slack

**Example: WhatsApp Adapter**:
```typescript
// gateway/src/channels/whatsapp.ts
import twilio from 'twilio';

export class WhatsAppAdapter extends BaseChannelAdapter {
  readonly channelType = 'whatsapp';
  private client: twilio.Twilio;
  
  async connect() {
    this.client = twilio(process.env.TWILIO_SID, process.env.TWILIO_AUTH_TOKEN);
    
    // Webhook server for incoming messages
    this.app.post('/webhooks/whatsapp', async (req, res) => {
      const { From, Body, MediaUrl0 } = req.body;
      
      // Map WhatsApp phone to internal user ID
      const userId = await this.userService.getByWhatsApp(From);
      
      this.emit('message', {
        id: crypto.randomUUID(),
        channel: 'whatsapp',
        userId,
        content: Body,
        attachments: MediaUrl0 ? [{ url: MediaUrl0 }] : [],
        metadata: {
          channelUserId: From,
          channelMessageId: req.body.MessageSid,
          timestamp: new Date()
        }
      });
      
      res.sendStatus(200);
    });
  }
  
  async send(message: OutgoingMessage) {
    const whatsappNumber = await this.userService.getWhatsAppNumber(message.userId);
    
    await this.client.messages.create({
      from: `whatsapp:${process.env.TWILIO_WHATSAPP_NUMBER}`,
      to: `whatsapp:${whatsappNumber}`,
      body: message.content,
      mediaUrl: message.attachments?.map(a => a.url)
    });
  }
}
```

---

### 3.3 Multi-User System

#### 3.3.1 Authentication Flow

**Supported Methods**:
1. Email/Password (bcrypt)
2. OAuth2 (Google, GitHub, Microsoft)
3. Magic Link (passwordless)
4. API Keys (for programmatic access)

**JWT Token Structure**:
```typescript
interface JWTPayload {
  sub: string;        // user_id (UUID)
  email: string;
  workspaces: Array<{
    id: string;       // workspace_id
    role: 'owner' | 'admin' | 'member' | 'guest';
  }>;
  iat: number;
  exp: number;        // 7 days
}
```

**Auth Middleware** (Gateway):
```typescript
// gateway/src/auth/middleware.ts
export const requireAuth = async (req, res, next) => {
  const token = req.headers.authorization?.replace('Bearer ', '');
  
  if (!token) {
    return res.status(401).json({ error: 'No token provided' });
  }
  
  try {
    const payload = jwt.verify(token, process.env.JWT_SECRET) as JWTPayload;
    
    // Attach user context to request
    req.user = {
      id: payload.sub,
      email: payload.email,
      workspaces: payload.workspaces
    };
    
    next();
  } catch (err) {
    return res.status(401).json({ error: 'Invalid token' });
  }
};

export const requireWorkspace = (req, res, next) => {
  const workspaceId = req.params.workspaceId || req.query.workspace;
  
  if (!workspaceId) {
    return res.status(400).json({ error: 'Workspace ID required' });
  }
  
  const membership = req.user.workspaces.find(w => w.id === workspaceId);
  
  if (!membership) {
    return res.status(403).json({ error: 'Not a member of this workspace' });
  }
  
  req.workspace = { id: workspaceId, role: membership.role };
  next();
};
```

#### 3.3.2 Data Isolation

**Scoping Pattern**:
- All queries MUST filter by `user_id` AND `workspace_id`
- Row-Level Security (RLS) policies in PostgreSQL

**Example RLS Policy**:
```sql
-- Enable RLS
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;

-- Policy: Users can only see conversations in their workspaces
CREATE POLICY conversations_workspace_isolation ON conversations
  USING (
    workspace_id IN (
      SELECT workspace_id FROM workspace_members
      WHERE user_id = current_setting('app.user_id')::uuid
    )
  );

-- Set user context in each query (via application)
SET LOCAL app.user_id = '<user_uuid>';
SELECT * FROM conversations;  -- Automatically filtered
```

---

### 3.4 Mobile Applications

#### 3.4.1 Shared Backend API

**WebSocket + REST Hybrid**:
- **WebSocket**: Real-time chat streaming
- **REST**: CRUD operations (conversations, settings, etc.)

**Mobile-Specific Endpoints**:
```typescript
// Push notifications
POST /api/mobile/register-push
{
  "device_token": "fcm_token_...",
  "platform": "ios" | "android"
}

// Offline sync queue
GET /api/mobile/sync?since=<timestamp>
Response: {
  messages: [...],
  conversations: [...],
  tasks: [...]
}
```

#### 3.4.2 iOS App (Swift/SwiftUI)

**Architecture**: MVVM + Combine

```swift
// Shared/Models/Message.swift
struct Message: Codable, Identifiable {
    let id: String
    let conversationId: String
    let role: MessageRole
    let content: String
    let createdAt: Date
}

// Services/APIClient.swift
class APIClient: ObservableObject {
    private let baseURL = "https://api.librebird.ai"
    private var webSocket: URLSessionWebSocketTask?
    
    @Published var messages: [Message] = []
    
    func connect(token: String) {
        let url = URL(string: "\(baseURL)/ws")!
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        
        webSocket = URLSession.shared.webSocketTask(with: request)
        webSocket?.resume()
        receiveMessage()
    }
    
    private func receiveMessage() {
        webSocket?.receive { [weak self] result in
            switch result {
            case .success(.string(let text)):
                if let message = try? JSONDecoder().decode(Message.self, from: text.data(using: .utf8)!) {
                    DispatchQueue.main.async {
                        self?.messages.append(message)
                    }
                }
                self?.receiveMessage()  // Continue receiving
            case .failure(let error):
                print("WebSocket error: \(error)")
            default:
                break
            }
        }
    }
}

// Views/ChatView.swift
struct ChatView: View {
    @StateObject private var api = APIClient()
    @State private var inputText = ""
    
    var body: some View {
        VStack {
            ScrollView {
                ForEach(api.messages) { message in
                    MessageBubble(message: message)
                }
            }
            
            HStack {
                TextField("Message", text: $inputText)
                    .textFieldStyle(.roundedBorder)
                
                Button("Send") {
                    api.sendMessage(inputText)
                    inputText = ""
                }
            }
            .padding()
        }
        .onAppear {
            api.connect(token: AuthManager.shared.token)
        }
    }
}
```

**Local Persistence** (CoreData):
```swift
// Models/ConversationEntity+CoreDataClass.swift
@objc(ConversationEntity)
public class ConversationEntity: NSManagedObject {
    @NSManaged public var id: String
    @NSManaged public var title: String
    @NSManaged public var lastSync: Date?
    @NSManaged public var messages: NSSet?
}

// Sync logic
class SyncManager {
    func syncIfNeeded() async {
        let lastSync = UserDefaults.standard.object(forKey: "lastSync") as? Date ?? .distantPast
        
        let updates = try await api.getUpdates(since: lastSync)
        
        // Merge into CoreData
        await MainActor.run {
            let context = PersistenceController.shared.container.viewContext
            for message in updates.messages {
                let entity = MessageEntity(context: context)
                entity.id = message.id
                entity.content = message.content
                // ...
            }
            try? context.save()
            UserDefaults.standard.set(Date(), forKey: "lastSync")
        }
    }
}
```

#### 3.4.3 Android App (Kotlin/Compose)

**Similar architecture**, using:
- **Retrofit** for REST API
- **OkHttp** + WebSocket for real-time
- **Room** for local database
- **WorkManager** for background sync

---

### 3.5 Plugin Ecosystem

#### 3.5.1 Enhanced Skill Manifest

**Backward Compatible** with existing `skill.json`, adding:

```json
{
  "name": "github_advanced",
  "display_name": "GitHub Advanced",
  "version": "2.1.0",
  "author": "community",
  "description": "PR reviews, CI/CD monitoring, code search",
  "icon": "🐙",
  "category": "developer_tools",
  "runtime": "python",  // or "typescript", "docker"
  "entrypoint": "__init__.py",
  
  "dependencies": ["requests>=2.31.0", "PyGithub>=2.0.0"],
  
  "permissions": [
    "network",           // Internet access
    "env_vars",          // Read environment variables
    "filesystem:read",   // Read-only filesystem
    "api:github"         // Pre-approved API access
  ],
  
  "settings": {
    "github_token": {
      "type": "secret",
      "description": "GitHub Personal Access Token",
      "required": true,
      "validation": "^ghp_[a-zA-Z0-9]{36}$"
    },
    "default_org": {
      "type": "string",
      "description": "Default GitHub organization",
      "required": false
    }
  },
  
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "review_pr",
        "description": "AI-powered PR code review",
        "parameters": {
          "type": "object",
          "properties": {
            "repo": {"type": "string"},
            "pr_number": {"type": "integer"}
          },
          "required": ["repo", "pr_number"]
        }
      }
    }
  ],
  
  "marketplace": {
    "price": 0,  // Free, or price in cents
    "license": "MIT",
    "repository": "https://github.com/user/skill-github-advanced",
    "homepage": "https://docs.example.com/github-skill"
  }
}
```

#### 3.5.2 CLI Tool

```bash
# Install CLI
npm install -g @librebird/cli

# Scaffold new skill
lb create-skill --name my-weather --template python
# Generates:
# my-weather/
# ├── skill.json
# ├── __init__.py
# ├── tests/
# └── README.md

# Local development
cd my-weather
lb dev  # Runs local Brain + Hands with skill hot-reload

# Test skill
lb test-skill  # Runs tests/ and validates manifest

# Publish to marketplace
lb login
lb publish-skill --public
# Output: Published my-weather@1.0.0 → https://marketplace.librebird.ai/skills/my-weather
```

#### 3.5.3 Marketplace Backend

**Stack**: PostgreSQL + S3 + GitHub Actions CI

```typescript
// marketplace/src/routes/skills.ts
app.post('/api/marketplace/skills/publish', requireAuth, async (req, res) => {
  const { manifestUrl, repositoryUrl } = req.body;
  
  // Fetch and validate manifest
  const manifest = await fetch(manifestUrl).then(r => r.json());
  const validation = validateManifest(manifest);
  if (!validation.valid) {
    return res.status(400).json({ errors: validation.errors });
  }
  
  // Security scan
  const scanResult = await securityScanner.scan(repositoryUrl);
  if (scanResult.critical > 0) {
    return res.status(400).json({ error: 'Security issues detected', details: scanResult });
  }
  
  // Create skill entry
  const skill = await db.skills.create({
    name: manifest.name,
    version: manifest.version,
    authorId: req.user.id,
    manifest,
    status: 'pending_review',  // Manual approval for first publish
    packageUrl: await uploadToS3(manifest.name, repositoryUrl)
  });
  
  // Notify moderators
  await notifyModerators('New skill pending review', skill);
  
  res.json({ skillId: skill.id, status: 'pending_review' });
});
```

---

### 3.6 Deployment Strategy

#### 3.6.1 Docker Compose (Self-Hosted)

**Target**: Single-machine deployment for individuals/teams

```yaml
# docker-compose.yml
version: '3.8'

services:
  gateway:
    build: ./gateway
    image: librebird/gateway:latest
    ports:
      - "8741:8741"    # HTTP/WS
    environment:
      - NODE_ENV=production
      - REDIS_URL=redis://redis:6379
      - DATABASE_URL=postgresql://postgres:password@postgres:5432/librebird
      - BRAIN_GRPC_URL=brain:50051
      - JWT_SECRET=${JWT_SECRET}
    depends_on:
      - redis
      - postgres
      - brain
    restart: unless-stopped

  brain:
    build: ./brain
    image: librebird/brain:latest
    volumes:
      - ./models:/models:ro      # Local GGUF models
      - ./skills:/app/skills:ro  # Custom skills
    environment:
      - DATABASE_URL=postgresql://postgres:password@postgres:5432/librebird
      - REDIS_URL=redis://redis:6379
      - HANDS_GRPC_URL=hands:50052
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 16G
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]  # Optional GPU
    restart: unless-stopped

  hands:
    build: ./hands
    image: librebird/hands:latest
    privileged: true  # For Docker-in-Docker sandboxing
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./skills:/app/skills:ro
    environment:
      - DATABASE_URL=postgresql://postgres:password@postgres:5432/librebird
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    restart: unless-stopped

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      - POSTGRES_DB=librebird
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=password
    volumes:
      - pg_data:/var/lib/postgresql/data
    restart: unless-stopped

volumes:
  redis_data:
  pg_data:
```

**Quick Start**:
```bash
# Clone repo
git clone https://github.com/librebird/librebird-platform.git
cd librebird-platform

# Configure
cp .env.example .env
# Edit .env with your settings (JWT_SECRET, API keys, etc.)

# Start services
docker-compose up -d

# View logs
docker-compose logs -f

# Access web UI
open http://localhost:8741
```

#### 3.6.2 Kubernetes (Cloud/Enterprise)

**Helm Chart** structure:
```
charts/librebird/
├── Chart.yaml
├── values.yaml
├── templates/
│   ├── gateway/
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   ├── hpa.yaml              # Horizontal autoscaling
│   │   └── ingress.yaml
│   ├── brain/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── hands/
│   │   └── job-template.yaml     # On-demand Jobs
│   └── redis/
│       ├── statefulset.yaml
│       └── service.yaml
```

**Install**:
```bash
helm repo add librebird https://charts.librebird.ai
helm install librebird librebird/librebird \
  --set gateway.replicas=3 \
  --set brain.replicas=2 \
  --set postgres.enabled=false \
  --set postgres.externalHost=my-db.rds.amazonaws.com \
  --set ingress.enabled=true \
  --set ingress.host=librebird.mycompany.com
```

---

## 4. Data Model Changes

### 4.1 Migration Path (SQLite → PostgreSQL)

**Strategy**: Dual-database support during transition

```python
# database.py (enhanced)
class Database:
    def __init__(self, config: DatabaseConfig):
        self.backend = config.backend  # 'sqlite' or 'postgresql'
        
        if self.backend == 'sqlite':
            self._conn = aiosqlite.connect(config.path)
        else:
            self._conn = asyncpg.create_pool(config.url)
    
    # Abstraction layer for queries
    async def get_conversations(self, user_id: str, workspace_id: str):
        if self.backend == 'sqlite':
            # Legacy single-user: no filtering
            return await self._conn.execute("SELECT * FROM conversations")
        else:
            # Multi-user: filter by user + workspace
            return await self._conn.fetch(
                "SELECT * FROM conversations WHERE user_id=$1 AND workspace_id=$2",
                user_id, workspace_id
            )
```

**Migration Script**:
```python
# scripts/migrate_sqlite_to_postgres.py
async def migrate():
    sqlite_db = await aiosqlite.connect('libre_bird.db')
    pg_db = await asyncpg.connect(os.getenv('DATABASE_URL'))
    
    # Create default user for single-user migration
    user_id = await pg_db.fetchval(
        "INSERT INTO users (email, display_name) VALUES ($1, $2) RETURNING id",
        "migrated@local", "Migrated User"
    )
    
    workspace_id = await pg_db.fetchval(
        "INSERT INTO workspaces (name, owner_id) VALUES ($1, $2) RETURNING id",
        "My Workspace", user_id
    )
    
    # Migrate conversations
    async for row in sqlite_db.execute("SELECT * FROM conversations"):
        await pg_db.execute(
            """INSERT INTO conversations (id, title, user_id, workspace_id, created_at)
               VALUES ($1, $2, $3, $4, $5)""",
            row['id'], row['title'], user_id, workspace_id, row['created_at']
        )
    
    # Migrate messages, tasks, etc.
    # ...
    
    print(f"Migration complete. User ID: {user_id}, Workspace ID: {workspace_id}")
```

### 4.2 Vector Memory Schema

```sql
-- brain/migrations/002_add_vector_memory.sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE memory_embeddings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  
  content TEXT NOT NULL,
  embedding VECTOR(384),  -- sentence-transformers/all-MiniLM-L6-v2
  
  source_type VARCHAR(50) NOT NULL,  -- 'conversation', 'document', 'context', 'knowledge'
  source_id TEXT,
  
  metadata JSONB DEFAULT '{}',
  
  created_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ  -- Optional TTL
);

-- Indexes
CREATE INDEX idx_memory_user_workspace ON memory_embeddings(user_id, workspace_id);
CREATE INDEX idx_memory_embedding ON memory_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_memory_created ON memory_embeddings(created_at DESC);

-- Enable RLS
ALTER TABLE memory_embeddings ENABLE ROW LEVEL SECURITY;

CREATE POLICY memory_isolation ON memory_embeddings
  USING (
    workspace_id IN (
      SELECT workspace_id FROM workspace_members
      WHERE user_id = current_setting('app.user_id')::uuid
    )
  );
```

**Usage**:
```python
# brain/memory/vector_store.py
from sentence_transformers import SentenceTransformer

class VectorMemory:
    def __init__(self, db: asyncpg.Pool):
        self.db = db
        self.encoder = SentenceTransformer('all-MiniLM-L6-v2')
    
    async def add(self, user_id: str, workspace_id: str, content: str, metadata: dict):
        embedding = self.encoder.encode(content).tolist()
        
        await self.db.execute(
            """INSERT INTO memory_embeddings (user_id, workspace_id, content, embedding, metadata)
               VALUES ($1, $2, $3, $4, $5)""",
            user_id, workspace_id, content, embedding, json.dumps(metadata)
        )
    
    async def search(self, user_id: str, workspace_id: str, query: str, limit: int = 5):
        query_embedding = self.encoder.encode(query).tolist()
        
        results = await self.db.fetch(
            """SELECT content, metadata, 1 - (embedding <=> $1) AS similarity
               FROM memory_embeddings
               WHERE user_id = $2 AND workspace_id = $3
               ORDER BY embedding <=> $1
               LIMIT $4""",
            query_embedding, user_id, workspace_id, limit
        )
        
        return [dict(r) for r in results]
```

---

## 5. Source Code Structure

### 5.1 Monorepo Layout

```
librebird-platform/
├── packages/
│   ├── gateway/                # Node.js/TypeScript
│   │   ├── src/
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   └── Dockerfile
│   │
│   ├── brain/                  # Python
│   │   ├── server.py
│   │   ├── llm_engine.py       # Enhanced
│   │   ├── skill_loader.py     # From current codebase
│   │   ├── memory/
│   │   ├── providers/
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   │
│   ├── hands/                  # Python + Docker
│   │   ├── server.py
│   │   ├── executor.py
│   │   ├── skills/             # Migrated from current codebase
│   │   ├── sandbox/
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   │
│   ├── shared/                 # Shared TypeScript types
│   │   ├── types.ts
│   │   └── proto/
│   │       ├── brain.proto
│   │       └── hands.proto
│   │
│   ├── web/                    # React/Next.js web app
│   │   ├── src/
│   │   ├── public/
│   │   └── package.json
│   │
│   ├── mobile-ios/             # Swift/SwiftUI
│   │   ├── LibreBird.xcodeproj
│   │   └── Sources/
│   │
│   ├── mobile-android/         # Kotlin/Compose
│   │   ├── app/
│   │   └── build.gradle
│   │
│   └── cli/                    # @librebird/cli
│       ├── src/
│       └── package.json
│
├── skills/                     # Community skills (Git submodules)
│   ├── official/
│   │   ├── core/               # Migrated from current codebase
│   │   ├── productivity/
│   │   └── ...
│   └── community/
│       └── ...
│
├── charts/                     # Helm charts
│   └── librebird/
│
├── docker-compose.yml
├── .env.example
├── package.json                # Root workspace config (npm/yarn/pnpm)
└── README.md
```

### 5.2 Reuse Strategy

**Preserve from current codebase**:
- ✅ `skills/` (26 skills / 101 tools) → Move to `hands/skills/`
- ✅ `skill_loader.py` → Keep in `brain/` and `hands/`
- ✅ `llm_engine.py` → Enhance with multi-provider in `brain/`
- ✅ `agent_modes.py` → Keep in `brain/`
- ✅ `database.py` → Abstract to support PostgreSQL
- ✅ `memory.py` → Enhance with pgvector in `brain/memory/`
- ✅ `context_collector.py` → Keep for desktop, adapt for cross-platform
- ✅ `proactive.py` → Keep in `brain/`, make channel-aware
- ✅ `notifications.py` → Refactor to use Gateway push
- ✅ `tts.py`, `voice_input.py` → Platform-specific, keep for desktop

**Refactor/Replace**:
- ❌ `server.py` → Split into `gateway/` and `brain/server.py`
- ❌ `app.py` (pywebview) → Becomes standalone desktop client
- ❌ `frontend/` → Rebuild as React app in `packages/web/`

---

## 6. Delivery Phases

### Phase 1: Architecture Foundation (Months 1-3)

**Goal**: Gateway/Brain/Hands microservices working locally

**Deliverables**:
- [ ] Gateway service (Node.js) with WebSocket + gRPC
- [ ] Brain gRPC wrapper around existing `llm_engine.py`
- [ ] Hands gRPC service with Docker sandboxing
- [ ] PostgreSQL schema + migration script (SQLite → Postgres)
- [ ] Docker Compose setup for local development
- [ ] All 26 existing skills working in new architecture

**Success Criteria**:
- Existing desktop app can switch to new backend (backward compatible)
- Single-user mode works identically to current version
- All integration tests passing

### Phase 2: Multi-User + Web Dashboard (Months 4-6)

**Goal**: Multi-user system with web interface

**Deliverables**:
- [ ] User authentication (JWT + OAuth2)
- [ ] Workspaces & RBAC
- [ ] Web app (React + WebSocket)
- [ ] User settings & skill management UI
- [ ] Data isolation (RLS policies)
- [ ] Multi-provider LLM support (OpenAI, Anthropic, Google)
- [ ] Vector memory (pgvector RAG)

**Success Criteria**:
- 3+ users can collaborate in a workspace
- Web UI feature parity with desktop app
- RAG memory retrieval working (<500ms p95)

### Phase 3: Channel Integrations (Months 7-9)

**Goal**: Multi-channel support (WhatsApp, Telegram, Discord)

**Deliverables**:
- [ ] Channel adapter framework
- [ ] WhatsApp adapter (Twilio)
- [ ] Telegram bot adapter
- [ ] Discord bot adapter
- [ ] Unified message format handling
- [ ] Channel-specific features (buttons, media, voice)

**Success Criteria**:
- Users can chat via 3+ channels simultaneously
- Messages sync across channels in real-time
- Voice messages transcribed via Whisper

### Phase 4: Mobile + Marketplace (Months 10-12)

**Goal**: Mobile apps and plugin ecosystem

**Deliverables**:
- [ ] iOS app (Swift/SwiftUI) with push notifications
- [ ] Android app (Kotlin/Compose)
- [ ] Enhanced skill manifest format
- [ ] Marketplace backend (skill registry + S3 storage)
- [ ] Marketplace web UI (browse, install, rate)
- [ ] CLI tool (`@librebird/cli`) for skill development
- [ ] Security scanner for community skills
- [ ] Kubernetes Helm chart
- [ ] Documentation site

**Success Criteria**:
- Mobile apps in TestFlight/Play Store beta
- 10+ community skills published to marketplace
- Kubernetes deployment tested on AWS/GCP

---

## 7. Verification Approach

### 7.1 Testing Strategy

**Unit Tests**:
- Coverage target: >80% for core services
- Tools: Jest (Gateway), pytest (Brain/Hands)

**Integration Tests**:
- Gateway ↔ Brain ↔ Hands RPC communication
- End-to-end message flow (user input → LLM → tool execution → response)
- Database migrations (SQLite → PostgreSQL)

**Load Tests**:
- 1000 concurrent WebSocket connections
- LLM streaming latency <500ms p95
- Tool execution <2s p95

**Security Tests**:
- Penetration testing (OWASP Top 10)
- Sandbox escape attempts (Hands containers)
- JWT token validation

### 7.2 Linting & Type Checking

**TypeScript** (Gateway):
```bash
npm run lint      # ESLint + Prettier
npm run typecheck # tsc --noEmit
```

**Python** (Brain/Hands):
```bash
ruff check .           # Fast linter
mypy brain/ hands/     # Type checking
```

### 7.3 CI/CD Pipeline

**GitHub Actions**:
```yaml
# .github/workflows/ci.yml
name: CI

on: [push, pull_request]

jobs:
  test-gateway:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
      - run: npm ci
        working-directory: packages/gateway
      - run: npm run lint
        working-directory: packages/gateway
      - run: npm test
        working-directory: packages/gateway

  test-brain:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
        working-directory: packages/brain
      - run: ruff check .
        working-directory: packages/brain
      - run: pytest
        working-directory: packages/brain

  integration-tests:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_PASSWORD: test
      redis:
        image: redis:7-alpine
    steps:
      - uses: actions/checkout@v4
      - run: docker-compose -f docker-compose.test.yml up --abort-on-container-exit
```

---

## 8. Risk Mitigation

### 8.1 Technical Risks

| Risk | Mitigation |
|------|------------|
| **Performance degradation** (microservices overhead) | Use gRPC for low-latency RPC; benchmark early; keep local mode for single-user |
| **Complexity explosion** | Strict interface contracts; comprehensive docs; start simple (Docker Compose before K8s) |
| **Data migration bugs** | Extensive testing; backup-before-migrate; rollback plan; dual-database support |
| **Sandbox escapes** (Hands security) | Regular security audits; resource limits; container hardening; least privilege |
| **LLM API costs** | Default to local models; user quotas; cost alerts; optimize prompts |

### 8.2 Product Risks

| Risk | Mitigation |
|------|------------|
| **Desktop users feel abandoned** | Desktop app remains first-class; feature parity; self-hosted option |
| **Community skill adoption slow** | Seed marketplace with 20+ official skills; incentivize early publishers |
| **Mobile apps underused** | Focus on high-value mobile-specific features (push, voice, camera) |
| **Competition from OpenClaw/others** | Differentiate on privacy, open source, screen context, skill count |

---

## 9. Open Questions & Decisions Needed

1. **Gateway Language**: Node.js/TypeScript (recommended) vs Go (higher performance)?
2. **Vector DB**: pgvector (simpler) vs ChromaDB/Qdrant (more features)?
3. **Message Queue**: Redis Streams (lightweight) vs RabbitMQ (robust)?
4. **Mobile Priority**: iOS-first, Android-first, or parallel development?
5. **Cloud Hosting**: AWS (de facto standard) vs GCP (better ML tools) vs cloud-agnostic?
6. **Business Model**: Fully open source vs open-core with pro features?

**Recommended Defaults** (for immediate start):
- Gateway: **Node.js/TypeScript** (aligns with OpenClaw, mature ecosystem)
- Vector DB: **pgvector** (simpler deployment, one less service)
- Queue: **Redis Streams + BullMQ** (already using Redis for sessions)
- Mobile: **iOS first** (higher-value users, easier distribution)
- Cloud: **Cloud-agnostic** (Terraform + Docker, deploy anywhere)
- Business: **Fully open source** (MIT), optional managed cloud later

---

## 10. Success Metrics

### 10.1 Technical KPIs

- **Latency**: Gateway → Brain first token <500ms (p95)
- **Throughput**: 1000+ concurrent users per Gateway instance
- **Uptime**: 99.9% for self-hosted (monitoring via Prometheus)
- **Test Coverage**: >80% for core services
- **Build Time**: <10 minutes for full CI pipeline

### 10.2 Product KPIs

- **Adoption**: 10k users in first 6 months
- **Engagement**: 50+ messages/user/month
- **Retention**: >70% month-1 retention
- **Ecosystem**: 100+ community skills in first year
- **Channels**: 50% of users connect non-web channels

---

## 11. References

### 11.1 Technologies

- **Gateway**: [Fastify](https://fastify.dev), [Socket.io](https://socket.io), [BullMQ](https://docs.bullmq.io)
- **Brain**: [FastAPI](https://fastapi.tiangolo.com), [gRPC Python](https://grpc.io/docs/languages/python/), [llama-cpp-python](https://github.com/abetlen/llama-cpp-python)
- **Database**: [PostgreSQL](https://postgresql.org), [pgvector](https://github.com/pgvector/pgvector)
- **Deployment**: [Docker Compose](https://docs.docker.com/compose/), [Kubernetes](https://kubernetes.io), [Helm](https://helm.sh)

### 11.2 Existing Codebase

Key files to reference during implementation:
- [`server.py`](./server.py) - Current FastAPI backend (split into Gateway + Brain)
- [`llm_engine.py`](./llm_engine.py) - LLM inference engine (enhance with multi-provider)
- [`skill_loader.py`](./skill_loader.py) - Skill discovery system (keep as-is)
- [`database.py`](./database.py) - Database layer (abstract for PostgreSQL)
- [`skills/*/skill.json`](./skills/) - Existing skill manifests (migrate format)

---

## 12. Next Steps

After approval of this specification:

1. **Create detailed implementation plan** (task breakdown for each phase)
2. **Set up project scaffolding** (monorepo, CI/CD, Docker configs)
3. **Begin Phase 1**: Gateway + Brain + Hands microservices
4. **Weekly progress reviews** (demo working features each week)
5. **Continuous user feedback** (beta program with current Libre Bird users)

**Estimated Start Date**: After plan approval  
**Phase 1 Target Completion**: 3 months from start  
**Full Platform Launch (v1.0)**: 12 months from start

---

*This specification is a living document and will be updated as implementation progresses and new requirements emerge.*
