"""Synthetic memory corpora and ground-truth queries for benchmarking.

Generates realistic agent-memory scenarios without external dependencies.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any


@dataclass
class MemoryQuery:
    query: str
    expected_doc_ids: list[str]
    layer: str  # semantic | episodic | procedural
    description: str


@dataclass
class MemoryCorpus:
    documents: list[dict[str, Any]]
    queries: list[MemoryQuery]


def _make_semantic_corpus() -> tuple[list[dict[str, Any]], list[MemoryQuery]]:
    """Stable facts about a fictional API and project."""
    docs: list[dict[str, Any]] = [
        {"id": "sem_001", "text": "The Kestrel API base URL is https://api.kestrel.dev/v2 and requires an X-API-Key header.", "layer": "semantic"},
        {"id": "sem_002", "text": "Authentication tokens expire after 3600 seconds and must be refreshed using the /auth/refresh endpoint.", "layer": "semantic"},
        {"id": "sem_003", "text": "The batch ingest endpoint POST /v2/batch accepts up to 1000 records per request with a 10MB payload limit.", "layer": "semantic"},
        {"id": "sem_004", "text": "Rate limits are 100 requests per minute for standard keys and 1000 per minute for enterprise keys.", "layer": "semantic"},
        {"id": "sem_005", "text": "Webhook signatures use HMAC-SHA256 with the secret provided in the dashboard settings panel.", "layer": "semantic"},
        {"id": "sem_006", "text": "The Python SDK raises KestrelRateLimitError when status 429 is returned with Retry-After header.", "layer": "semantic"},
        {"id": "sem_007", "text": "Default retry policy uses exponential backoff starting at 1 second with a maximum of 5 retries.", "layer": "semantic"},
        {"id": "sem_008", "text": "Pagination uses cursor-based links. The next_cursor field is present when more results exist.", "layer": "semantic"},
        {"id": "sem_009", "text": "The search endpoint supports filtering by created_at, tags, and status using query parameters.", "layer": "semantic"},
        {"id": "sem_010", "text": "File uploads go to POST /v2/files and return a file_id used for async processing callbacks.", "layer": "semantic"},
        {"id": "sem_011", "text": "Project Kestrel uses semantic versioning. Breaking changes bump the major version.", "layer": "semantic"},
        {"id": "sem_012", "text": "The GraphQL gateway is deprecated and will be removed in version 3.0 scheduled for Q3 2026.", "layer": "semantic"},
        {"id": "sem_013", "text": "Database migrations are managed with Alembic. Run alembic upgrade head before deploying.", "layer": "semantic"},
        {"id": "sem_014", "text": "Environment variables for production include DATABASE_URL, REDIS_URL, and SECRET_KEY.", "layer": "semantic"},
        {"id": "sem_015", "text": "The worker queue uses Redis Streams. Job handlers must be idempotent because retries are automatic.", "layer": "semantic"},
        {"id": "sem_016", "text": "Docker images are published to ghcr.io/kestrel/kestrel-agent. Tags include latest, stable, and commit SHA.", "layer": "semantic"},
        {"id": "sem_017", "text": "Unit tests use pytest. Integration tests require Docker Compose and take approximately four minutes.", "layer": "semantic"},
        {"id": "sem_018", "text": "The configuration schema is validated with Pydantic v2 models in config.py at startup.", "layer": "semantic"},
        {"id": "sem_019", "text": "Structured logging outputs JSON lines to stdout. Log level is controlled with LOG_LEVEL.", "layer": "semantic"},
        {"id": "sem_020", "text": "Health checks respond on /health with status 200 and a JSON body containing database and cache states.", "layer": "semantic"},
        # Distractors: similar topics but different details
        {"id": "sem_d1", "text": "The Falcon API uses OAuth2 flow with PKCE and requires a client_id registered in the portal.", "layer": "semantic"},
        {"id": "sem_d2", "text": "AWS Lambda has a 15-minute timeout and 10GB memory limit for ephemeral storage.", "layer": "semantic"},
        {"id": "sem_d3", "text": "PostgreSQL supports JSONB indexing with GIN operators for fast containment queries.", "layer": "semantic"},
        {"id": "sem_d4", "text": "React Server Components render on the server and stream HTML to the client boundary.", "layer": "semantic"},
        {"id": "sem_d5", "text": "Terraform state locking prevents concurrent modifications using DynamoDB or Consul backends.", "layer": "semantic"},
    ]

    queries = [
        MemoryQuery("What is the Kestrel API base URL?", ["sem_001"], "semantic", "direct fact lookup"),
        MemoryQuery("How do I refresh an expired token?", ["sem_002"], "semantic", "procedural fact lookup"),
        MemoryQuery("What are the rate limits for enterprise keys?", ["sem_004"], "semantic", "numeric fact with distractors"),
        MemoryQuery("How does pagination work?", ["sem_008"], "semantic", "concept lookup"),
        MemoryQuery("What endpoint do I use for file uploads?", ["sem_010"], "semantic", "endpoint recall"),
        MemoryQuery("When is the GraphQL gateway being removed?", ["sem_012"], "semantic", "temporal fact"),
        MemoryQuery("How do I run database migrations?", ["sem_013"], "semantic", "command recall"),
        MemoryQuery("What environment variables are needed for production?", ["sem_014"], "semantic", "list recall"),
        MemoryQuery("Where are Docker images published?", ["sem_016"], "semantic", "registry fact"),
        MemoryQuery("How long do integration tests take?", ["sem_017"], "semantic", "duration fact"),
    ]
    return docs, queries


def _make_episodic_corpus() -> tuple[list[dict[str, Any]], list[MemoryQuery]]:
    """Conversation turns and events."""
    docs: list[dict[str, Any]] = [
        {"id": "epi_001", "text": "User asked about API rate limits on Monday. Explained 100 req/min standard, 1000 enterprise.", "layer": "episodic"},
        {"id": "epi_002", "text": "User reported 429 errors yesterday. Advised implementing exponential backoff with max 5 retries.", "layer": "episodic"},
        {"id": "epi_003", "text": "User forgot the base URL. Reminded them it is https://api.kestrel.dev/v2 with X-API-Key header.", "layer": "episodic"},
        {"id": "epi_004", "text": "User wanted to upload a large CSV. Suggested using the batch endpoint with 1000 record chunks.", "layer": "episodic"},
        {"id": "epi_005", "text": "User asked about webhook security. Explained HMAC-SHA256 signatures with dashboard secret.", "layer": "episodic"},
        {"id": "epi_006", "text": "User experienced a database migration failure. Guided them to run alembic upgrade head before deploy.", "layer": "episodic"},
        {"id": "epi_007", "text": "User was confused about pagination. Showed cursor-based links and next_cursor field usage.", "layer": "episodic"},
        {"id": "epi_008", "text": "User asked for the Docker image location. Shared ghcr.io/kestrel/kestrel-agent with tag options.", "layer": "episodic"},
        {"id": "epi_009", "text": "User wanted to know test duration. Mentioned pytest unit tests and four-minute integration suite.", "layer": "episodic"},
        {"id": "epi_010", "text": "User inquired about log format. Explained JSON lines to stdout controlled by LOG_LEVEL.", "layer": "episodic"},
        {"id": "epi_011", "text": "User had Redis connection issues. Verified REDIS_URL env var and checked stream consumer groups.", "layer": "episodic"},
        {"id": "epi_012", "text": "User asked about the health endpoint. Described /health returning 200 with DB and cache status.", "layer": "episodic"},
        # Distractors
        {"id": "epi_d1", "text": "User asked about Kubernetes pod scheduling. Explained node affinity and taint tolerations.", "layer": "episodic"},
        {"id": "epi_d2", "text": "User wanted help with CSS grid layout. Demonstred grid-template-columns and auto-fill.", "layer": "episodic"},
        {"id": "epi_d3", "text": "User had questions about Go module versioning. Explained semantic import versioning.", "layer": "episodic"},
        {"id": "epi_d4", "text": "User was debugging a memory leak in a Java application. Suggested heap dump analysis.", "layer": "episodic"},
        {"id": "epi_d5", "text": "User asked about neural network hyperparameters. Discussed learning rate schedules and dropout.", "layer": "episodic"},
    ]

    queries = [
        MemoryQuery("What did the user ask about on Monday?", ["epi_001"], "episodic", "temporal recall"),
        MemoryQuery("When did the user report 429 errors?", ["epi_002"], "episodic", "event recall"),
        MemoryQuery("What did I tell the user about the base URL?", ["epi_003"], "episodic", "advice recall"),
        MemoryQuery("How did I help with CSV uploads?", ["epi_004"], "episodic", "problem-solution recall"),
        MemoryQuery("What security advice did I give about webhooks?", ["epi_005"], "episodic", "security advice recall"),
        MemoryQuery("What migration issue did the user have?", ["epi_006"], "episodic", "failure episode recall"),
        MemoryQuery("What did I explain about pagination?", ["epi_007"], "episodic", "tutorial recall"),
        MemoryQuery("What Docker information did I share?", ["epi_008"], "episodic", "resource sharing recall"),
        MemoryQuery("What did the user ask about Redis?", ["epi_011"], "episodic", "troubleshooting recall"),
        MemoryQuery("What did I say about the health endpoint?", ["epi_012"], "episodic", "endpoint explanation recall"),
    ]
    return docs, queries


def _make_procedural_corpus() -> tuple[list[dict[str, Any]], list[MemoryQuery]]:
    """Reusable methods and debug recipes."""
    docs: list[dict[str, Any]] = [
        {"id": "proc_001", "text": "To handle rate limits: catch KestrelRateLimitError, read Retry-After header, sleep, then retry with exponential backoff capped at 60 seconds.", "layer": "procedural"},
        {"id": "proc_002", "text": "To refresh a token: POST to /auth/refresh with the current token in the Authorization header. Store the new token and its expiry.", "layer": "procedural"},
        {"id": "proc_003", "text": "To upload large files: split into 10MB chunks, POST each to /v2/files, collect file_ids, then poll the status endpoint until processing completes.", "layer": "procedural"},
        {"id": "proc_004", "text": "To verify webhooks: compute HMAC-SHA256 of the payload using the dashboard secret, compare hex digest to the X-Signature header in constant time.", "layer": "procedural"},
        {"id": "proc_005", "text": "To debug 500 errors: check structured logs for trace_id, query the error aggregation dashboard, then inspect the specific service pod logs.", "layer": "procedural"},
        {"id": "proc_006", "text": "To run migrations safely: create a backup, run alembic upgrade head in a transaction, verify with smoke tests, then promote to production.", "layer": "procedural"},
        {"id": "proc_007", "text": "To paginate through results: extract next_cursor from each response, append it as the cursor query param, stop when next_cursor is absent.", "layer": "procedural"},
        {"id": "proc_008", "text": "To deploy a new version: build the Docker image, push to ghcr with commit SHA tag, update the deployment manifest, then verify /health on new pods.", "layer": "procedural"},
        {"id": "proc_009", "text": "To add a new feature: write pytest tests first, implement the feature, run the full test suite, update documentation, then open a pull request.", "layer": "procedural"},
        {"id": "proc_010", "text": "To investigate memory leaks: capture a heap dump, analyze dominator tree for retained objects, check for unclosed connections or circular references.", "layer": "procedural"},
        # Distractors
        {"id": "proc_d1", "text": "To optimize SQL queries: run EXPLAIN ANALYZE, add composite indexes on filtered columns, and consider partitioning for large tables.", "layer": "procedural"},
        {"id": "proc_d2", "text": "To configure CDN caching: set Cache-Control headers, enable stale-while-revalidate, and purge cache on deployment.", "layer": "procedural"},
        {"id": "proc_d3", "text": "To set up monitoring: install Prometheus exporter, configure alert rules, and route critical alerts to PagerDuty.", "layer": "procedural"},
    ]

    queries = [
        MemoryQuery("How do I handle rate limits in Kestrel?", ["proc_001"], "procedural", "error handling recipe"),
        MemoryQuery("What is the token refresh procedure?", ["proc_002"], "procedural", "auth recipe"),
        MemoryQuery("How do I upload a 50MB file?", ["proc_003"], "procedural", "large file recipe"),
        MemoryQuery("How do I verify webhook authenticity?", ["proc_004"], "procedural", "security recipe"),
        MemoryQuery("What is the debugging procedure for 500 errors?", ["proc_005"], "procedural", "debug recipe"),
        MemoryQuery("How do I safely run database migrations?", ["proc_006"], "procedural", "deployment recipe"),
        MemoryQuery("How do I iterate through paginated results?", ["proc_007"], "procedural", "pagination recipe"),
        MemoryQuery("What are the deployment steps for a new version?", ["proc_008"], "procedural", "deploy recipe"),
        MemoryQuery("What is the feature development workflow?", ["proc_009"], "procedural", "dev workflow recipe"),
        MemoryQuery("How do I investigate a memory leak?", ["proc_010"], "procedural", "debug recipe"),
    ]
    return docs, queries


def build_memory_corpus(seed: int = 42) -> MemoryCorpus:
    random.seed(seed)
    sem_docs, sem_queries = _make_semantic_corpus()
    epi_docs, epi_queries = _make_episodic_corpus()
    proc_docs, proc_queries = _make_procedural_corpus()

    all_docs = sem_docs + epi_docs + proc_docs
    all_queries = sem_queries + epi_queries + proc_queries
    random.shuffle(all_queries)
    return MemoryCorpus(documents=all_docs, queries=all_queries)
