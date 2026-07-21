
"""Large-scale synthetic memory corpus for stress-testing retrieval.

Scales the original benchmark to ~500 documents and ~150 queries across
semantic, episodic, and procedural layers with heavy distractor injection.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any


@dataclass
class MemoryQuery:
    query: str
    expected_doc_ids: list[str]
    layer: str
    description: str


@dataclass
class MemoryCorpus:
    documents: list[dict[str, Any]]
    queries: list[MemoryQuery]


# --- Semantic: large API/product fact base ---
SEMANTIC_TOPICS = [
    ("API base URL", "The {product} API base URL is https://api.{product}.dev/v2 and requires an X-API-Key header."),
    ("token expiry", "Authentication tokens expire after {n} seconds and must be refreshed using the /auth/refresh endpoint."),
    ("batch ingest", "The batch ingest endpoint POST /v2/batch accepts up to {n} records per request with a {size}MB payload limit."),
    ("rate limits", "Rate limits are {n} requests per minute for standard keys and {m} per minute for enterprise keys."),
    ("webhook signatures", "Webhook signatures use HMAC-SHA256 with the secret provided in the dashboard settings panel."),
    ("rate limit error", "The Python SDK raises {product}RateLimitError when status 429 is returned with Retry-After header."),
    ("retry policy", "Default retry policy uses exponential backoff starting at {n} second with a maximum of {m} retries."),
    ("pagination", "Pagination uses cursor-based links. The next_cursor field is present when more results exist."),
    ("search filters", "The search endpoint supports filtering by created_at, tags, and status using query parameters."),
    ("file uploads", "File uploads go to POST /v2/files and return a file_id used for async processing callbacks."),
    ("semantic versioning", "Project {product} uses semantic versioning. Breaking changes bump the major version."),
    ("GraphQL deprecation", "The GraphQL gateway is deprecated and will be removed in version {n}.0 scheduled for {quarter}."),
    ("database migrations", "Database migrations are managed with {tool}. Run {tool} upgrade head before deploying."),
    ("environment variables", "Environment variables for production include DATABASE_URL, REDIS_URL, and SECRET_KEY."),
    ("worker queue", "The worker queue uses Redis Streams. Job handlers must be idempotent because retries are automatic."),
    ("Docker registry", "Docker images are published to ghcr.io/{product}/{product}-agent. Tags include latest, stable, and commit SHA."),
    ("testing", "Unit tests use {tool}. Integration tests require Docker Compose and take approximately {n} minutes."),
    ("config validation", "The configuration schema is validated with Pydantic v2 models in config.py at startup."),
    ("structured logging", "Structured logging outputs JSON lines to stdout. Log level is controlled with LOG_LEVEL."),
    ("health checks", "Health checks respond on /health with status 200 and a JSON body containing database and cache states."),
    ("caching", "Response caching uses Redis with a default TTL of {n} seconds for read-heavy endpoints."),
    ("feature flags", "Feature flags are evaluated with Unleash. Flags can be toggled per environment in the admin dashboard."),
    ("auth methods", "Supported authentication methods include API key, OAuth2, and JWT bearer tokens."),
    ("encryption", "Data at rest is encrypted with AES-256. Data in transit uses TLS 1.3."),
    ("regions", "Available regions include us-east-1, eu-west-1, and ap-southeast-1 with latency targets under 50ms."),
    ("compliance", "The platform is SOC 2 Type II certified and GDPR compliant with data residency controls."),
    ("monitoring", "Metrics are exported in Prometheus format on port 9090. Alerts route to PagerDuty for severity >= critical."),
    ("backups", "Automated backups run daily at 02:00 UTC with 30-day retention. Point-in-time recovery is available for 7 days."),
    ("limits", "Maximum payload size is {size}MB. Maximum connection duration is {n} minutes. Maximum concurrent streams is {m}."),
    ("SDKs", "Official SDKs are available for Python, TypeScript, Go, and Rust. Community SDKs exist for Ruby and PHP."),
]

# Distractor topics (other products/tech)
SEMANTIC_DISTRACTORS = [
    "The Falcon API uses OAuth2 flow with PKCE and requires a client_id registered in the portal.",
    "AWS Lambda has a 15-minute timeout and 10GB memory limit for ephemeral storage.",
    "PostgreSQL supports JSONB indexing with GIN operators for fast containment queries.",
    "React Server Components render on the server and stream HTML to the client boundary.",
    "Terraform state locking prevents concurrent modifications using DynamoDB or Consul backends.",
    "Kubernetes uses etcd for cluster state storage. The control plane requires at least 3 nodes for HA.",
    "Elasticsearch relevance scoring uses BM25 by default. Custom similarity plugins are supported.",
    "GraphQL federation allows combining multiple schemas into a unified gateway with @key directives.",
    "Stripe webhooks include a Stripe-Signature header for verifying event authenticity.",
    "Docker multi-stage builds reduce final image size by separating build dependencies from runtime.",
    "Redis Cluster shards data across nodes using hash slots. Resharding requires manual migration.",
    "gRPC uses Protocol Buffers for serialization and HTTP/2 for transport with bi-directional streaming.",
    "Kafka partitions are the unit of parallelism. Consumers in the same group divide partitions evenly.",
    "Caddy server supports automatic HTTPS via Let's Encrypt with zero configuration.",
    "Supabase Realtime broadcasts database changes over WebSockets using Elixir and Phoenix.",
    "Next.js App Router uses React Server Components by default. Client components need the 'use client' directive.",
    "Vercel Edge Functions run in V8 isolates with a 50MB memory limit and 30-second execution timeout.",
    "Cloudflare Workers use the Service Worker API. KV storage has eventual consistency with 60-second global propagation.",
    "Tailwind CSS uses utility classes generated at build time. The JIT engine only includes used styles.",
    "Prisma ORM generates type-safe database clients from a schema definition file.",
    "Svelte compiles components to imperative DOM updates at build time, eliminating the virtual DOM.",
    "Rust ownership prevents data races at compile time through the borrow checker and lifetime system.",
    "Go channels enable CSP-style concurrency. Buffered channels block only when the buffer is full.",
    "Zig uses explicit allocators. The standard library never allocates without an allocator parameter.",
    "Nix provides reproducible builds through pure functions. Derivations are cached in the Nix store by hash.",
]

# --- Episodic: conversation events ---
EPISODIC_TEMPLATES = [
    ("rate limits", "User asked about API rate limits on {day}. Explained {n} req/min standard, {m} enterprise."),
    ("429 errors", "User reported 429 errors {time}. Advised implementing exponential backoff with max {n} retries."),
    ("base URL", "User forgot the base URL. Reminded them it is https://api.{product}.dev/v2 with X-API-Key header."),
    ("CSV upload", "User wanted to upload a large CSV. Suggested using the batch endpoint with {n} record chunks."),
    ("webhook security", "User asked about webhook security. Explained HMAC-SHA256 signatures with dashboard secret."),
    ("migration failure", "User experienced a database migration failure. Guided them to run {tool} upgrade head before deploy."),
    ("pagination", "User was confused about pagination. Showed cursor-based links and next_cursor field usage."),
    ("Docker image", "User asked for the Docker image location. Shared ghcr.io/{product}/{product}-agent with tag options."),
    ("test duration", "User wanted to know test duration. Mentioned {tool} unit tests and {n}-minute integration suite."),
    ("log format", "User inquired about log format. Explained JSON lines to stdout controlled by LOG_LEVEL."),
    ("Redis issues", "User had Redis connection issues. Verified REDIS_URL env var and checked stream consumer groups."),
    ("health endpoint", "User asked about the health endpoint. Described /health returning 200 with DB and cache status."),
    ("feature flag", "User asked how to enable beta features. Showed them the Unleash admin panel and environment toggles."),
    ("auth setup", "User struggled with OAuth2 setup. Walked them through client registration and redirect URI configuration."),
    ("SSL error", "User got certificate errors in staging. Explained the self-signed cert issue and how to trust the CA."),
    ("memory spike", "User reported memory spikes during batch jobs. Suggested reducing chunk size and adding memory profiling."),
    ("slow query", "User had a slow search query. Recommended adding a composite index on (status, created_at)."),
    ("webhook delay", "User noticed webhook delays. Checked the queue depth and increased worker pool from {n} to {m}."),
    ("region latency", "User asked about latency in ap-southeast-1. Ran ping tests and showed average RTT of {n}ms."),
    ("backup restore", "User needed to restore from backup. Guided them through point-in-time recovery for the {day} snapshot."),
]

EPISODIC_DISTRACTORS = [
    "User asked about Kubernetes pod scheduling. Explained node affinity and taint tolerations.",
    "User wanted help with CSS grid layout. Demonstrated grid-template-columns and auto-fill.",
    "User had questions about Go module versioning. Explained semantic import versioning.",
    "User was debugging a memory leak in a Java application. Suggested heap dump analysis.",
    "User asked about neural network hyperparameters. Discussed learning rate schedules and dropout.",
    "User wanted to set up CI/CD with GitHub Actions. Created a workflow with matrix builds.",
    "User asked about TypeScript generic constraints. Showed examples with extends and conditional types.",
    "User had trouble with CSS specificity. Explained the cascade and !important anti-pattern.",
    "User wanted to optimize LCP. Suggested preloading the hero image and inlining critical CSS.",
    "User asked about SQL injection prevention. Recommended parameterized queries and ORM usage.",
]

# --- Procedural: recipes and workflows ---
PROCEDURAL_TEMPLATES = [
    ("rate limits", "To handle rate limits: catch {product}RateLimitError, read Retry-After header, sleep, then retry with exponential backoff capped at {n} seconds."),
    ("token refresh", "To refresh a token: POST to /auth/refresh with the current token in the Authorization header. Store the new token and its expiry."),
    ("file upload", "To upload large files: split into {size}MB chunks, POST each to /v2/files, collect file_ids, then poll the status endpoint until processing completes."),
    ("webhook verify", "To verify webhooks: compute HMAC-SHA256 of the payload using the dashboard secret, compare hex digest to the X-Signature header in constant time."),
    ("debug 500", "To debug 500 errors: check structured logs for trace_id, query the error aggregation dashboard, then inspect the specific service pod logs."),
    ("migration", "To run migrations safely: create a backup, run {tool} upgrade head in a transaction, verify with smoke tests, then promote to production."),
    ("pagination", "To paginate through results: extract next_cursor from each response, append it as the cursor query param, stop when next_cursor is absent."),
    ("deploy", "To deploy a new version: build the Docker image, push to ghcr with commit SHA tag, update the deployment manifest, then verify /health on new pods."),
    ("feature", "To add a new feature: write {tool} tests first, implement the feature, run the full test suite, update documentation, then open a pull request."),
    ("memory leak", "To investigate memory leaks: capture a heap dump, analyze dominator tree for retained objects, check for unclosed connections or circular references."),
    ("auth setup", "To set up authentication: register an application in the dashboard, copy the client_id and secret, configure the callback URL, then test the OAuth flow."),
    ("SSL cert", "To add a custom domain: verify domain ownership with DNS TXT record, generate a CSR, upload the certificate, then update the CNAME."),
    ("monitoring", "To add monitoring: instrument code with Prometheus client library, define SLIs and SLOs, create alert rules, then set up Grafana dashboards."),
    ("cache warming", "To warm caches: pre-compute popular queries during off-peak hours, store results in Redis with 24h TTL, then serve from cache."),
    ("data export", "To export user data: validate the request against GDPR requirements, query all related tables, anonymize PII, then provide a downloadable archive."),
    ("incident response", "To handle an incident: page the on-call engineer, create a war room channel, contain the blast radius, then write a post-mortem within 48 hours."),
    ("security audit", "To audit dependencies: run Snyk or Dependabot scans, review CVE severity, update critical packages, then verify tests pass."),
    ("load test", "To load test: define RPS targets, write k6 or Locust scripts, run against staging, identify bottlenecks, then tune resource limits."),
    ("rollback", "To rollback a deployment: identify the last stable commit SHA, update the deployment manifest to that image, verify health checks, then monitor error rates."),
    ("onboarding", "To onboard a new service: create a service account, grant minimal IAM permissions, add to service mesh, then register health and metrics endpoints."),
]

PROCEDURAL_DISTRACTORS = [
    "To optimize SQL queries: run EXPLAIN ANALYZE, add composite indexes on filtered columns, and consider partitioning for large tables.",
    "To configure CDN caching: set Cache-Control headers, enable stale-while-revalidate, and purge cache on deployment.",
    "To set up monitoring: install Prometheus exporter, configure alert rules, and route critical alerts to PagerDuty.",
    "To write clean code: follow SOLID principles, keep functions under 20 lines, and prefer composition over inheritance.",
    "To debug race conditions: enable thread sanitizer, reproduce under load, then add synchronization primitives.",
    "To handle security breaches: rotate all credentials, audit access logs, notify affected users, then patch the vulnerability.",
    "To improve API design: use resource-oriented URLs, implement pagination, version via Accept header, and document with OpenAPI.",
    "To reduce bundle size: enable tree shaking, split code by route, lazy load components, and compress assets with Brotli.",
    "To set up multi-region: replicate databases with read replicas, use anycast DNS, and implement conflict-free replicated data types.",
    "To handle deadlocks: acquire locks in consistent order, use timeout parameters, and implement retry with jitter.",
]


def _fmt(template: str, product: str = "kestrel", n: int = 1, m: int = 5, size: int = 10, tool: str = "pytest", day: str = "Monday", time: str = "yesterday", quarter: str = "Q3 2026") -> str:
    return template.format(
        product=product, n=n, m=m, size=size, tool=tool, day=day, time=time, quarter=quarter
    )


def _generate_semantic_corpus(seed: int) -> tuple[list[dict[str, Any]], list[MemoryQuery]]:
    rng = random.Random(seed)
    docs: list[dict[str, Any]] = []
    queries: list[MemoryQuery] = []
    
    products = ["kestrel", "falcon", "raven", "hawk", "swift", "sparrow", "phoenix", "drake"]
    tools = ["pytest", "jest", "mocha", "vitest", "unittest", "cargo test", "go test"]
    quarters = ["Q1 2026", "Q2 2026", "Q3 2026", "Q4 2026"]
    
    # Generate core semantic docs (variations across products)
    doc_id = 0
    for _topic_idx, (topic_key, template) in enumerate(SEMANTIC_TOPICS):
        for product in products:
            doc_id += 1
            doc = {
                "id": f"sem_{doc_id:04d}",
                "text": _fmt(template, product=product, n=rng.randint(1, 60), m=rng.randint(5, 100), size=rng.randint(5, 50), tool=rng.choice(tools), quarter=rng.choice(quarters)),
                "layer": "semantic",
            }
            docs.append(doc)
            # Add a query for some docs (not all, to keep query count manageable)
            if doc_id % 3 == 0 and len(queries) < 60:
                q_text = {
                    "API base URL": f"What is the {product} API base URL?",
                    "token expiry": f"How do I refresh a {product} token?",
                    "rate limits": f"What are the {product} rate limits?",
                    "retry policy": f"What is the {product} retry policy?",
                    "pagination": f"How does {product} pagination work?",
                    "file uploads": f"How do I upload files to {product}?",
                    "semantic versioning": f"What versioning does {product} use?",
                    "database migrations": f"How do I run {product} migrations?",
                    "environment variables": f"What env vars does {product} need?",
                    "worker queue": f"What queue does {product} use?",
                    "Docker registry": f"Where are {product} Docker images?",
                    "testing": f"How do I test {product}?",
                    "config validation": f"How is {product} config validated?",
                    "structured logging": f"What logging does {product} use?",
                    "health checks": f"What is the {product} health endpoint?",
                    "caching": f"How does {product} cache responses?",
                    "feature flags": f"How do {product} feature flags work?",
                    "auth methods": f"What auth does {product} support?",
                    "encryption": f"How is {product} data encrypted?",
                    "regions": f"What regions does {product} support?",
                    "compliance": f"What compliance does {product} have?",
                    "monitoring": f"How do I monitor {product}?",
                    "backups": f"How do {product} backups work?",
                    "limits": f"What are {product} resource limits?",
                    "SDKs": f"What SDKs does {product} have?",
                }.get(topic_key, f"Tell me about {product} {topic_key}")
                queries.append(MemoryQuery(q_text, [doc["id"]], "semantic", f"{product} {topic_key}"))
    
    # Add distractors
    for i, text in enumerate(SEMANTIC_DISTRACTORS):
        docs.append({"id": f"sem_d{i+1:04d}", "text": text, "layer": "semantic"})
    
    return docs, queries


def _generate_episodic_corpus(seed: int) -> tuple[list[dict[str, Any]], list[MemoryQuery]]:
    rng = random.Random(seed + 1)
    docs: list[dict[str, Any]] = []
    queries: list[MemoryQuery] = []
    
    products = ["kestrel", "falcon", "raven", "hawk", "swift"]
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    times = ["yesterday", "last week", "this morning", "on Friday", "two days ago"]
    
    doc_id = 0
    for _topic_idx, (topic_key, template) in enumerate(EPISODIC_TEMPLATES):
        for product in products:
            for day in days[:3]:  # 3 days per product per topic
                doc_id += 1
                doc = {
                    "id": f"epi_{doc_id:04d}",
                    "text": _fmt(template, product=product, day=day, time=rng.choice(times), n=rng.randint(1, 10), m=rng.randint(10, 50)),
                    "layer": "episodic",
                }
                docs.append(doc)
                if doc_id % 4 == 0 and len(queries) < 50:
                    q_text = {
                        "rate limits": f"What did the user ask about {product} rate limits {day}?",
                        "429 errors": f"When did the user report {product} 429 errors?",
                        "base URL": f"What did I tell the user about {product} base URL?",
                        "CSV upload": f"How did I help with {product} CSV uploads?",
                        "webhook security": f"What security advice did I give about {product} webhooks?",
                        "migration failure": f"What migration issue did the {product} user have?",
                        "pagination": f"What did I explain about {product} pagination?",
                        "Docker image": f"What Docker info did I share for {product}?",
                        "test duration": f"What did the user ask about {product} test duration?",
                        "log format": f"What did I say about {product} log format?",
                        "Redis issues": f"What {product} Redis issue did the user have?",
                        "health endpoint": f"What did I say about {product} health endpoint?",
                        "feature flag": f"How did I help with {product} feature flags?",
                        "auth setup": f"What {product} auth issue did the user have?",
                        "SSL error": f"What SSL problem did the {product} user face?",
                        "memory spike": f"What did the user report about {product} memory?",
                        "slow query": f"What query issue did the {product} user have?",
                        "webhook delay": f"What webhook delay did the {product} user notice?",
                        "region latency": f"What latency issue did the {product} user ask about?",
                        "backup restore": f"What backup help did the {product} user need?",
                    }.get(topic_key, f"What happened with {product} {topic_key}?")
                    queries.append(MemoryQuery(q_text, [doc["id"]], "episodic", f"{product} {topic_key} episode"))
    
    # Distractors
    for i, text in enumerate(EPISODIC_DISTRACTORS):
        docs.append({"id": f"epi_d{i+1:04d}", "text": text, "layer": "episodic"})
    
    return docs, queries


def _generate_procedural_corpus(seed: int) -> tuple[list[dict[str, Any]], list[MemoryQuery]]:
    rng = random.Random(seed + 2)
    docs: list[dict[str, Any]] = []
    queries: list[MemoryQuery] = []
    
    products = ["kestrel", "falcon", "raven", "hawk"]
    tools = ["pytest", "jest", "mocha"]
    
    doc_id = 0
    for _topic_idx, (topic_key, template) in enumerate(PROCEDURAL_TEMPLATES):
        for product in products:
            doc_id += 1
            doc = {
                "id": f"proc_{doc_id:04d}",
                "text": _fmt(template, product=product, n=rng.randint(10, 120), m=rng.randint(3, 10), size=rng.randint(5, 50), tool=rng.choice(tools)),
                "layer": "procedural",
            }
            docs.append(doc)
            if doc_id % 3 == 0 and len(queries) < 50:
                q_text = {
                    "rate limits": f"How do I handle {product} rate limits?",
                    "token refresh": f"What is the {product} token refresh procedure?",
                    "file upload": f"How do I upload large files to {product}?",
                    "webhook verify": f"How do I verify {product} webhook authenticity?",
                    "debug 500": f"What is the {product} 500 error debugging procedure?",
                    "migration": f"How do I safely run {product} migrations?",
                    "pagination": f"How do I iterate through {product} paginated results?",
                    "deploy": f"What are the deployment steps for {product}?",
                    "feature": f"What is the {product} feature development workflow?",
                    "memory leak": f"How do I investigate a {product} memory leak?",
                    "auth setup": f"How do I set up {product} authentication?",
                    "SSL cert": f"How do I add a custom domain to {product}?",
                    "monitoring": f"How do I add monitoring to {product}?",
                    "cache warming": f"How do I warm {product} caches?",
                    "data export": f"How do I export data from {product}?",
                    "incident response": f"What is the {product} incident response procedure?",
                    "security audit": f"How do I audit {product} dependencies?",
                    "load test": f"How do I load test {product}?",
                    "rollback": f"How do I rollback a {product} deployment?",
                    "onboarding": f"How do I onboard a new service to {product}?",
                }.get(topic_key, f"How do I {topic_key} in {product}?")
                queries.append(MemoryQuery(q_text, [doc["id"]], "procedural", f"{product} {topic_key} recipe"))
    
    # Distractors
    for i, text in enumerate(PROCEDURAL_DISTRACTORS):
        docs.append({"id": f"proc_d{i+1:04d}", "text": text, "layer": "procedural"})
    
    return docs, queries


def build_large_memory_corpus(seed: int = 42) -> MemoryCorpus:
    """Build a large corpus: ~500+ docs, ~150+ queries."""
    rng = random.Random(seed)
    sem_docs, sem_queries = _generate_semantic_corpus(seed)
    epi_docs, epi_queries = _generate_episodic_corpus(seed)
    proc_docs, proc_queries = _generate_procedural_corpus(seed)
    
    all_docs = sem_docs + epi_docs + proc_docs
    all_queries = sem_queries + epi_queries + proc_queries
    rng.shuffle(all_queries)
    
    return MemoryCorpus(documents=all_docs, queries=all_queries)
