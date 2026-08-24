"""Breadth benchmark scenario corpora (BENCH-003).

Builds the fixed scenario matrix for the S10 breadth benchmark:

  scenarios  = baseline, recency, conflict, update, obsolete,
               distractor, overlap, common_term
  checkpoints = 0..N-1  (corpus growth: checkpoint i adds i * GROWTH_STEP
               deterministic filler documents on top of the scenario corpus)

Every scenario is built deterministically from (scenario, seed, checkpoint):
the base memory corpus (``build_memory_corpus``) is extended with a
scenario-specific stressor set and, at each growth checkpoint, a fixed number
of ground-truth-free filler documents. Ground-truth queries are scenario-owned
so each stressor has a well-defined expected answer set.

Design notes (fairness / honesty):
  - No scenario gives the Kestrel arm the ground-truth layer label; the
    benchmark runner always queries all eligible layers (BENCH-002).
  - ``conflict``/``obsolete``/``distractor`` add documents that share query
    vocabulary but are NOT the expected answer, so every arm must rank the
    true document above vocabulary-adjacent noise.
  - ``update`` changes the ground truth to the newest document, so an arm
    must prefer the updated fact over the superseded one.
  - ``overlap``/``common_term`` stress ranking when many documents share the
    query's terms (low discriminative power), which exercises the smoothed
    non-negative IDF used by every lexical arm.
  - Growth filler documents never carry ground truth; they only grow the
    corpus, so growth degradation is measured against the same query set.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datasets_corpus.memory_corpus import (
    MemoryCorpus,
    MemoryQuery,
    build_memory_corpus,
)

GROWTH_STEP = 6  # filler documents added per checkpoint

SCENARIOS: tuple[str, ...] = (
    "baseline",
    "recency",
    "conflict",
    "update",
    "obsolete",
    "distractor",
    "overlap",
    "common_term",
)

# --- Scenario stressor documents -------------------------------------------


def _conflict_docs() -> list[dict[str, str]]:
    """Documents that directly contradict the base semantic facts.

    Each shares the query vocabulary but asserts a different (wrong) value,
    so a lexical-only arm that cannot tell the facts apart will rank these
    at or above the true document.
    """
    return [
        {
            "id": "conflict_001",
            "text": "The Kestrel API base URL is https://api.kestrel-legacy.dev/v1 and does not require any header.",
            "layer": "semantic",
        },
        {
            "id": "conflict_002",
            "text": "Authentication tokens expire after 300 seconds and must be refreshed using the /auth/legacy endpoint.",
            "layer": "semantic",
        },
        {
            "id": "conflict_003",
            "text": "Rate limits are 10 requests per minute for standard keys and 50 per minute for enterprise keys.",
            "layer": "semantic",
        },
        {
            "id": "conflict_004",
            "text": "Pagination uses page numbers with the page parameter and a total count field.",
            "layer": "semantic",
        },
        {
            "id": "conflict_005",
            "text": "Webhook signatures use plain SHA1 of the payload with no secret.",
            "layer": "semantic",
        },
    ]


def _update_docs() -> list[dict[str, str]]:
    """Newer documents that supersede base facts.

    The ground-truth query for each updated topic points at the NEW document,
    so an arm must prefer the updated fact over the superseded one even though
    both share the query vocabulary.
    """
    return [
        {
            "id": "update_001",
            "text": "The Kestrel API base URL is now https://api.kestrel.dev/v3 and requires an X-API-Key header; the v2 base URL was retired on 2026-08-01.",
            "layer": "semantic",
        },
        {
            "id": "update_002",
            "text": "Authentication tokens now expire after 7200 seconds and must be refreshed using the /auth/refresh endpoint.",
            "layer": "semantic",
        },
        {
            "id": "update_003",
            "text": "Rate limits were raised: 500 requests per minute for standard keys and 5000 per minute for enterprise keys, effective 2026-08-15.",
            "layer": "semantic",
        },
    ]


def _obsolete_docs() -> list[dict[str, str]]:
    """Documents that explicitly describe superseded behavior.

    The query asks about the CURRENT state; the obsolete document is a
    vocabulary-adjacent distractor that must not win.
    """
    return [
        {
            "id": "obsolete_001",
            "text": "The old Kestrel API base URL was https://api.kestrel.dev/v1; it is obsolete and no longer served.",
            "layer": "semantic",
        },
        {
            "id": "obsolete_002",
            "text": "The legacy token refresh flow with the /auth/legacy endpoint is obsolete and has been removed.",
            "layer": "semantic",
        },
        {
            "id": "obsolete_003",
            "text": "The deprecated batch ingest endpoint accepted up to 100 records per request and has been retired.",
            "layer": "semantic",
        },
    ]


def _distractor_docs() -> list[dict[str, str]]:
    """Near-duplicate documents sharing most of the query vocabulary.

    Each is a plausible but wrong statement about the same topic as a base
    fact, so precision@k is a real test (the true document must outrank its
    near-duplicates).
    """
    return [
        {
            "id": "distractor_001",
            "text": "The Kestrel API base URL is https://api.kestrel.dev/v2 but requires an Authorization bearer token instead of an X-API-Key header.",
            "layer": "semantic",
        },
        {
            "id": "distractor_002",
            "text": "Authentication tokens expire after 3600 seconds and must be refreshed using the /auth/token endpoint.",
            "layer": "semantic",
        },
        {
            "id": "distractor_003",
            "text": "The batch ingest endpoint POST /v2/batch accepts up to 10000 records per request with a 100MB payload limit.",
            "layer": "semantic",
        },
        {
            "id": "distractor_004",
            "text": "Rate limits are 100 requests per minute for standard keys and 1000 per minute for enterprise keys on the v1 gateway.",
            "layer": "semantic",
        },
        {
            "id": "distractor_005",
            "text": "Pagination uses cursor-based links and the next_cursor field is always present.",
            "layer": "semantic",
        },
        {
            "id": "distractor_006",
            "text": "Webhook signatures use HMAC-SHA256 with the secret provided in the account settings panel.",
            "layer": "semantic",
        },
    ]


def _overlap_docs() -> list[dict[str, str]]:
    """Documents that share a broad common vocabulary with the overlap queries.

    Several documents mention the same topic words (api / key / request /
    endpoint) so the query's terms are low-discrimination; the expected
    document is the only one with the specific detail the query asks about.
    """
    return [
        {
            "id": "overlap_001",
            "text": "The API request for the key header is sent on every endpoint call to the api host.",
            "layer": "semantic",
        },
        {
            "id": "overlap_002",
            "text": "An API key is required for requests to the kestrel api host on every endpoint.",
            "layer": "semantic",
        },
        {
            "id": "overlap_003",
            "text": "The api host accepts requests and the key header is validated on the endpoint.",
            "layer": "semantic",
        },
        {
            "id": "overlap_004",
            "text": "Requests to the api must include the key header or the endpoint returns a request error.",
            "layer": "semantic",
        },
        {
            "id": "overlap_005",
            "text": "The kestrel api host requires an X-API-Key header on every request to every endpoint.",
            "layer": "semantic",
        },
    ]


def _common_term_docs() -> list[dict[str, str]]:
    """Documents dominated by very common terms (high document frequency).

    Every document contains the same common words; only one carries the
    specific (rare) term the query asks about. This exercises the smoothed
    non-negative IDF path: common terms must not zero out a document, and the
    arm must still surface the document containing the rare discriminator.
    """
    return [
        {
            "id": "common_001",
            "text": "the api the key the token the request the endpoint the status the response the error the time",
            "layer": "semantic",
        },
        {
            "id": "common_002",
            "text": "the api the key the token the request the endpoint the status the response the error the time the retry",
            "layer": "semantic",
        },
        {
            "id": "common_003",
            "text": "the api the key the token the request the endpoint the status the response the error the time the rate limit",
            "layer": "semantic",
        },
        {
            "id": "common_004",
            "text": "the api the key the token the request the endpoint the status the response the error the time the pagination",
            "layer": "semantic",
        },
        {
            "id": "common_005",
            "text": "the api the key the token the request the endpoint the status the response the error the time the webhook",
            "layer": "semantic",
        },
        {
            "id": "common_006",
            "text": "the api the key the token the request the endpoint the status the response the error the time the migration",
            "layer": "semantic",
        },
    ]


# --- Growth filler (deterministic, ground-truth-free) -----------------------

_GROWTH_FILLER_TOPICS = [
    "The staging environment uses the same configuration schema as production.",
    "Backup jobs run nightly and are verified with a restore smoke test.",
    "The metrics endpoint reports request counts and error rates per minute.",
    "Feature flags are evaluated at request time from the configuration store.",
    "The integration suite runs against a disposable containerized database.",
    "Deployments are rolled back automatically when the health check fails.",
    "The log retention policy keeps structured logs for thirty days.",
    "Rate limit counters are stored in a sharded in-memory cache.",
    "The documentation site is rebuilt from the source tree on every release.",
    "Support tickets reference the run id and the environment name.",
    "The analytics pipeline aggregates events into hourly buckets.",
    "Secrets are injected at startup from the secret store, never committed.",
]


def _growth_filler(checkpoint: int, seed: int) -> list[dict[str, str]]:
    """Deterministic filler documents for growth checkpoints.

    checkpoint i adds i * GROWTH_STEP documents, drawn from a fixed topic
    pool with a per-seed rotation so different seeds produce different but
    still deterministic filler. None of these documents answer any query, so
    they only grow the corpus.
    """
    count = max(0, checkpoint) * GROWTH_STEP
    if count == 0:
        return []
    rng = random.Random(seed)
    order = list(range(len(_GROWTH_FILLER_TOPICS)))
    rng.shuffle(order)
    docs: list[dict[str, str]] = []
    for i in range(count):
        topic = _GROWTH_FILLER_TOPICS[order[i % len(order)]]
        docs.append(
            {
                "id": f"growth_{checkpoint}_{i:03d}",
                "text": topic,
                "layer": "semantic",
            }
        )
    return docs


# --- Scenario query sets ----------------------------------------------------

def _base_queries(corpus: MemoryCorpus) -> list[MemoryQuery]:
    return list(corpus.queries)


def _update_queries() -> list[MemoryQuery]:
    """Queries whose ground truth is the UPDATED document, not the base one."""
    return [
        MemoryQuery(
            "What is the current Kestrel API base URL?",
            ["update_001"],
            "semantic",
            "updated fact supersedes base sem_001",
        ),
        MemoryQuery(
            "How long do authentication tokens expire now?",
            ["update_002"],
            "semantic",
            "updated fact supersedes base sem_002",
        ),
        MemoryQuery(
            "What are the current rate limits for standard keys?",
            ["update_003"],
            "semantic",
            "updated fact supersedes base sem_004",
        ),
    ]


def _obsolete_queries() -> list[MemoryQuery]:
    """Queries that ask about the CURRENT state; obsolete docs are distractors."""
    return [
        MemoryQuery(
            "What is the Kestrel API base URL that is currently served?",
            ["sem_001"],
            "semantic",
            "obsolete_001 is a distractor",
        ),
        MemoryQuery(
            "How do I refresh an authentication token now?",
            ["sem_002"],
            "semantic",
            "obsolete_002 is a distractor",
        ),
        MemoryQuery(
            "How many records does the current batch ingest endpoint accept?",
            ["sem_003"],
            "semantic",
            "obsolete_003 is a distractor",
        ),
    ]


def _overlap_queries() -> list[MemoryQuery]:
    """Queries whose terms appear in many documents; one doc has the detail."""
    return [
        MemoryQuery(
            "which kestrel api host requires the key header on every request",
            ["overlap_005"],
            "semantic",
            "all overlap docs share terms; only 005 has every/requires",
        ),
        MemoryQuery(
            "what happens when the api request is missing the key header",
            ["overlap_004"],
            "semantic",
            "only 004 mentions the error",
        ),
    ]


def _common_term_queries() -> list[MemoryQuery]:
    """Queries made of ubiquitous terms plus one rare discriminator.

    The common words (api, key, token, request, endpoint, status, response,
    error, time) appear in nearly every document, so they carry almost no
    ranking signal; only the rare discriminator (rate limit, webhook,
    migration, pagination) identifies the target document. This exercises the
    smoothed non-negative IDF path: ubiquitous terms must not zero out a
    document, and the arm must still surface the document containing the rare
    discriminator.
    """
    common = "the api key token request endpoint status response error time"
    return [
        MemoryQuery(
            f"{common} rate limit",
            ["common_003"],
            "semantic",
            "only common_003 contains rate limit",
        ),
        MemoryQuery(
            f"{common} webhook",
            ["common_005"],
            "semantic",
            "only common_005 contains webhook",
        ),
        MemoryQuery(
            f"{common} migration",
            ["common_006"],
            "semantic",
            "only common_006 contains migration",
        ),
        MemoryQuery(
            f"{common} pagination",
            ["common_004"],
            "semantic",
            "only common_004 contains pagination",
        ),
    ]


# --- Scenario assembly ------------------------------------------------------

def _assemble(
    base: MemoryCorpus,
    *,
    extra_docs: list[dict[str, str]],
    queries: list[MemoryQuery],
    checkpoint: int,
    seed: int,
) -> MemoryCorpus:
    docs = list(base.documents) + [dict(d) for d in extra_docs]
    docs.extend(_growth_filler(checkpoint, seed))
    return MemoryCorpus(documents=docs, queries=queries)


def build_breadth_corpus(
    scenario: str,
    *,
    seed: int = 42,
    checkpoint: int = 0,
) -> MemoryCorpus:
    """Build the deterministic breadth corpus for (scenario, seed, checkpoint).

    ``scenario`` must be one of :data:`SCENARIOS`. ``checkpoint`` is a
    non-negative growth stage (0 = no growth filler). ``seed`` selects the
    deterministic base-corpus construction and the growth-filler rotation.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
    if checkpoint < 0:
        raise ValueError("checkpoint must be >= 0")

    base = build_memory_corpus(seed=seed)

    if scenario == "baseline":
        return _assemble(base, extra_docs=[], queries=_base_queries(base), checkpoint=checkpoint, seed=seed)

    if scenario == "recency":
        # Base facts plus a burst of recent episodic chatter appended after
        # them (same concept as the S9 recency stress, made scenario-owned).
        chatter = [
            {"id": "chatter_001", "text": "Just checked the API docs again, the base URL thing keeps tripping me up, the endpoint path and the key header.", "layer": "episodic"},
            {"id": "chatter_002", "text": "Been hitting rate limits all afternoon with the standard key, definitely need the higher tier for this batch work.", "layer": "episodic"},
            {"id": "chatter_003", "text": "The token refresh flow worked after I read the auth section, expiry is annoying though, keeps timing out.", "layer": "episodic"},
            {"id": "chatter_004", "text": "Batch upload discussion in the channel, someone said the payload cap, we should test the record limit with a big file.", "layer": "episodic"},
            {"id": "chatter_005", "text": "Webhook signatures came up again in review, the HMAC secret from the dashboard, everyone keeps asking about it.", "layer": "episodic"},
            {"id": "chatter_006", "text": "Retry policy chat, exponential backoff starting small, max retries before we give up, the 429 handling.", "layer": "episodic"},
            {"id": "chatter_007", "text": "Pagination again, next cursor, linked pages, the API keeps returning the cursor field, mildly confusing.", "layer": "episodic"},
            {"id": "chatter_008", "text": "Search filtering question in standup, created at and tags and status query params, the search endpoint.", "layer": "episodic"},
            {"id": "chatter_009", "text": "File uploads and the file id for async callbacks, the processing pipeline, uploads endpoint talk.", "layer": "episodic"},
            {"id": "chatter_010", "text": "Versioning and breaking changes, major version bump, semantic versioning argument in the team channel.", "layer": "episodic"},
            {"id": "chatter_011", "text": "GraphQL gateway deprecation, removal version, the timeline for retirement, legacy clients.", "layer": "episodic"},
            {"id": "chatter_012", "text": "Migrations again, alembic upgrade head, someone broke staging, the migration order.", "layer": "episodic"},
            {"id": "chatter_013", "text": "Environment variables for prod, database url, redis url, secret key, the config checklist.", "layer": "episodic"},
            {"id": "chatter_014", "text": "Worker queue and redis streams, idempotent handlers, retries are automatic, the job design.", "layer": "episodic"},
            {"id": "chatter_015", "text": "Docker image tags, latest stable commit sha, ghcr registry, the container publish workflow.", "layer": "episodic"},
            {"id": "chatter_016", "text": "Tests and the four minute integration suite, docker compose, the pytest setup.", "layer": "episodic"},
            {"id": "chatter_017", "text": "Config schema and pydantic v2, validation at startup, the config module.", "layer": "episodic"},
            {"id": "chatter_018", "text": "Structured logging JSON lines, log level env var, stdout output format.", "layer": "episodic"},
            {"id": "chatter_019", "text": "Health checks, the health endpoint, database and cache state in the response body.", "layer": "episodic"},
            {"id": "chatter_020", "text": "Talked to the user about the base URL, they keep pasting the wrong host, X-API-Key header.", "layer": "episodic"},
        ]
        return _assemble(base, extra_docs=chatter, queries=_base_queries(base), checkpoint=checkpoint, seed=seed)

    if scenario == "conflict":
        return _assemble(base, extra_docs=_conflict_docs(), queries=_base_queries(base), checkpoint=checkpoint, seed=seed)

    if scenario == "update":
        # Ground truth for the updated topics points at the new documents; the
        # superseded base facts remain in the corpus as distractors.
        return _assemble(
            base,
            extra_docs=_update_docs(),
            queries=_base_queries(base) + _update_queries(),
            checkpoint=checkpoint,
            seed=seed,
        )

    if scenario == "obsolete":
        return _assemble(
            base,
            extra_docs=_obsolete_docs(),
            queries=_base_queries(base) + _obsolete_queries(),
            checkpoint=checkpoint,
            seed=seed,
        )

    if scenario == "distractor":
        return _assemble(base, extra_docs=_distractor_docs(), queries=_base_queries(base), checkpoint=checkpoint, seed=seed)

    if scenario == "overlap":
        return _assemble(
            base,
            extra_docs=_overlap_docs(),
            queries=_base_queries(base) + _overlap_queries(),
            checkpoint=checkpoint,
            seed=seed,
        )

    if scenario == "common_term":
        # The common-term corpus is a self-contained sub-corpus of ubiquitous
        # documents; it evaluates ONLY its own ubiquitous-term queries (the
        # base queries are not meaningful against this corpus because the
        # common docs deliberately share the base discriminators such as
        # "webhook" / "migration", which would conflate the scenario).
        return _assemble(
            base,
            extra_docs=_common_term_docs(),
            queries=_common_term_queries(),
            checkpoint=checkpoint,
            seed=seed,
        )

    raise AssertionError(f"unhandled scenario {scenario!r}")


def scenario_digest_inputs(
    scenario: str,
    *,
    seeds: list[int],
    checkpoints: list[int],
) -> dict[str, Any]:
    """Canonical, digest-bound description of the fixed scenario matrix.

    Used to compute the fixture digest (BENCH-003): the exact documents and
    queries for every (seed, checkpoint) in the matrix, so the published raw
    results are bound to the exact fixtures that produced them.
    """
    return {
        "scenario": scenario,
        "matrix": {
            str(seed): {
                str(checkpoint): {
                    "documents": [
                        {"id": d["id"], "layer": d["layer"], "text": d["text"]}
                        for d in build_breadth_corpus(scenario, seed=seed, checkpoint=checkpoint).documents
                    ],
                    "queries": [
                        {
                            "query": q.query,
                            "expected_doc_ids": q.expected_doc_ids,
                            "layer": q.layer,
                            "description": q.description,
                        }
                        for q in build_breadth_corpus(scenario, seed=seed, checkpoint=checkpoint).queries
                    ],
                }
                for checkpoint in checkpoints
            }
            for seed in seeds
        },
    }
