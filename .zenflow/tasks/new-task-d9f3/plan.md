# Full SDD workflow

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

---

## Workflow Steps

### [x] Step: Requirements
<!-- chat-id: 8c51549c-63d5-414b-aaf8-11b49aa32409 -->

Create a Product Requirements Document (PRD) based on the feature description.

1. Review existing codebase to understand current architecture and patterns
2. Analyze the feature definition and identify unclear aspects
3. Ask the user for clarifications on aspects that significantly impact scope or user experience
4. Make reasonable decisions for minor details based on context and conventions
5. If user can't clarify, make a decision, state the assumption, and continue

Save the PRD to `{@artifacts_path}/requirements.md`.

### [x] Step: Technical Specification
<!-- chat-id: a59aa048-70de-4c38-bcc3-1db0e4e7af24 -->

Create a technical specification based on the PRD in `{@artifacts_path}/requirements.md`.

1. Review existing codebase architecture and identify reusable components
2. Define the implementation approach

Save to `{@artifacts_path}/spec.md` with:
- Technical context (language, dependencies)
- Implementation approach referencing existing code patterns
- Source code structure changes
- Data model / API / interface changes
- Delivery phases (incremental, testable milestones)
- Verification approach using project lint/test commands

### [x] Step: Planning
<!-- chat-id: 10cb9db3-3857-4810-8cdb-5dfd14b4f6e3 -->

Create a detailed implementation plan based on `{@artifacts_path}/spec.md`.

1. Break down the work into concrete tasks
2. Each task should reference relevant contracts and include verification steps
3. Replace the Implementation step below with the planned tasks

Rule of thumb for step size: each step should represent a coherent unit of work (e.g., implement a component, add an API endpoint). Avoid steps that are too granular (single function) or too broad (entire feature).

Important: unit tests must be part of each implementation task, not separate tasks. Each task should implement the code and its tests together, if relevant.

If the feature is trivial and doesn't warrant full specification, update this workflow to remove unnecessary steps and explain the reasoning to the user.

Save to `{@artifacts_path}/plan.md`.

---

## Implementation Plan

This project transforms Libre Bird into a full-blown OpenClaw-like platform through 4 delivery phases over 12 months. Each phase builds incrementally and maintains backward compatibility.

### Timeline Overview
- **Phase 1** (Months 1-3): Architecture Foundation (Gateway/Brain/Hands)
- **Phase 2** (Months 4-6): Multi-User + Web Dashboard
- **Phase 3** (Months 7-9): Channel Integrations
- **Phase 4** (Months 10-12): Mobile + Marketplace

---

## Phase 1: Architecture Foundation (Months 1-3)

**Goal**: Establish Gateway/Brain/Hands microservices architecture with Docker Compose local deployment

### [ ] Step: Project scaffolding and monorepo setup
<!-- chat-id: 58ce7e22-3b4b-490e-9bea-dc56881d3f13 -->

Set up the monorepo structure with proper tooling and initial configurations.

**Tasks**:
- Create monorepo directory structure (`packages/gateway`, `packages/brain`, `packages/hands`, `packages/shared`)
- Set up root `package.json` with workspace configuration (npm/yarn/pnpm)
- Create shared protobuf definitions in `packages/shared/proto/` (brain.proto, hands.proto)
- Set up linting configs (ESLint for TypeScript, Ruff for Python)
- Create `.env.example` with all required environment variables
- Initialize Git repository with proper `.gitignore`
- Create basic `README.md` with architecture overview

**Verification**:
- [ ] All workspace packages resolve correctly
- [ ] Linters run successfully on empty projects
- [ ] Protobuf definitions compile successfully

### [ ] Step: Gateway service foundation (Node.js/TypeScript)
<!-- chat-id: 668e0b17-c708-414a-b414-e2e68b214552 -->

Implement the core Gateway service with WebSocket support and basic routing.

**Implementation**:
- Set up Fastify server with TypeScript in `packages/gateway/`
- Implement WebSocket server using `ws` library
- Create session management with Redis (ioredis client)
- Add JWT authentication middleware (Passport.js)
- Implement basic health check endpoint (`/health`)
- Create structured logging with Winston
- Add Prometheus metrics endpoints
- Implement gRPC client stub for Brain service
- Write unit tests for auth middleware and session management

**Files**:
- `packages/gateway/src/server.ts`
- `packages/gateway/src/auth/middleware.ts`
- `packages/gateway/src/session/manager.ts`
- `packages/gateway/src/brain/client.ts`
- `packages/gateway/src/utils/logger.ts`
- `packages/gateway/src/utils/metrics.ts`
- `packages/gateway/Dockerfile`
- `packages/gateway/tests/`

**Verification**:
- [ ] WebSocket server accepts connections
- [ ] Health check endpoint returns 200
- [ ] JWT tokens validated correctly
- [ ] Redis session storage working
- [ ] All unit tests passing (`npm test`)
- [ ] Lint and typecheck clean (`npm run lint`, `npm run typecheck`)

### [ ] Step: Brain service with gRPC wrapper
<!-- chat-id: 33c378c0-66d5-4597-a87f-e931e3257d06 -->

Refactor existing `llm_engine.py` to work as a gRPC service.

**Implementation**:
- Create `packages/brain/` structure from existing code
- Implement gRPC server in `brain/server.py` using grpcio
- Create `BrainService` implementing `StreamChat` and `ExecuteTool` RPCs
- Migrate existing `llm_engine.py` with minimal changes (keep local model support)
- Keep existing `skill_loader.py` and `agent_modes.py` as-is
- Add health check gRPC endpoint
- Write integration tests for gRPC communication
- Create Dockerfile with llama-cpp-python dependencies

**Files**:
- `packages/brain/server.py` (new)
- `packages/brain/llm_engine.py` (migrated)
- `packages/brain/skill_loader.py` (migrated)
- `packages/brain/agent_modes.py` (migrated)
- `packages/brain/requirements.txt`
- `packages/brain/Dockerfile`
- `packages/brain/tests/`

**Verification**:
- [ ] gRPC server starts successfully
- [ ] Existing local LLM inference works via gRPC
- [ ] Skill loading mechanism functional
- [ ] Gateway can communicate with Brain via gRPC
- [ ] All tests passing (`pytest`)
- [ ] Lint clean (`ruff check .`)

### [ ] Step: Hands service with Docker sandboxing
<!-- chat-id: 6598cd78-fa46-4252-9b92-b67893f5de23 -->

Create the Hands service for sandboxed tool execution.

**Implementation**:
- Create `packages/hands/` structure
- Migrate all 26 skills from `skills/` directory
- Implement gRPC server in `hands/server.py`
- Create Docker executor in `hands/executor.py` for container management
- Build sandbox Docker image (`hands/sandbox/Dockerfile`)
- Implement sandbox entrypoint (`hands/sandbox/entrypoint.py`)
- Add permission checking and audit logging (`hands/security/`)
- Implement resource limits (CPU, memory, timeout)
- Write integration tests for sandboxed execution
- Test all 26 existing skills in sandbox environment

**Files**:
- `packages/hands/server.py`
- `packages/hands/executor.py`
- `packages/hands/skills/` (migrated from root)
- `packages/hands/sandbox/Dockerfile`
- `packages/hands/sandbox/entrypoint.py`
- `packages/hands/security/allowlist.py`
- `packages/hands/security/audit.py`
- `packages/hands/Dockerfile`
- `packages/hands/tests/`

**Verification**:
- [ ] All 26 skills execute successfully in sandbox
- [ ] Resource limits enforced correctly
- [ ] Audit logs generated for tool executions
- [ ] gRPC communication working with Brain
- [ ] Security tests pass (no sandbox escapes)
- [ ] All tests passing (`pytest`)
- [ ] Lint clean (`ruff check .`)

### [ ] Step: PostgreSQL schema and migration tooling
<!-- chat-id: 6767c39b-3bfb-48e4-a15a-c4ce3331965a -->

Design multi-user database schema and create SQLite migration tools.

**Implementation**:
- Create PostgreSQL schema with user, workspace, and conversation tables
- Add pgvector extension for future RAG support
- Implement Row-Level Security (RLS) policies for data isolation
- Create database abstraction layer in `packages/brain/database.py`
- Support both SQLite (legacy) and PostgreSQL backends
- Write migration script (`scripts/migrate_sqlite_to_postgres.py`)
- Add database versioning and migration framework (Alembic)
- Create seed data script for testing
- Write tests for migration accuracy

**Files**:
- `packages/brain/migrations/001_initial_schema.sql`
- `packages/brain/migrations/002_add_vector_memory.sql`
- `packages/brain/database.py` (enhanced)
- `scripts/migrate_sqlite_to_postgres.py`
- `scripts/seed_test_data.py`
- `tests/test_migrations.py`

**Verification**:
- [ ] PostgreSQL schema created successfully
- [ ] SQLite to PostgreSQL migration preserves all data
- [ ] RLS policies enforce user isolation
- [ ] Database abstraction layer works with both backends
- [ ] Migration tests passing
- [ ] Rollback functionality verified

### [ ] Step: Docker Compose local development environment
<!-- chat-id: a81324b5-65df-4313-88e8-20b84261f24a -->

Create Docker Compose setup for running all services locally.

**Implementation**:
- Create `docker-compose.yml` with Gateway, Brain, Hands, Redis, PostgreSQL
- Add `docker-compose.dev.yml` for development with volume mounts
- Configure networking between services
- Set up environment variable templating
- Add health checks for all services
- Create startup script for initial setup
- Write integration tests using Docker Compose
- Document local development workflow

**Files**:
- `docker-compose.yml`
- `docker-compose.dev.yml`
- `docker-compose.test.yml`
- `.env.example`
- `scripts/docker-start.sh`
- `docs/local-development.md`

**Verification**:
- [ ] All services start with `docker-compose up`
- [ ] Inter-service communication working
- [ ] Health checks passing
- [ ] Can send message through full pipeline (Gateway → Brain → Hands)
- [ ] Integration tests passing
- [ ] Services restart gracefully

### [ ] Step: Backward compatibility with desktop app
<!-- chat-id: 779c4304-36e8-4470-9752-1a183c44724c -->

Ensure existing macOS desktop app can use new backend.

**Implementation**:
- Create compatibility API endpoints in Gateway
- Add backward-compatible authentication (optional JWT)
- Map old REST endpoints to new microservices
- Test existing desktop app (`app.py`) with new backend
- Create migration guide for desktop users
- Add feature flag for enabling/disabling new architecture
- Write compatibility tests

**Files**:
- `packages/gateway/src/compat/desktop.ts`
- `docs/desktop-migration.md`
- `tests/compat/test_desktop_app.py`

**Verification**:
- [ ] Desktop app connects to Gateway successfully
- [ ] All existing features work without modification
- [ ] Performance comparable to monolithic version
- [ ] No breaking changes for existing users
- [ ] Compatibility tests passing

---

## Phase 2: Multi-User + Web Dashboard (Months 4-6)

**Goal**: Add multi-user support, authentication, workspaces, and web interface

### [ ] Step: User authentication and authorization system
<!-- chat-id: c0ca772a-6a66-4f9c-bb9a-5256c5125b34 -->

Implement JWT-based auth with multiple strategies.

**Implementation**:
- Create user registration and login endpoints
- Implement bcrypt password hashing
- Add JWT token generation and validation
- Implement OAuth2 support (Google, GitHub)
- Create magic link passwordless authentication
- Add API key generation for programmatic access
- Implement refresh token mechanism
- Create RBAC system (owner, admin, member, guest roles)
- Write comprehensive auth tests

**Files**:
- `packages/gateway/src/auth/strategies/jwt.ts`
- `packages/gateway/src/auth/strategies/oauth.ts`
- `packages/gateway/src/auth/strategies/magic-link.ts`
- `packages/gateway/src/auth/rbac.ts`
- `packages/gateway/src/routes/auth.ts`
- `packages/gateway/tests/auth/`

**Verification**:
- [ ] User registration and login working
- [ ] JWT tokens issued and validated correctly
- [ ] OAuth2 flow functional for Google and GitHub
- [ ] Magic links sent and verified
- [ ] RBAC permissions enforced correctly
- [ ] Security tests passing (no token leaks, proper hashing)
- [ ] All unit tests passing

### [ ] Step: Workspace management and team collaboration
<!-- chat-id: b3f20b09-4bd2-454d-80c4-f1731cb9a676 -->

Implement workspace system for team collaboration.

**Implementation**:
- Create workspace CRUD endpoints
- Implement workspace membership management
- Add workspace invitation system (email invites)
- Create workspace switching in Gateway
- Add workspace-scoped data queries
- Implement workspace settings and preferences
- Create workspace analytics dashboard
- Write workspace isolation tests

**Files**:
- `packages/gateway/src/routes/workspaces.ts`
- `packages/gateway/src/routes/invitations.ts`
- `packages/brain/workspace_manager.py`
- `packages/gateway/tests/workspaces/`

**Verification**:
- [ ] Users can create and join workspaces
- [ ] Workspace invitations sent and accepted
- [ ] Data properly isolated between workspaces
- [ ] Workspace settings persist correctly
- [ ] RLS policies enforced at database level
- [ ] All tests passing

### [ ] Step: Multi-provider LLM support (OpenAI, Anthropic, Google)
<!-- chat-id: 92c851ac-0787-4e50-ae68-48ac95b886e1 -->

Extend Brain to support cloud LLM providers.

**Implementation**:
- Create provider abstraction in `brain/providers/`
- Implement OpenAI provider (`providers/openai.py`)
- Implement Anthropic provider (`providers/anthropic.py`)
- Implement Google Gemini provider (`providers/google.py`)
- Add provider configuration per workspace/user
- Implement streaming for all providers
- Add usage tracking and quota management
- Create provider fallback mechanism
- Write provider integration tests

**Files**:
- `packages/brain/providers/base.py`
- `packages/brain/providers/local.py` (extracted from llm_engine)
- `packages/brain/providers/openai.py`
- `packages/brain/providers/anthropic.py`
- `packages/brain/providers/google.py`
- `packages/brain/llm_engine.py` (enhanced)
- `packages/brain/tests/providers/`

**Verification**:
- [ ] All providers support streaming responses
- [ ] Provider switching works seamlessly
- [ ] Usage quotas enforced correctly
- [ ] Fallback to local model when API fails
- [ ] Cost tracking accurate
- [ ] Integration tests passing for each provider

### [ ] Step: Vector memory (RAG) with pgvector
<!-- chat-id: 955f8907-c9b6-467b-8fad-76edbe792ba2 -->

Implement semantic memory and retrieval.

**Implementation**:
- Set up sentence-transformers for embeddings
- Create vector memory manager (`brain/memory/vector_store.py`)
- Implement automatic conversation embedding
- Add semantic search with time-weighted ranking
- Create memory retrieval API
- Implement memory cleanup policies (TTL, size limits)
- Add context injection into LLM prompts
- Optimize vector search performance (HNSW indexes)
- Write RAG integration tests

**Files**:
- `packages/brain/memory/vector_store.py`
- `packages/brain/memory/embeddings.py`
- `packages/brain/memory/retrieval.py`
- `packages/brain/tests/memory/`

**Verification**:
- [ ] Conversations automatically embedded
- [ ] Semantic search returns relevant results
- [ ] RAG context improves LLM responses
- [ ] Search latency <500ms p95
- [ ] Memory cleanup works correctly
- [ ] All tests passing

### [ ] Step: Web application (React + WebSocket)
<!-- chat-id: 6538114b-57ac-4ab7-bc06-c3e63b58df50 -->

Build modern web interface with real-time chat.

**Implementation**:
- Set up React + TypeScript project in `packages/web/`
- Create WebSocket client with auto-reconnect
- Implement chat UI with message streaming
- Build conversation management interface
- Create settings panel (profile, API keys, preferences)
- Add skill management UI (enable/disable, configure)
- Implement workspace switcher
- Create responsive design (mobile-friendly)
- Add dark mode support
- Write component tests (Jest + React Testing Library)

**Files**:
- `packages/web/src/components/Chat/`
- `packages/web/src/components/Settings/`
- `packages/web/src/components/Skills/`
- `packages/web/src/hooks/useWebSocket.ts`
- `packages/web/src/api/client.ts`
- `packages/web/src/App.tsx`
- `packages/web/tests/`

**Verification**:
- [ ] Real-time message streaming works
- [ ] WebSocket reconnection functional
- [ ] All CRUD operations working
- [ ] UI responsive on mobile
- [ ] Dark mode toggles correctly
- [ ] Component tests passing
- [ ] Lighthouse score >90

---

## Phase 3: Channel Integrations (Months 7-9)

**Goal**: Enable multi-channel support (WhatsApp, Telegram, Discord)

### [ ] Step: Channel adapter framework
<!-- chat-id: ce455c3f-13b9-4177-8a2d-f2692940cc26 -->

Create base infrastructure for channel integrations.

**Implementation**:
- Define `BaseChannelAdapter` interface in `gateway/channels/base.ts`
- Create unified message format converter
- Implement channel registry and lifecycle management
- Add channel-specific user ID mapping
- Create attachment handling abstraction
- Implement channel configuration management
- Write adapter framework tests

**Files**:
- `packages/gateway/src/channels/base.ts`
- `packages/gateway/src/channels/registry.ts`
- `packages/gateway/src/channels/message-converter.ts`
- `packages/gateway/tests/channels/`

**Verification**:
- [ ] Adapter interface well-defined
- [ ] Message conversion preserves content
- [ ] Channel lifecycle managed correctly
- [ ] Framework tests passing

### [ ] Step: WhatsApp adapter (Twilio)
<!-- chat-id: ec792562-5345-49e8-a87b-63138a8a8cb2 -->

Integrate WhatsApp Business API via Twilio.

**Implementation**:
- Create WhatsApp adapter extending `BaseChannelAdapter`
- Set up Twilio client configuration
- Implement webhook endpoint for incoming messages
- Add support for media messages (images, voice, documents)
- Implement voice message transcription (Whisper)
- Add WhatsApp-specific features (buttons, lists)
- Create user onboarding flow (link WhatsApp to account)
- Write WhatsApp integration tests

**Files**:
- `packages/gateway/src/channels/whatsapp.ts`
- `packages/gateway/src/routes/webhooks/whatsapp.ts`
- `packages/gateway/tests/channels/whatsapp.test.ts`

**Verification**:
- [ ] Messages sent and received via WhatsApp
- [ ] Media attachments handled correctly
- [ ] Voice messages transcribed
- [ ] User linking functional
- [ ] Integration tests passing

### [ ] Step: Telegram bot adapter
<!-- chat-id: 149fc192-fb0a-4d28-92fa-8e5d6a3cc52b -->

Create Telegram bot integration.

**Implementation**:
- Create Telegram adapter using `node-telegram-bot-api`
- Implement polling and webhook modes
- Add support for inline keyboards and buttons
- Handle Telegram-specific features (commands, stickers)
- Implement file uploads and downloads
- Create bot registration flow
- Add group chat support
- Write Telegram integration tests

**Files**:
- `packages/gateway/src/channels/telegram.ts`
- `packages/gateway/src/routes/webhooks/telegram.ts`
- `packages/gateway/tests/channels/telegram.test.ts`

**Verification**:
- [ ] Bot responds to commands and messages
- [ ] Inline keyboards functional
- [ ] File sharing works
- [ ] Group chats supported
- [ ] Integration tests passing

### [ ] Step: Discord bot adapter
<!-- chat-id: cb767cb9-ce0f-4fe0-b08e-ae63c83a04de -->

Implement Discord integration.

**Implementation**:
- Create Discord adapter using `discord.js`
- Implement slash commands
- Add support for embeds and rich messages
- Handle Discord permissions and roles
- Create server setup flow
- Add voice channel support (future consideration)
- Write Discord integration tests

**Files**:
- `packages/gateway/src/channels/discord.ts`
- `packages/gateway/tests/channels/discord.test.ts`

**Verification**:
- [ ] Bot joins servers and responds
- [ ] Slash commands registered and working
- [ ] Embeds render correctly
- [ ] Permissions enforced
- [ ] Integration tests passing

### [ ] Step: Cross-channel message synchronization
<!-- chat-id: 68fa2664-ee54-4326-948e-bbb1501bff15 -->

Ensure messages sync across all connected channels.

**Implementation**:
- Implement message routing logic in Gateway
- Create conversation merging for multi-channel users
- Add message deduplication
- Implement presence synchronization
- Create channel preference settings
- Add notification routing rules
- Write sync integration tests

**Files**:
- `packages/gateway/src/sync/router.ts`
- `packages/gateway/src/sync/deduplicator.ts`
- `packages/gateway/tests/sync/`

**Verification**:
- [ ] Messages appear across all channels
- [ ] No duplicate messages
- [ ] Presence status synced
- [ ] Notification preferences respected
- [ ] Sync tests passing

---

## Phase 4: Mobile + Marketplace (Months 10-12)

**Goal**: Launch mobile apps and plugin marketplace

### [ ] Step: Mobile backend APIs and push notifications
<!-- chat-id: 96916f8a-08f9-445d-bb59-54eaad789b02 -->

Prepare backend for mobile app support.

**Implementation**:
- Create mobile-specific REST endpoints
- Implement FCM/APNs push notification system
- Add device token registration endpoints
- Create offline sync queue API
- Implement background job system for push delivery
- Add mobile analytics tracking
- Write mobile API tests

**Files**:
- `packages/gateway/src/routes/mobile.ts`
- `packages/gateway/src/push/fcm.ts`
- `packages/gateway/src/push/apns.ts`
- `packages/gateway/tests/mobile/`

**Verification**:
- [ ] Push notifications delivered successfully
- [ ] Offline sync working
- [ ] Device registration functional
- [ ] API tests passing

### [ ] Step: iOS app (Swift/SwiftUI)

Build native iOS application.

**Implementation**:
- Create Xcode project in `packages/mobile-ios/`
- Implement MVVM architecture with Combine
- Create WebSocket client with auto-reconnect
- Build SwiftUI chat interface
- Implement CoreData for local persistence
- Add push notification handling
- Implement biometric authentication (Face ID/Touch ID)
- Add Siri shortcuts support
- Create app icons and launch screens
- Write UI tests (XCTest)

**Files**:
- `packages/mobile-ios/Sources/`
- `packages/mobile-ios/Tests/`
- `packages/mobile-ios/LibreBird.xcodeproj`

**Verification**:
- [ ] App builds and runs on iOS 15+
- [ ] Real-time chat functional
- [ ] Offline mode works
- [ ] Push notifications received
- [ ] Biometric auth working
- [ ] UI tests passing
- [ ] TestFlight beta deployed

### [ ] Step: Android app (Kotlin/Compose)

Build native Android application.

**Implementation**:
- Create Android project in `packages/mobile-android/`
- Set up Jetpack Compose UI
- Implement MVVM with Kotlin Coroutines
- Create WebSocket client using OkHttp
- Build Material Design 3 chat interface
- Implement Room database for local storage
- Add FCM push notification support
- Create WorkManager background sync
- Implement biometric authentication
- Add app widgets and quick tiles
- Write instrumented tests (Espresso)

**Files**:
- `packages/mobile-android/app/`
- `packages/mobile-android/app/src/test/`
- `packages/mobile-android/build.gradle`

**Verification**:
- [ ] App builds and runs on Android 8+
- [ ] Real-time chat functional
- [ ] Offline sync working
- [ ] Push notifications received
- [ ] Biometric auth working
- [ ] Instrumented tests passing
- [ ] Play Store beta deployed

### [ ] Step: Enhanced skill SDK and CLI tooling
<!-- chat-id: 7e5551b0-83b0-4b7e-9bfe-8a59c544bfc4 -->

Create developer tools for skill creation.

**Implementation**:
- Build `@librebird/cli` package
- Implement `create-skill` scaffolding command
- Add skill validation and linting
- Create local development mode (`lb dev`)
- Implement skill testing framework
- Add skill publishing workflow
- Create skill documentation generator
- Write comprehensive SDK documentation

**Files**:
- `packages/cli/src/commands/create-skill.ts`
- `packages/cli/src/commands/test-skill.ts`
- `packages/cli/src/commands/publish-skill.ts`
- `packages/cli/tests/`
- `docs/skill-development.md`

**Verification**:
- [ ] Can scaffold new skill with `lb create-skill`
- [ ] Skill validation catches errors
- [ ] Local dev mode works
- [ ] Publishing flow functional
- [ ] CLI tests passing
- [ ] Documentation clear and complete

### [ ] Step: Marketplace backend and security scanner
<!-- chat-id: acebece0-369e-4e34-9f18-1ac93979d7f4 -->

Build skill marketplace infrastructure.

**Implementation**:
- Create marketplace database schema (skills, versions, reviews, ratings)
- Implement skill registry API endpoints
- Add S3 storage for skill packages
- Create security scanner (static analysis, dependency checks)
- Implement manual review workflow
- Add skill versioning and changelog support
- Create download and install API
- Implement usage analytics
- Write marketplace API tests

**Files**:
- `packages/marketplace/src/routes/skills.ts`
- `packages/marketplace/src/security/scanner.ts`
- `packages/marketplace/src/storage/s3.ts`
- `packages/marketplace/tests/`

**Verification**:
- [ ] Skills can be published and discovered
- [ ] Security scanner detects vulnerabilities
- [ ] Versioning works correctly
- [ ] Downloads tracked accurately
- [ ] API tests passing

### [ ] Step: Marketplace web UI
<!-- chat-id: 426948c3-afbc-49d0-a956-39182abb5426 -->

Create user-facing marketplace interface.

**Implementation**:
- Build marketplace section in web app
- Create skill browsing and search UI
- Implement skill detail pages with reviews
- Add one-click skill installation
- Create skill management dashboard
- Implement rating and review system
- Add category and tag filtering
- Create featured skills section
- Write UI tests

**Files**:
- `packages/web/src/pages/Marketplace/`
- `packages/web/src/components/SkillCard/`
- `packages/web/tests/marketplace/`

**Verification**:
- [ ] Can browse and search skills
- [ ] Skill installation works
- [ ] Reviews and ratings functional
- [ ] Search performance good (<200ms)
- [ ] UI tests passing

### [ ] Step: Kubernetes deployment and Helm charts
<!-- chat-id: ffe0963f-9558-460d-880d-d5134adb41e7 -->

Prepare for cloud deployment.

**Implementation**:
- Create Helm chart structure in `charts/librebird/`
- Define Kubernetes deployments for all services
- Create service definitions and ingress rules
- Implement horizontal pod autoscaling (HPA)
- Add persistent volume claims for databases
- Create ConfigMaps and Secrets management
- Implement health checks and readiness probes
- Add Prometheus monitoring integration
- Write deployment documentation

**Files**:
- `charts/librebird/Chart.yaml`
- `charts/librebird/values.yaml`
- `charts/librebird/templates/`
- `docs/kubernetes-deployment.md`

**Verification**:
- [ ] Helm chart installs successfully
- [ ] All services running in cluster
- [ ] Autoscaling triggers correctly
- [ ] Health checks passing
- [ ] Metrics collected in Prometheus
- [ ] Tested on AWS/GCP/Azure

### [ ] Step: Documentation and launch preparation

Finalize documentation and prepare for v1.0 launch.

**Implementation**:
- Create comprehensive README.md
- Write architecture documentation
- Create API reference documentation
- Build user guides (getting started, tutorials)
- Create video walkthroughs
- Write deployment guides (Docker, Kubernetes)
- Create troubleshooting guide
- Write contributing guidelines
- Create security policy and disclosure process
- Set up community channels (Discord, forum)

**Files**:
- `README.md`
- `docs/architecture.md`
- `docs/api-reference.md`
- `docs/user-guide.md`
- `docs/deployment/`
- `CONTRIBUTING.md`
- `SECURITY.md`

**Verification**:
- [ ] Documentation complete and accurate
- [ ] All guides tested by external reviewers
- [ ] API docs auto-generated and current
- [ ] Community channels active
- [ ] Security process documented

---

## Testing and Quality Assurance

Throughout all phases, maintain:

### Continuous Integration
- Run linters on every commit (ESLint, Ruff)
- Execute unit tests in CI pipeline
- Run integration tests on PR
- Check test coverage (>80% target)
- Perform security scanning

### Performance Testing
- Load test Gateway (1000+ concurrent WebSocket connections)
- Benchmark LLM latency (<500ms p95 first token)
- Test database query performance
- Profile memory usage under load
- Test mobile app performance

### Security Audits
- Regular dependency updates
- Penetration testing (OWASP Top 10)
- Sandbox escape testing for Hands
- JWT token security validation
- Database RLS policy verification

---

## Risk Mitigation Notes

- **Backward Compatibility**: Desktop app must continue working throughout Phase 1
- **Data Safety**: All migrations must have rollback capability
- **Incremental Delivery**: Each phase should be independently deployable and usable
- **Community Feedback**: Beta program with current users after each phase
- **Documentation First**: Write docs alongside code, not after
