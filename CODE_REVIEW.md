# Kestrel - Full Project Code Review

## Context

Kestrel is a privacy-first, self-hostable AI agent platform with a microservices architecture: a **Node.js/TypeScript Gateway** (HTTP/WebSocket API), a **Python Brain** (LLM orchestration, memory, persistence via gRPC), and **Python Hands** (sandboxed tool execution via gRPC). It includes a React/Vite frontend, PostgreSQL+pgvector for storage, Redis for sessions, and 27+ built-in skills. ~21,500 lines of code total.

---

## What Works Well

### 1. Architecture & Separation of Concerns
The three-service split (Gateway, Brain, Hands) is clean and well-motivated. Each service has a clear responsibility. gRPC contracts (`brain.proto`, `hands.proto`) are thorough — 20+ message types, 11 Brain RPCs, 3 Hands RPCs. This is a solid foundation for a project of this ambition.

### 2. Database Schema (10 migrations)
The schema is mature and well-designed:
- Proper UUIDs, timestamps, foreign keys with CASCADE
- JSONB for flexible metadata
- pgvector for semantic memory
- Indexes on frequently queried columns
- RLS policies were designed (though later disabled — see issues)

### 3. Environment Configuration
`.env.example` is comprehensive (171 lines) covering every service, 11 feature flags, monitoring hooks, and OAuth. Good use of Docker Compose variable interpolation.

### 4. Docker Setup
Multi-stage Dockerfiles for all services, proper health checks, volume persistence, correct startup ordering. This is well done for local development.

### 5. Protobuf Contracts
Well-defined service boundaries. The proto files serve as living documentation of the API surface between services.

### 6. TypeScript Strictness
Gateway and web packages use strict TypeScript with ESLint. Good choice of modern tooling (Fastify, Vite, vitest).

### 7. Skills Library
27+ skills covering a wide range of capabilities (browser automation, email, GitHub, SSH, system monitoring, etc.). Each skill has a `manifest.json` with clear metadata.

### 8. Documentation
README.md and SETUP.md provide reasonable onboarding. Architecture diagram, service table, and security features are all documented.

---

## What Doesn't Work Well

### 9. Test Coverage (~5-10%) — SEVERELY LACKING
Only **8 test files** across the entire project:
- Gateway: `auth.test.ts` (9 tests), `session.test.ts` (7 tests)
- Brain: `test_providers.py` (6 tests), `test_user_management.py` (6 tests)
- Hands: `test_permissions.py` (3 tests), `test_skill_loader.py` (5 tests)
- Web: **zero tests**, no test script in package.json

**Not tested at all:** gRPC handlers, conversation/message CRUD, vector memory, Docker sandbox execution, any route handler, any channel adapter, any integration between services.

### 10. No CI/CD — Zero Automation
No `.github/workflows/`, no Jenkinsfile, no GitLab CI — nothing. Linting, type-checking, and tests are never enforced automatically. Build failures can go unnoticed.

### 11. Pervasive `any` Casts in Gateway
Critical code paths use `(req as any).user`, `req.body as any`, `req.params as any` throughout routes (`tasks.ts:27-29`, `auth.ts`, `workspaces.ts`). This defeats TypeScript's safety and hides bugs.

### 12. No Input Validation Framework
User input is checked ad-hoc (e.g., `goal.trim().length < 3` in `tasks.ts:31`) but there's no schema validation library (zod, joi, ajv). No max-length checks on workspace names, no password complexity rules, no request body schemas.

### 13. Missing Rate Limiting
No rate limiting on any endpoint, including authentication (`/api/auth/login`, `/api/auth/register`, magic link requests). Vulnerable to brute force.

### 14. No Formatting Tool
ESLint is configured but no Prettier. No pre-commit hooks (husky/lint-staged). Code style enforcement is manual.

### 15. Legacy Frontend Cruft
`/packages/gateway/frontend/` contains an abandoned frontend (raw HTML/JS). Not referenced anywhere, not built, but still in the repo alongside the real frontend at `/packages/web/`. Confusing for contributors.

### 16. Feature Flags Not Consistently Checked
`.env.example` defines 11 feature flags (`ENABLE_OAUTH`, `ENABLE_VECTOR_MEMORY`, etc.) but code doesn't always check them before initializing the corresponding subsystems.

### 17. Not Production-Ready
- No SSL/TLS configuration
- No Kubernetes manifests or production deployment tooling
- No database backup strategy
- No log aggregation setup
- Prometheus metrics defined in env but no Prometheus/Grafana in docker-compose
- Nginx config missing security headers (X-Frame-Options, CSP, etc.)

---

## What Is Broken / Critical Security Issues

### 18. CRITICAL: SHA-256 Password Hashing (`brain/server.py:87`)
```python
pw_hash = hashlib.sha256((password + salt).encode()).hexdigest()
```
SHA-256 is **not** a password hashing algorithm. It's fast by design, making it trivially brute-forceable with GPUs. Must use bcrypt or argon2.

### 19. CRITICAL: API Key Encryption Not Implemented (`brain/provider_config.py:42`)
```python
"api_key": row["api_key_encrypted"] or "",  # TODO: decrypt
```
The database column is named `api_key_encrypted` but decryption was never implemented. API keys (OpenAI, Anthropic, etc.) are stored and returned in plain text. The `TODO` comment confirms this is known but unfixed.

### 20. HIGH: Hardcoded Default JWT Secret (`gateway/src/server.ts:32`)
```typescript
jwtSecret: process.env.JWT_SECRET || 'dev-secret-change-me',
```
If `JWT_SECRET` isn't set, anyone who knows this string (it's in the public repo) can forge authentication tokens. The server should refuse to start without an explicit secret in production.

### 21. HIGH: JWT Token in WebSocket Query String (`web/src/api/client.ts:183`)
```typescript
const wsUrl = `${protocol}//${window.location.host}/ws?token=${accessToken}`;
```
Tokens in query strings are logged in server access logs, browser history, proxy logs, and referrer headers. Should use a post-connection auth handshake instead.

### 22. HIGH: Tokens Stored in localStorage (`web/src/api/client.ts`)
JWT access and refresh tokens stored in `localStorage`, which is accessible to any XSS attack. Any script injection on the page can steal all auth tokens.

### 23. HIGH: Migration Number Conflict
Two files share the `009_` prefix:
- `009_disable_rls.sql`
- `009_memory_graph.sql`

Docker's init scripts run them alphabetically, so `disable_rls` runs first (disabling RLS), then `memory_graph` creates new tables without RLS. But this is fragile — renaming either file or changing init behavior could break the schema. The second file should be `010_memory_graph.sql`.

### 24. MEDIUM: RLS Globally Disabled (`009_disable_rls.sql`)
Row-Level Security policies were carefully designed in migration 001, then entirely disabled in migration 009. The comment says "application layer handles isolation" — but this means every single query in Brain must correctly filter by workspace. One missed `WHERE workspace_id = $X` clause and data leaks between tenants.

### 25. MEDIUM: No JSON Parse Error Handling
`JSON.parse()` calls in several gateway routes lack try-catch. Malformed input causes unhandled exceptions instead of 400 responses.

### 26. MEDIUM: Brain gRPC Failure Doesn't Block Startup (`gateway/src/brain/client.ts:35-46`)
Gateway starts successfully even if Brain is unreachable. The system appears healthy but every chat request will fail. Should at minimum log a prominent warning or expose this in the health check.

---

## Recommended Fix Priority

If you want me to implement fixes, here's the order I'd recommend:

| Priority | Issue | Effort |
|----------|-------|--------|
| 1 | Replace SHA-256 with bcrypt/argon2 for passwords (#18) | Small |
| 2 | Implement API key encryption (#19) | Medium |
| 3 | Fail on missing JWT_SECRET in production (#20) | Small |
| 4 | Fix migration numbering conflict (#23) | Trivial |
| 5 | Move WS token out of query string (#21) | Medium |
| 6 | Add request body validation (zod/joi) (#12) | Medium |
| 7 | Add rate limiting to auth endpoints (#13) | Small |
| 8 | Replace `any` casts with proper types (#11) | Medium |
| 9 | Add CI/CD pipeline (#10) | Medium |
| 10 | Increase test coverage (#9) | Large |
| 11 | Remove legacy frontend (#15) | Trivial |
| 12 | Add Prettier + pre-commit hooks (#14) | Small |
