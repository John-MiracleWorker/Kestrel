# Explicit LAN Model Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the trusted local owner explicitly discover compatible model servers on other computers on the same private network, inspect bounded probe evidence, and create disabled target drafts that remain ineligible until reviewed and enabled.

**Architecture:** Add a server-owned LAN discovery subsystem beside the existing single-provider probe service. It enumerates local interfaces, produces a read-only scan preview, and runs only after an authenticated owner request confirms one interface/private scope. Passive mDNS candidates and a bounded active probe matrix feed the existing capability probe logic through literal private addresses. Durable scan receipts and observations live in routing SQLite schema v3. Import creates disabled provider/target drafts; a separate revision-checked review transaction assigns trust and roles. Address, certificate, API, catalog, or capability drift marks the draft stale and invalidates dependent routing authority.

**Tech Stack:** Python 3.11 `ipaddress`, `socket`, `concurrent.futures`, `psutil==7.2.2`, `zeroconf==0.150.0`, existing bounded HTTP/model catalog utilities, FastAPI/Pydantic, SQLite routing ledger, React/Vitest Flock workspace, and SSE through the existing fetch-stream helper.

## Global Constraints

- Implement after the desktop foundation and Wildflower shell, and before Adaptive Flock qualification. This plan owns routing schema migration `2 -> 3`; qualification owns `3 -> 4`.
- Discovery is manual-only. No startup scan, background scheduler, periodic scan, or implicit scan from opening Flock.
- The owner must first choose and confirm an interface/private scope. The server recalculates and validates the submitted scope; it never trusts client-provided host lists.
- Never probe a public, global, multicast target, unspecified address, loopback address during LAN mode, or address outside the confirmed scope.
- IPv4 active enumeration is capped at 256 hosts. IPv6 is passive mDNS or exact manual host only; never enumerate an IPv6 subnet.
- Active scan ports are exactly `1234`, `8000`, `8080`, and `11434`. An unusual port requires a separate exact manual-host request.
- Defaults/maxima: 16 concurrent sockets, 256 hosts, 4 known ports per host, 0.75 s TCP timeout, 2.0 s HTTP timeout, 45 s total deadline, 256 KiB response limit, 8 models per server, zero redirects, 2.5 s mDNS window.
- Do not guess credentials, send provider secrets, reuse cloud credentials, perform arbitrary port scanning, follow redirects, resolve attacker-provided public DNS, or execute model-supplied content.
- Treat all mDNS names, HTTP headers, catalogs, models, errors, and capability responses as untrusted bounded text.
- Discovery may reduce authority by staling/disabling a target; it never enables a profile/target, assigns trust, expands capabilities, or activates routing.
- A discovered endpoint keeps locality `local` and trust class `unconfirmed` until explicit review. The UI must say prompts/code leave the current computer.
- Preserve capability provenance. A successful catalog does not prove tools, JSON, streaming, vision, or model identity.
- Store redacted provenance, digests, counts, and typed failures; do not store raw credentials or full provider response bodies.
- Keep deterministic network fakes. Put live socket/mDNS tests behind `RUN_LAN_DISCOVERY_INTEGRATION=1`.
- Run focused tests per task and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q` after each phase.

---

## Phase 1: Define and Persist the Bounded Scan Contract

### Task 1: Add private-network scope and endpoint primitives

**Files:**

- Create: `src/nested_memvid_agent/lan_discovery_models.py`
- Create: `src/nested_memvid_agent/lan_discovery_scope.py`
- Create: `tests/test_lan_discovery_scope.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/test_packaging_deployment.py`

**Interfaces:**

- Produce: `NetworkInterface`, `PrivateScanScope`, `ResolvedLanEndpoint`, `LanScanLimits`, and `LanScanPreview`.
- Produce: `enumerate_private_interfaces()` and `preview_private_scope(interface_id, network)`.
- Invariant: the canonical server preview supplies the exact host count and port matrix the start request binds.

- [ ] **Step 1: Write failing boundary tests**

```python
@pytest.mark.parametrize(
    "network",
    ["8.8.8.0/24", "0.0.0.0/24", "127.0.0.0/24", "224.0.0.0/24"],
)
def test_scan_scope_rejects_non_private_network(network: str) -> None:
    with pytest.raises(ValueError, match="private interface scope"):
        PrivateScanScope.from_request(interface_fixture(), network)


def test_scan_scope_caps_active_ipv4_hosts() -> None:
    with pytest.raises(ValueError, match="at most 256 hosts"):
        PrivateScanScope.from_request(interface_fixture("10.0.0.2/16"), "10.0.0.0/16")


def test_ipv6_scope_never_produces_an_active_host_enumeration() -> None:
    scope = PrivateScanScope.from_request(
        interface_fixture("fd00::2/64"),
        "fd00::/64",
    )
    assert scope.active_hosts == ()
    assert scope.passive_or_manual_only is True
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_discovery_scope.py \
  tests/test_packaging_deployment.py
```

Expected: LAN modules and dependencies are absent.

- [ ] **Step 3: Implement strict canonicalization**

Pin the optional integration dependencies:

```toml
lan-discovery = [
  "psutil==7.2.2",
  "zeroconf==0.150.0",
]
```

The fully bundled Desktop sidecar includes this extra; a non-Desktop advanced install without it reports passive discovery unavailable but may still use exact manual entry if supported.

Define exact limits:

```python
KNOWN_MODEL_SERVICE_PORTS = (1234, 8000, 8080, 11434)
MAX_ACTIVE_HOSTS = 256
MAX_SCAN_CONCURRENCY = 16
TCP_CONNECT_TIMEOUT_SECONDS = 0.75
HTTP_PROBE_TIMEOUT_SECONDS = 2.0
TOTAL_SCAN_DEADLINE_SECONDS = 45.0
MAX_PROBE_RESPONSE_BYTES = 256 * 1024
MAX_DISCOVERED_MODELS = 8
MDNS_WINDOW_SECONDS = 2.5
```

Generate opaque interface IDs as a digest of OS interface identity and addresses; do not use renderer-submitted display names as authority. Require the submitted network to be a subnet of an address actually attached to that interface.

- [ ] **Step 4: Run model/scope tests and lock checks**

Run:

```bash
uv lock --check
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_discovery_scope.py \
  tests/test_packaging_deployment.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass on Linux, macOS, and Windows mocks.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/lan_discovery_models.py \
  src/nested_memvid_agent/lan_discovery_scope.py \
  tests/test_lan_discovery_scope.py \
  tests/test_packaging_deployment.py \
  pyproject.toml uv.lock
git commit -m "feat: define bounded private LAN scan scopes"
```

### Task 2: Add routing schema v3 scan receipts and observations

**Files:**

- Modify: `src/nested_memvid_agent/routing/ledger_schema.py`
- Create: `src/nested_memvid_agent/routing/lan_records.py`
- Create: `src/nested_memvid_agent/routing/lan_ledger.py`
- Create: `src/nested_memvid_agent/routing/lan_serialization.py`
- Create: `tests/test_lan_discovery_ledger.py`
- Modify: `tests/test_agent_routing_ledger.py`

**Interfaces:**

- Schema version: `ROUTING_SCHEMA_VERSION = 3`.
- Tables: `routing_lan_scans`, `routing_lan_observations`, and `routing_lan_scan_events`.
- Scan states: `draft`, `running`, `cancelling`, `cancelled`, `completed`, `failed`, `interrupted`.
- Produce revision-checked `LanDiscoveryLedger` CRUD/transition methods.
- Invariant: observations and terminal receipt fields are append-only after terminalization.

- [ ] **Step 1: Write failing migration and immutability tests**

```python
def test_routing_v2_migrates_to_v3_without_rewriting_route_history(
    v2_state: AgentStateStore,
) -> None:
    before = route_history_digest(v2_state)
    ledger = RoutingLedger(v2_state)
    assert ledger.schema_version() == 3
    assert route_history_digest(v2_state) == before


def test_terminal_scan_receipt_is_immutable(lan_ledger: LanDiscoveryLedger) -> None:
    receipt = completed_scan_fixture(lan_ledger)
    with pytest.raises(LanScanRevisionConflict):
        lan_ledger.append_observation(receipt.scan_id, observation_fixture())
```

- [ ] **Step 2: Run and verify migration failure**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_discovery_ledger.py \
  tests/test_agent_routing_ledger.py
```

Expected: schema remains v2 and LAN tables are absent.

- [ ] **Step 3: Implement additive schema v3**

Use columns sufficient to reconstruct and authenticate the scan:

- scan: ID, status, revision, owner principal, confirmed interface ID, network, limits JSON/digest, preview digest, timestamps, cancel reason, terminal reason, candidate/error/timeout counts, terminal receipt JSON/digest;
- observation: scan ID, stable endpoint ID, source (`mdns`, `active`, `manual`), interface/address/port, API shape, TLS/certificate evidence, catalog/capability digests, bounded public payload, freshness timestamp, error category;
- event: monotonically increasing per-scan sequence, type, bounded payload, timestamp.

Use canonical JSON, SHA-256 digests, foreign keys, unique `(scan_id, endpoint_id)`, and indexes for status/event polling. Revision-check every mutable transition inside one SQLite transaction.

- [ ] **Step 4: Run ledger and full suite**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_discovery_ledger.py \
  tests/test_agent_routing_ledger.py \
  tests/test_state_store.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: v2 migration preserves existing route decisions/outcomes/shadows/calibrations.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/ledger_schema.py \
  src/nested_memvid_agent/routing/lan_records.py \
  src/nested_memvid_agent/routing/lan_ledger.py \
  src/nested_memvid_agent/routing/lan_serialization.py \
  tests/test_lan_discovery_ledger.py \
  tests/test_agent_routing_ledger.py
git commit -m "feat: persist immutable LAN discovery evidence"
```

---

## Phase 2: Discover Candidates Without Becoming a Port Scanner

### Task 3: Add bounded passive mDNS candidate collection

**Files:**

- Create: `src/nested_memvid_agent/lan_mdns.py`
- Create: `tests/test_lan_mdns.py`
- Create: `tests/integration/test_lan_mdns_integration.py`

**Interfaces:**

- Consume: confirmed interface addresses and a 2.5-second deadline.
- Produce: deduplicated `LanCandidate` records only.
- Service type allowlist: `_ollama._tcp.local.`, `_lmstudio._tcp.local.`, `_openai._tcp.local.`, and `_kestrel-model._tcp.local.`.
- Invariant: mDNS metadata never grants provider identity, capability, trust, or target enablement.

- [ ] **Step 1: Write failing hostile-packet tests**

```python
def test_mdns_rejects_public_and_out_of_scope_addresses() -> None:
    candidates = collect_with_fake_records(
        records=[
            mdns_record("public", "8.8.8.8", 11434),
            mdns_record("other-lan", "192.168.50.4", 11434),
            mdns_record("selected", "192.168.1.9", 11434),
        ],
        scope=scope("192.168.1.0/24"),
    )
    assert [(item.address, item.port) for item in candidates] == [
        ("192.168.1.9", 11434)
    ]


def test_mdns_text_is_bounded_and_not_trusted_as_provider_identity() -> None:
    item = collect_with_fake_records(records=[oversized_txt_record()])[0]
    assert item.provider_hint is None
    assert len(json.dumps(item.public_metadata)) <= 4096
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q tests/test_lan_mdns.py
```

Expected: module absent.

- [ ] **Step 3: Implement an injectable collector**

Wrap `zeroconf` behind a protocol so unit tests never touch the network. Bind only selected interfaces. Normalize and bound instance/service/TXT values, reject link-local addresses without the selected zone/interface, deduplicate by `(address, port)`, and stop at the deadline even when callbacks continue.

- [ ] **Step 4: Run deterministic and gated integration tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q tests/test_lan_mdns.py
RUN_LAN_DISCOVERY_INTEGRATION=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run pytest -q tests/integration/test_lan_mdns_integration.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: deterministic tests pass; gated integration advertises one local fixture and discovers only it.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/lan_mdns.py \
  tests/test_lan_mdns.py \
  tests/integration/test_lan_mdns_integration.py
git commit -m "feat: collect bounded LAN model mDNS candidates"
```

### Task 4: Add the private-address active probe matrix

**Files:**

- Create: `src/nested_memvid_agent/lan_scanner.py`
- Create: `src/nested_memvid_agent/lan_http_transport.py`
- Create: `tests/test_lan_scanner.py`
- Create: `tests/test_lan_http_transport.py`
- Create: `tests/integration/test_lan_scanner_integration.py`
- Modify: `src/nested_memvid_agent/llm/provider_urls.py`

**Interfaces:**

- Consume: canonical `PrivateScanScope`, exact limits, passive candidates, cancellation token, monotonic clock, and socket/HTTP adapters.
- Produce: typed endpoint observations with `reachable`, `api_shape`, `transport_security`, `catalog`, and failure category.
- Active paths are allowlisted probes for Ollama and OpenAI-compatible APIs; no arbitrary path comes from the network.
- Invariant: connect to a literal private address, send no credentials, and follow zero redirects.

- [ ] **Step 1: Write failing SSRF/limit tests**

```python
def test_scanner_never_calls_public_or_unconfirmed_endpoint() -> None:
    scanner = scanner_with_recording_transport()
    scanner.scan(
        preview=preview_fixture(),
        injected_candidates=[
            candidate("192.168.1.10", 11434),
            candidate("169.254.169.254", 80),
            candidate("8.8.8.8", 11434),
        ],
    )
    assert scanner.transport.destinations == {
        ("192.168.1.10", 1234),
        ("192.168.1.10", 8000),
        ("192.168.1.10", 8080),
        ("192.168.1.10", 11434),
    }


def test_redirect_response_is_recorded_and_not_followed() -> None:
    transport = fake_transport(redirect_to="http://8.8.8.8/models")
    result = probe_endpoint(endpoint("192.168.1.10", 11434), transport)
    assert result.error_category == "redirect_rejected"
    assert transport.request_count == 1
```

Add exact assertions for maximum sockets, total deadline, response size, cancellation, bounded errors, and maximum model count.

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_scanner.py \
  tests/test_lan_http_transport.py
```

Expected: modules absent.

- [ ] **Step 3: Implement the scanner**

Perform a bounded TCP reachability check before HTTP. Submit at most 16 concurrent tasks. At every admission, check total deadline and cancellation. Use literal IPv4 or bracketed IPv6 URLs; never DNS-resolve an automatically discovered candidate. Probe only:

- `GET /api/tags` for Ollama shape;
- `GET /v1/models` for OpenAI-compatible shape;
- a bounded generation/capability probe only after catalog identity is established.

Use a no-redirect handler. Read at most 256 KiB plus one sentinel byte. Reject control characters and bound each display/error field to 1,024 characters.

- [ ] **Step 4: Run unit, integration, and security tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_scanner.py \
  tests/test_lan_http_transport.py \
  tests/test_provider_probe.py
RUN_LAN_DISCOVERY_INTEGRATION=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run pytest -q tests/integration/test_lan_scanner_integration.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: the integration fixture is discovered on its exact private/loopback test scope; redirect/public/oversize fixtures fail closed.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/lan_scanner.py \
  src/nested_memvid_agent/lan_http_transport.py \
  src/nested_memvid_agent/llm/provider_urls.py \
  tests/test_lan_scanner.py \
  tests/test_lan_http_transport.py \
  tests/integration/test_lan_scanner_integration.py
git commit -m "feat: probe only bounded private model endpoints"
```

### Task 5: Reuse capability probes and import disabled drafts

**Files:**

- Create: `src/nested_memvid_agent/lan_discovery_service.py`
- Create: `tests/test_lan_discovery_service.py`
- Modify: `src/nested_memvid_agent/provider_probe.py`
- Modify: `src/nested_memvid_agent/routing/ledger_registry.py`
- Modify: `src/nested_memvid_agent/server_routing_routes.py`
- Modify: `tests/test_server_routing_discovery.py`
- Modify: `tests/test_adaptive_flock_provider_update_semantics.py`

**Interfaces:**

- Consume: LAN endpoint observation and existing `ProviderProbeService`.
- Produce: disabled `ProviderProfile` plus disabled `ModelTarget` drafts with stable provenance.
- Draft defaults: `enabled=False`, `locality="local"`, `trust_class="unconfirmed"`, `health="unknown"`.
- Produce: `review_lan_target(target_id, expected_revision, trust_class, intended_roles, privacy_acknowledged, enabled)`.
- Invariant: stale discovery metadata forces `enabled=False` and `health="unavailable"`.

- [ ] **Step 1: Write failing disabled/import/stale tests**

```python
def test_imported_lan_server_and_models_are_disabled(tmp_path: Path) -> None:
    result = service.import_observation(observation_with_models("qwen", "coder"))
    assert result.profile.profile.enabled is False
    assert all(not item.target.enabled for item in result.targets)
    assert all(item.target.trust_class == "unconfirmed" for item in result.targets)
    assert ledger.list_model_targets(enabled_only=True) == []


def test_review_cannot_enable_stale_or_unacknowledged_lan_target() -> None:
    with pytest.raises(ValueError, match="fresh discovery evidence"):
        service.review_target(stale_target_request())
    with pytest.raises(ValueError, match="privacy acknowledgement"):
        service.review_target(unacknowledged_request())
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_discovery_service.py \
  tests/test_server_routing_discovery.py \
  tests/test_adaptive_flock_provider_update_semantics.py
```

Expected: LAN import/review service absent.

- [ ] **Step 3: Implement stable provenance and review**

Persist discovery metadata containing scan/observation IDs, interface, address, port, transport security, API shape, catalog digest/freshness, certificate digest when present, capability evidence/provenance, and `stale` reason.

Use stable IDs derived from scan-independent endpoint identity and model name. If the same endpoint/model reappears unchanged, revision it. If address, API shape, model identity, catalog, certificate, or observed capability changes, mark the previous binding stale and disable it before creating/updating the new draft. Return affected target/grant IDs so the activation service can suspend them after that plan lands.

Review is a separate server transaction requiring:

- current profile and target revisions;
- fresh observation digest;
- owner privacy acknowledgement;
- explicit trust class;
- intended roles/task-family affinities;
- capability claims no stronger than observed/provider/operator evidence;
- selected enabled state.

- [ ] **Step 4: Run routing suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_discovery_service.py \
  tests/test_server_routing_discovery.py \
  tests/test_adaptive_flock_provider_update_semantics.py \
  tests/test_agent_routing_guardrails.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: drafts cannot enter eligible inventory before review.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/lan_discovery_service.py \
  src/nested_memvid_agent/provider_probe.py \
  src/nested_memvid_agent/routing/ledger_registry.py \
  src/nested_memvid_agent/server_routing_routes.py \
  tests/test_lan_discovery_service.py \
  tests/test_server_routing_discovery.py \
  tests/test_adaptive_flock_provider_update_semantics.py
git commit -m "feat: import LAN endpoints as disabled target drafts"
```

---

## Phase 3: Add Durable Owner-Controlled Scan APIs

### Task 6: Implement the scan manager and interruption behavior

**Files:**

- Create: `src/nested_memvid_agent/lan_scan_manager.py`
- Create: `tests/test_lan_scan_manager.py`
- Modify: `src/nested_memvid_agent/server.py`

**Interfaces:**

- Produce: create/start/cancel/status/list/event projection over durable scan records.
- One scan worker at a time per owner profile; no scan resumes automatically after sidecar restart.
- Cancellation stops new admissions and waits only for already bounded socket calls.
- Invariant: UI disconnect does not cancel; sidecar restart terminalizes a running scan as `interrupted`.

- [ ] **Step 1: Write failing lifecycle/race tests**

```python
def test_scan_requires_preview_digest_and_expected_revision(manager: LanScanManager) -> None:
    draft = manager.create_draft(preview_fixture())
    with pytest.raises(LanScanRevisionConflict):
        manager.start(
            draft.scan_id,
            expected_revision=99,
            preview_digest=draft.preview_digest,
        )


def test_restart_marks_running_scan_interrupted_without_resuming(
    state: AgentStateStore,
) -> None:
    seed_running_scan(state)
    manager = LanScanManager.recover(state, scanner=recording_scanner())
    assert manager.get("lan_1").status == "interrupted"
    assert manager.scanner.calls == []
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q tests/test_lan_scan_manager.py
```

Expected: manager absent.

- [ ] **Step 3: Implement the manager**

Use a single bounded executor owned by FastAPI lifespan. Persist state/event changes before emitting them. The terminal receipt includes preview digest, limits, mDNS availability, exact probed host/port counts, observations, error category counts, cancellation/interruption reason, start/end timestamps, and receipt digest.

Do not make draft-import mutations from a worker after cancellation without a completed observation transaction. Each imported draft links to its terminal or partial scan evidence.

- [ ] **Step 4: Run manager, shutdown, and full tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_scan_manager.py \
  tests/test_chaos_recovery.py \
  tests/test_run_backpressure.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass and no executor/thread survives app shutdown.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/lan_scan_manager.py \
  src/nested_memvid_agent/server.py \
  tests/test_lan_scan_manager.py
git commit -m "feat: run durable owner-controlled LAN scans"
```

### Task 7: Expose strict LAN discovery and review routes

**Files:**

- Create: `src/nested_memvid_agent/server_lan_discovery_routes.py`
- Create: `tests/test_server_lan_discovery_routes.py`
- Modify: `src/nested_memvid_agent/server.py`
- Modify: `src/nested_memvid_agent/server_routing_routes.py`
- Modify: `tests/test_server_security_headers.py`

**Interfaces:**

- `GET /api/routing/lan/interfaces`
- `POST /api/routing/lan/preview`
- `POST /api/routing/lan/scans`
- `POST /api/routing/lan/scans/{scan_id}/start`
- `GET /api/routing/lan/scans`
- `GET /api/routing/lan/scans/{scan_id}`
- `POST /api/routing/lan/scans/{scan_id}/cancel`
- `GET /api/routing/lan/scans/{scan_id}/events`
- `POST /api/routing/lan/manual-probe`
- `POST /api/routing/lan/targets/{target_id}/review`

All mutation bodies are strict (`extra="forbid"`), owner-authenticated, revision-checked, bounded, and raw-secret-free.

- [ ] **Step 1: Write failing API contract tests**

```python
def test_scan_start_requires_explicit_confirmed_scope(client: TestClient) -> None:
    response = client.post(
        "/api/routing/lan/scans",
        json={"interface_id": "if_1", "network": "192.168.1.0/24"},
    )
    assert response.status_code == 422


def test_manual_probe_rejects_public_host_and_allows_one_exact_private_port(
    client: TestClient,
) -> None:
    assert client.post(
        "/api/routing/lan/manual-probe",
        json={"host": "8.8.8.8", "port": 11434},
    ).status_code == 400
    accepted = client.post(
        "/api/routing/lan/manual-probe",
        json={
            "host": "192.168.1.9",
            "port": 5001,
            "privacy_acknowledged": True,
        },
    )
    assert accepted.status_code == 202
```

- [ ] **Step 2: Run and verify route failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_lan_discovery_routes.py \
  tests/test_server_security_headers.py
```

Expected: 404/import failures.

- [ ] **Step 3: Implement route adapters**

`preview` returns a short-lived digest bound to interface identity, network, limits, host count, port count, mDNS availability, expiry, and current server version. `scans` requires `confirmed=true` plus that unexpired digest. Re-preview on interface/address change.

Manual hostname entry resolves once through an injected resolver, requires every selected address to be private/link-local and explicitly shown in the preview, and probes the selected literal address. Do not pass a hostname through ordinary redirect-following provider code.

SSE replays persisted events from `Last-Event-ID` and bounds each event. Authentication follows the existing fetch-stream path.

- [ ] **Step 4: Run API, routing, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_lan_discovery_routes.py \
  tests/test_server_routing_discovery.py \
  tests/test_server_security_headers.py \
  tests/test_agent_routing_guardrails.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/server_lan_discovery_routes.py \
  src/nested_memvid_agent/server.py \
  src/nested_memvid_agent/server_routing_routes.py \
  tests/test_server_lan_discovery_routes.py \
  tests/test_server_security_headers.py
git commit -m "feat: expose explicit LAN discovery API"
```

---

## Phase 4: Build the Flock LAN Experience

### Task 8: Add typed LAN client contracts and scan state

**Files:**

- Create: `web/src/flock/lan/types.ts`
- Create: `web/src/flock/lan/api.ts`
- Create: `web/src/flock/lan/api.test.ts`
- Create: `web/src/flock/lan/useLanScan.ts`
- Create: `web/src/flock/lan/useLanScan.test.ts`
- Modify: `web/src/routing/types.ts`
- Modify: `web/src/routing/api.ts`

**Interfaces:**

- Produce typed interface/preview/scan/observation/event/review contracts.
- Reconnect SSE using persisted event sequence.
- Invariant: no client control can expand server maxima or infer scan completion from a disconnected stream.

- [ ] **Step 1: Write failing request-shape and reconnect tests**

```ts
it("starts only the exact previewed scope", async () => {
  await startLanScan({
    scanId: "lan_1",
    expectedRevision: 1,
    previewDigest: "a".repeat(64),
    confirmed: true
  });
  expect(lastJsonBody()).toEqual({
    expected_revision: 1,
    preview_digest: "a".repeat(64),
    confirmed: true
  });
});

it("refetches server status after event-stream disconnect", async () => {
  const hook = renderLanScanHook("lan_1");
  disconnectEventStream();
  await eventually(() => expect(requests()).toContain("/api/routing/lan/scans/lan_1"));
  expect(hook.current.status).toBe("running");
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- flock/lan
```

Expected: modules absent.

- [ ] **Step 3: Implement typed client/hook**

Use server status as authority. SSE is an acceleration channel. Preserve explicit `unknown`, `interrupted`, and `cancelled` states. Never convert a timeout count into “server absent” without showing the typed reason.

- [ ] **Step 4: Run tests/build**

Run:

```bash
npm --prefix web test -- flock/lan routing
npm --prefix web run build
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/flock/lan web/src/routing/types.ts web/src/routing/api.ts
git commit -m "feat: add typed LAN discovery client"
```

### Task 9: Build Scan Network, result review, and target confirmation UI

**Files:**

- Create: `web/src/flock/lan/LanDiscoveryPanel.tsx`
- Create: `web/src/flock/lan/LanDiscoveryPanel.test.tsx`
- Create: `web/src/flock/lan/LanScopePreview.tsx`
- Create: `web/src/flock/lan/LanScanProgress.tsx`
- Create: `web/src/flock/lan/LanServerCard.tsx`
- Create: `web/src/flock/lan/LanTargetReview.tsx`
- Create: `web/src/flock/lan/ManualEndpointForm.tsx`
- Create: `web/src/flock/lan/lan.css`
- Modify: `web/src/flock/FlockWorkspace.tsx`
- Modify: `web/src/routing/RoutingCenter.tsx`
- Modify: `web/src/routing/RoutingCenter.test.tsx`

**Interfaces:**

- Owner flow: choose Scan network; choose interface/private scope; inspect exact bounds; confirm; watch progress; inspect servers/models/capabilities/privacy; review trust and roles; optionally enable.
- Results show address/interface, API shape, model inventory, transport security, capability provenance, freshness, source, and prompt/code privacy warning.
- Invariant: every discovered target begins visually and actually disabled.

- [ ] **Step 1: Write failing explicit-action and disabled-draft tests**

```ts
it("does not call discovery merely by opening Flock", () => {
  render(<FlockWorkspace initialTab="providers" />);
  expect(requests()).not.toContain("/api/routing/lan/interfaces");
  expect(requests().some((path) => path.includes("/lan/scans"))).toBe(false);
});

it("shows exact scope before scan and cannot broaden it while running", async () => {
  renderLanDiscovery();
  await user.click(screen.getByRole("button", { name: "Scan network" }));
  await chooseInterfaceAndScope("Wi-Fi", "192.168.1.0/24");
  expect(screen.getByText("Up to 254 hosts × 4 known model ports")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Confirm and scan" }));
  expect(screen.getByLabelText("Network scope")).toBeDisabled();
});

it("requires privacy acknowledgement before enabling a LAN target", () => {
  render(<LanTargetReview target={freshDisabledLanTarget} />);
  expect(screen.getByRole("button", { name: "Enable target" })).toBeDisabled();
  expect(screen.getByText(/prompts and code leave this computer/i)).toBeVisible();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- LanDiscoveryPanel LanTargetReview FlockWorkspace RoutingCenter
```

Expected: UI modules absent.

- [ ] **Step 3: Implement the flow**

Opening Flock does not enumerate interfaces; clicking Scan network does. Keep manual endpoint entry separate and require an exact host/port plus privacy acknowledgement. Use status text/icons in addition to color. Put raw response digests and bounded errors under Evidence.

If a target is stale, disable the enable action and show the exact drift (`address_changed`, `certificate_changed`, `catalog_changed`, `model_missing`, `capability_changed`, or `freshness_expired`) with Re-scan.

- [ ] **Step 4: Run renderer, API, and build tests**

Run:

```bash
npm --prefix web test -- flock routing
npm --prefix web run build
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_lan_discovery_routes.py \
  tests/test_lan_discovery_service.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/flock/lan web/src/flock/FlockWorkspace.tsx \
  web/src/routing/RoutingCenter.tsx \
  web/src/routing/RoutingCenter.test.tsx
git commit -m "feat: add explicit LAN model discovery workspace"
```

---

## Phase 5: Security and Installed-Path Qualification

### Task 10: Add adversarial network and renderer validation

**Files:**

- Create: `tests/test_lan_discovery_security.py`
- Create: `tests/evals/lan_discovery/hostile_responses.json`
- Create: `desktop/e2e/lan-discovery.spec.ts`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/SECURITY.md`
- Modify: `docs/TEST_MATRIX.md`

**Interfaces:**

- Test public-range rejection, DNS rebinding simulation, redirect, oversize/chunked response, slowloris deadline, malformed JSON, duplicate mDNS, interface change, cancellation, stale results, secret reflection, revision races, and target enablement bypass.
- Desktop E2E uses a controlled private-network fixture; it must never scan the CI runner’s ambient network.

- [ ] **Step 1: Add failing adversarial corpus tests**

```python
@pytest.mark.parametrize("case", load_hostile_cases())
def test_hostile_lan_response_never_expands_probe_or_authority(case: HostileCase) -> None:
    result = run_hostile_case(case)
    assert set(result.destinations) <= set(case.allowed_destinations)
    assert result.enabled_targets == ()
    assert case.secret_sentinel not in result.serialized_evidence
```

- [ ] **Step 2: Run the security suite red**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_lan_discovery_security.py \
  tests/test_lan_scanner.py \
  tests/test_server_lan_discovery_routes.py
```

Expected: any unimplemented hostile cases fail.

- [ ] **Step 3: Fix at the owning boundary**

Do not add test-only bypasses. Tighten scope validation, transport, redaction, response parsing, draft review, or event bounding as indicated.

- [ ] **Step 4: Run complete gates**

Run:

```bash
uv run python -m compileall -q src tests
uv run ruff check src tests
uv run mypy src
uv run bandit -q -r src -lll -iii
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
npm --prefix web test
npm --prefix web run build
npm --prefix desktop test
npm --prefix desktop run e2e -- lan-discovery
```

Expected: all deterministic gates pass.

- [ ] **Step 5: Run controlled live evidence**

On a private test network with a second computer serving a known Ollama or OpenAI-compatible fixture:

```bash
RUN_LAN_DISCOVERY_INTEGRATION=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run pytest -q \
  tests/integration/test_lan_mdns_integration.py \
  tests/integration/test_lan_scanner_integration.py
```

Record interface, private scope, app commit, sidecar digest, server fixture digest, exact destinations, timeouts, observations, disabled target IDs, and post-review state. This evidence is integration qualification, not permission to probe an uncontrolled network.

- [ ] **Step 6: Commit**

```bash
git add tests/test_lan_discovery_security.py \
  tests/evals/lan_discovery \
  desktop/e2e/lan-discovery.spec.ts \
  .github/workflows/ci.yml \
  docs/SECURITY.md docs/TEST_MATRIX.md
git commit -m "test: qualify bounded LAN discovery"
```

---

## Final Verification

- [ ] Run exact source gates at final `HEAD`:

```bash
uv lock --check
uv run python -m compileall -q src tests
uv run ruff check src tests
uv run mypy src
uv run bandit -q -r src -lll -iii
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
npm --prefix web run licenses:check
npm --prefix web run test:typecheck
npm --prefix web test
npm --prefix web run build
npm --prefix desktop run test:typecheck
npm --prefix desktop test
npm --prefix desktop run e2e -- lan-discovery
```

- [ ] Verify the route/schema contract:

```bash
rg -n "ROUTING_SCHEMA_VERSION = 3" src/nested_memvid_agent/routing/ledger_schema.py
rg -n "/api/routing/lan/" src/nested_memvid_agent/server_lan_discovery_routes.py
rg -n "enabled=False|trust_class=\"unconfirmed\"" \
  src/nested_memvid_agent/lan_discovery_service.py
git diff --check
git status --short
```

- [ ] Manually inspect the exact built Desktop path:

  - no scan occurs before clicking Scan network;
  - the owner sees the exact private scope and bounds before confirmation;
  - public/wide/invalid scopes are rejected;
  - scan progress survives renderer reload;
  - cancellation stops new probes;
  - every result is a disabled draft;
  - prompt/code privacy warning is explicit;
  - stale/certificate/catalog/capability drift blocks enablement;
  - target review requires trust, roles, privacy acknowledgement, and current revisions;
  - no discovered target gains tools, secrets, network, workspace, budget, or approval authority;
  - no raw response, credential, or API token appears in logs/events/support bundles.

- [ ] Record final commit SHA, routing schema migration receipt, deterministic test receipt, and controlled live evidence in the program index.

## Completion Criteria

- Kestrel can find compatible model servers on another computer on the explicitly selected private network.
- Discovery is manual, bounded, private-scope-only, allowlisted, redacted, and cancellable.
- Passive and active evidence are distinguished.
- Every server/model is imported as a disabled, unconfirmed draft.
- Enablement requires a separate revision-checked owner review.
- Address, certificate, API, model, catalog, capability, or freshness drift stales the target and removes eligibility.
- Existing provider discovery and routing tests remain green.
- Routing schema v2 upgrades additively to v3 without rewriting existing decisions or outcomes.
- Deterministic mocks prove safety; a controlled two-machine test proves the installed network path.
