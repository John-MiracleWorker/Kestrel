# Test Matrix

Last updated: 2026-08-03

## Unit and Contract Tests

Core unit coverage includes:

- model and dataclass validation
- layered memory thresholds and retrieval ranking
- in-memory backend contract
- Memvid backend contract without requiring `memvid-sdk`
- context compiler and pseudo-context packer budget behavior
- context-frame metadata and conflict handling
- consolidation and Nested Learning promotion gates
- task capsule summary/apply behavior
- provider parser, capability metadata, fallback, and streaming surfaces
- tool schemas, path safety, timeout enforcement, enablement gates, and exact-call approvals
- diagnosis classification and failure-memory recall
- repair worktree preparation, signed validation/reviewer artifacts, literal-tree commit gates, exact-digest rollback, and recovery quarantine
- skill manifest validation, install gates, instruction capsules, host Python/shell rejection, OCI scope policy, and bounded container execution
- CLI subcommands for chat, context, tools, approvals, memory, routines, doctor, run, and status
- state-store migrations through schema 19, terminal transition immutability, approval immutability, routine idempotency, behavior-delta ledgers, and replay safety
- LAN model discovery scope validation, bounded scanner, HTTP transport framing/deadline limits, mDNS containment, scan-manager lifecycle, preview/confirm gates, and disabled-draft import/review contracts
- LAN discovery adversarial qualification (`tests/test_lan_discovery_security.py` executing the hostile corpus in `tests/evals/lan_discovery/hostile_responses.json`): public-range rejection, DNS rebinding simulation, redirect, oversize/chunked responses, slowloris deadlines, malformed JSON, duplicate mDNS, interface change, cancellation, stale results, secret reflection, revision races, and target-enablement bypass — for every case, probe destinations never expand beyond the confirmed scope, no enabled target is produced, and the secret sentinel never appears in serialized evidence
- Adaptive Flock no-authority-expansion matrix (`tests/test_flock_authority_boundaries.py` executing the corpus in `tests/evals/adaptive_flock_qualification/authority_matrix.json`): tools, MCP, skills, plugins, network, workspace, secrets, budget, approvals, privacy, containment, task graph, memory, and high risk — for every case, a trained learned winner routed under an active durable grant leaves every non-routing-owned `AgentConfig` field identical, the compiled task contract digest unchanged, and high/critical-risk scopes deterministic-only (coordinator abstains with `high_risk`; the activation evaluator independently refuses with `high_risk_deterministic_only`)
- Adaptive Flock no-policy-memory proof (`tests/test_flock_no_policy_memory.py`, cross-checked in `tests/test_memory_promotion_gates.py` and `tests/test_mutation_gate.py`): a completed qualification run, scope activation, and learned routing under the active grant write zero memvid records on any layer, emit zero learning-kernel decisions, trigger zero mutation-gate evaluations, and qualification evidence can never satisfy the policy-delta hard gates

Run:

```bash
python -m pytest -q
python -m pytest -q tests/test_lan_discovery_security.py tests/test_lan_scanner.py tests/test_server_lan_discovery_routes.py
python -m pytest -q tests/test_flock_authority_boundaries.py tests/test_flock_no_policy_memory.py tests/test_memory_promotion_gates.py tests/test_mutation_gate.py
```

## Runtime Tests

Runtime tests cover:

- one-turn mock chat
- portable JSON tool-call loop
- malformed tool-call handling
- max tool rounds stop
- user/tool/failure/final-summary memory writes
- event log writes
- background run creation, status, events, cancellation, and approval blocking
- approval resume with tool result persistence
- task graph creation and ready-task filtering
- bounded autonomous scheduler drains
- subagent records and failure diagnosis metadata
- task capsule creation after completed runs
- full approval-to-capsule smoke flow
- deterministic `/search` direct-command tool routing
- behavior-delta compiler/preflight/review/action flows
- live-learning E2E harness contract tests
- proactive-routine scheduling, manual run-now idempotency/reclaim, owner workbench, and occurrence history
- repair-artifact handoff across the deterministic task DAG

## Integration Tests

Gated by env vars:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_memory_system.py tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
docker pull 'python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3'
RUN_EXTENSION_SANDBOX_INTEGRATION=1 \
KESTREL_EXTENSION_TEST_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3' \
python -m pytest -q tests/integration/test_extension_container_integration.py
```

Memvid integration must:

- create temp `.mv2` files only when missing
- write records
- seal
- verify
- close and reopen
- search again and confirm persistence
- round-trip context-frame metadata
- summarize run-scoped `complete.mv2` capsules

MCP integration must:

- launch the stdio fixture
- connect through `MCPManager`
- discover remote tools
- invoke a remote tool
- shut down the managed session

Executable-skill OCI integration must:

- fail rather than skip when enabled prerequisites are absent
- deny undeclared host paths and outbound network
- run as nonroot with a read-only root filesystem
- confine writes to an explicitly granted workspace subtree

Provider live integration is opt-in through `RUN_PROVIDER_INTEGRATION=1`; unit tests should mock provider responses by default. Ollama Cloud + `gpt-oss:120b` has been locally validated for live golden and live-learning E2E paths on memory and Memvid backends.

LAN discovery live integration is opt-in through `RUN_LAN_DISCOVERY_INTEGRATION=1` and requires a controlled private test network with a second machine serving a known fixture:

```bash
RUN_LAN_DISCOVERY_INTEGRATION=1 python -m pytest -q tests/integration/test_lan_mdns_integration.py tests/integration/test_lan_scanner_integration.py
```

It must never be run against an uncontrolled LAN. Record interface, private scope, app commit, fixture digests, exact destinations, timeouts, observations, disabled target IDs, and post-review state as the qualification receipt.

## Golden Evals

Golden evals are executable today:

```bash
VALIDATION_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3'
docker pull "$VALIDATION_IMAGE"
python scripts/run_golden_evals.py --backend memory --provider mock --validation-container-image "$VALIDATION_IMAGE"
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden --validation-container-image "$VALIDATION_IMAGE"
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memory --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memory --validation-container-image "$VALIDATION_IMAGE"
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memvid --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memvid --validation-container-image "$VALIDATION_IMAGE"
```

CI also runs `scripts/run_determinism_evals.py` with seed `1729`, twenty isolated repeats, and a
machine-readable flake receipt. Functional outcome signatures must be identical and every
underlying run must pass; measured latency is retained as a separate gate rather than compared for
byte equality.

The procedural-promotion case uses the pinned image for real private-snapshot repair validation. No image means a truthful failed golden case, never a host-process fallback.

The current golden set checks behavior such as:

- remembering a correction across turns
- retrieving a previous failure before repeating it
- using a procedural recipe only after repeated validated success
- refusing workspace path escape
- blocking shell without enablement
- verifying `.mv2` files
- compiling useful context under budget
- avoiding policy writes from ordinary events
- deterministic direct `/search` routing
- durable plan completion wait behavior

Golden evals should report pass/fail, relevant diagnostics, memory hits, context size, tool count, and failure reasons. They are behavioral checks across turns, not simple unit tests.

## Web Validation

```bash
npm run test --prefix web
npm run build --prefix web
```

The web package uses Vite, React, and TypeScript. The test command runs the TypeScript build plus the Vitest jsdom suite; the build command produces the static assets mounted by the FastAPI server.

The installed-renderer e2e suite (`npm run e2e --prefix desktop`, Playwright) drives the built renderer against deterministic in-page fixtures; `desktop/e2e/lan-discovery.spec.ts` qualifies the LAN discovery renderer journeys (no traffic before owner action, exact scope/bounds before confirmation, public/wide scope rejection, cancellation, disabled-draft results with privacy warning) against a controlled private-network fixture that never scans the ambient network.

## Packaging Validation

```bash
python -m pip install --require-hashes --only-binary=:all: -r config/python-build-bootstrap.txt
python -m pip install --no-build-isolation -e '.[memvid,openai,anthropic,gemini,server,mcp,keyring,dev]'
nest-agent doctor --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "packaging smoke"
docker build -t kestrel-agent:local .
docker run --rm kestrel-agent:local nest-agent doctor --backend memvid --memory-dir /data/memory --provider mock
```

For a tag, the release workflow builds one platform-independent Kestrel wheel before starting its
artifact matrix. The identical checksummed wheel and hash-locked release requirements are then
installed on Linux x86_64, macOS arm64 and x86_64, and native Windows x86_64 for Python 3.11,
3.12, and 3.13. Each lane asserts its runner architecture, exercises the installed package outside
the source checkout, asserts its
`importlib.metadata` version, performs a real Memvid v2 persistence/reopen integration, and on
Windows runs the checksummed PowerShell diagnostic without allowing prerequisite mutation.
