# Adaptive Flock Qualification and Scoped Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert Kestrel’s existing learned-shadow routing evidence into bounded, replayable production qualification receipts and exact owner-approved activation grants for low/medium-risk project/task-family scopes.

**Architecture:** Extend the existing routing ledger rather than building a second router. A qualification service snapshots exact scope, eligible inventory, prices, policy, learned configuration, project authority, and a hybrid corpus. A durable runner executes a fair target/case matrix through existing provider and validation paths under an immutable USD hard cap, records all attempts, and replays the ordered evidence twenty times. Terminal receipts are append-only and authenticated. Qualification creates evidence only. A separate revision-checked owner transaction creates one immutable grant per exact scope. At route-decision time, an activation evaluator verifies the current grant and bindings; absent, stale, suspended, or revoked authority falls back to deterministic static routing with a durable reason.

**Tech Stack:** Python 3.11, existing `RoutingLedger`, `DurableRoutingCoordinator`, `AdaptiveFlockRoutingService`, `LearnedRouterConfig`, `AdaptiveFlockRunManager`, SQLite routing schema v4, FastAPI/Pydantic, HMAC-SHA256 authenticated local receipts using owner-only key material, React/Vitest Flock workspace, deterministic mock/replay fixtures, and installed-artifact live-provider qualification gates.

## Global Constraints

- Implement after explicit LAN discovery. This plan owns routing schema migration `3 -> 4`.
- Preserve existing static, shadow, constrained, and adaptive routing behavior until an effective durable grant exists.
- The environment flag `NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED` may remain a global master permit during migration, but it is never sufficient authority.
- Qualification and activation never change task contracts, graphs, tools, workspace, network, secrets, budgets, approvals, privacy class, containment, or eligible target filters.
- Learned selection applies only after normal hard filters and only among already eligible targets.
- High/critical-risk routes remain deterministic. They may collect shadow diagnostics but cannot qualify or activate.
- Qualification never activates itself. Only an authenticated owner API action may create grants.
- Every selected comparative scope needs at least two real eligible targets and owner-approved real-project evidence before production activation.
- The corpus is hybrid: checked-in deterministic fixtures plus owner-selected real project tasks with trusted acceptance evidence.
- Default maximum qualification spend is USD 50.00 and is owner-editable before start. Store money as integer micro-USD; never binary floating-point.
- Snapshot the immutable maximum at start. It cannot increase. The owner may lower the effective stop cap, pause, resume, or cancel.
- Before every provider attempt, transactionally reserve conservative projected cost. Missing usage never releases a reservation to zero.
- A billed target without a trustworthy price cannot be admitted. A local/private non-billed target needs an explicit zero-cost price source; “unknown” is not zero.
- Transport outage, capability failure, contract failure, task-quality failure, validation failure, guardrail failure, cancellation, and budget rejection remain distinct.
- Provider outages do not punish learned task quality.
- Require defaults: 5 examples/scope, 3 examples/selected target, confidence 0.70, utility margin 0.08, attributable cost coverage 0.80, decay half-life 30 days, zero guardrail violations, replay 20/20.
- Store qualification/grant records in SQLite and evidence artifacts, not Memvid. Do not write policy memory from qualification or activation.
- Every mutation is revision-checked and owner-authenticated. Every receipt/grant transition is secret-safe and append-only.
- Exact route leases remain sticky for an in-flight attempt. Revocation affects new leases immediately and does not silently swap models mid-tool-loop.
- Keep deterministic fakes for clocks, IDs, prices, provider attempts, executor, and validation. Live-provider tests are gated and cannot be replaced by mock claims.
- Run focused tests after every task and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q` after every phase.

---

## Phase 1: Define Canonical Qualification Values and Durable Records

### Task 1: Add exact scope, money, price, and digest models

**Files:**

- Create: `src/nested_memvid_agent/routing/qualification_models.py`
- Create: `src/nested_memvid_agent/routing/qualification_digest.py`
- Create: `tests/test_flock_qualification_models.py`
- Modify: `src/nested_memvid_agent/routing/__init__.py`

**Interfaces:**

- Produce: `MoneyMicros`, `QualificationScope`, `QualificationThresholds`, `PriceSnapshot`, `TargetSnapshot`, `CorpusItem`, `CorpusManifest`, and canonical digest helpers.
- Exact scope fields: project ID, task family, risk, sorted capability key, policy ID/revision, eligible target IDs, target inventory digest, price digest, learned-config digest, and project-authority digest.
- Invariant: equivalent unordered inputs produce identical canonical digests; cross-project or risk/capability changes produce different digests.

- [ ] **Step 1: Write failing canonicalization tests**

```python
def test_money_uses_exact_micro_usd() -> None:
    assert MoneyMicros.from_usd_text("50.00").micros == 50_000_000
    assert MoneyMicros.from_usd_text("0.000001").micros == 1
    with pytest.raises(ValueError, match="at most six decimal places"):
        MoneyMicros.from_usd_text("0.0000001")


def test_scope_digest_is_order_independent_but_authority_sensitive() -> None:
    first = qualification_scope(
        capabilities=("tools", "json"),
        target_ids=("target_b", "target_a"),
    )
    second = qualification_scope(
        capabilities=("json", "tools"),
        target_ids=("target_a", "target_b"),
    )
    assert first.digest == second.digest
    assert replace(first, project_authority_digest="b" * 64).digest != first.digest
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_models.py
```

Expected: qualification models absent.

- [ ] **Step 3: Implement immutable validated dataclasses**

Use integer arithmetic:

```python
@dataclass(frozen=True, order=True)
class MoneyMicros:
    micros: int

    def __post_init__(self) -> None:
        if isinstance(self.micros, bool) or self.micros < 0:
            raise ValueError("money must be a non-negative integer micro-USD value")
```

Represent price source as one of:

```python
PriceSource = Literal[
    "provider_published",
    "operator_verified",
    "operator_confirmed_non_billed_local",
    "unknown",
]
```

An explicit non-billed local price is known zero and includes owner/time/source provenance. Unknown is never converted to zero. Canonical JSON sorts map keys and semantic sets, preserves ordered evidence lists, rejects NaN/Infinity, and hashes UTF-8 bytes with SHA-256.

- [ ] **Step 4: Run model and existing learned-router tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_models.py \
  tests/test_learned_routing.py \
  tests/test_adaptive_flock_learned_runtime.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/qualification_models.py \
  src/nested_memvid_agent/routing/qualification_digest.py \
  src/nested_memvid_agent/routing/__init__.py \
  tests/test_flock_qualification_models.py
git commit -m "feat: define canonical Flock qualification values"
```

### Task 2: Add routing schema v4 qualification and grant tables

**Files:**

- Modify: `src/nested_memvid_agent/routing/ledger_schema.py`
- Create: `src/nested_memvid_agent/routing/qualification_records.py`
- Create: `src/nested_memvid_agent/routing/qualification_serialization.py`
- Create: `src/nested_memvid_agent/routing/qualification_ledger.py`
- Create: `tests/test_flock_qualification_ledger.py`
- Modify: `tests/test_agent_routing_ledger.py`
- Modify: `tests/test_lan_discovery_ledger.py`

**Interfaces:**

- Schema version: `ROUTING_SCHEMA_VERSION = 4`.
- Tables:
  - `routing_qualification_runs`
  - `routing_qualification_cases`
  - `routing_qualification_attempts`
  - `routing_qualification_events`
  - `routing_qualification_receipts`
  - `routing_activation_grants`
  - `routing_activation_transitions`
- Add nullable activation columns to `routing_decisions`: `activation_grant_id`, `activation_receipt_id`, `activation_effective`, and `activation_reason`.
- Run states: `draft`, `ready`, `running`, `pausing`, `paused`, `cancelled`, `failed`, `completed`.
- Attempt states: `pending`, `reserved`, `running`, `completed`, `failed`, `cancelled`, `ambiguous`.

- [ ] **Step 1: Write failing v3 migration and race tests**

```python
def test_routing_v3_migrates_to_v4_without_rewriting_existing_evidence(
    v3_state: AgentStateStore,
) -> None:
    before = routing_and_lan_digest(v3_state)
    ledger = RoutingLedger(v3_state)
    assert ledger.schema_version() == 4
    assert routing_and_lan_digest(v3_state) == before


def test_qualification_run_revision_race_has_one_winner(
    qualification_ledger: QualificationLedger,
) -> None:
    run = qualification_ledger.create_run(run_draft())
    first = qualification_ledger.mark_ready(run.run_id, expected_revision=1)
    assert first.revision == 2
    with pytest.raises(QualificationRevisionConflict) as raised:
        qualification_ledger.mark_ready(run.run_id, expected_revision=1)
    assert raised.value.current_revision == 2
```

Add tests that receipt and grant base rows cannot be updated/deleted and activation transitions cannot be rewritten.

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_ledger.py \
  tests/test_agent_routing_ledger.py \
  tests/test_lan_discovery_ledger.py
```

Expected: schema remains v3 and records are absent.

- [ ] **Step 3: Implement additive schema v4**

Use integer micro-USD columns:

- immutable max spend;
- mutable effective stop cap;
- known actual spend;
- unresolved cost reserve;
- admitted in-flight reserve;
- per-attempt ceiling.

Store canonical JSON plus digest columns for scope/corpus/target/price/policy/learned/project authority/build. Cases bind immutable task contract, acceptance plan, repository/evidence digest, privacy eligibility, and scope digest. Attempts bind case, target, routing decision/lease, provider receipts, usage, reservation, actual/unresolved cost, validation, failure category, guardrail state, and bounded evidence references.

Create SQL triggers that reject update/delete of receipt rows, grant base rows, and transition rows. Allow a run/attempt to terminalize exactly once through ledger methods; reject post-terminal evidence append.

- [ ] **Step 4: Run ledger, migration, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_ledger.py \
  tests/test_agent_routing_ledger.py \
  tests/test_lan_discovery_ledger.py \
  tests/test_state_store.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: v3 evidence digest is unchanged and all new constraints pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/ledger_schema.py \
  src/nested_memvid_agent/routing/qualification_records.py \
  src/nested_memvid_agent/routing/qualification_serialization.py \
  src/nested_memvid_agent/routing/qualification_ledger.py \
  tests/test_flock_qualification_ledger.py \
  tests/test_agent_routing_ledger.py \
  tests/test_lan_discovery_ledger.py
git commit -m "feat: persist Flock qualification and grant ledger"
```

### Task 3: Authenticate receipts with owner-only control-plane key material

**Files:**

- Create: `src/nested_memvid_agent/control_plane_integrity.py`
- Create: `tests/test_control_plane_integrity.py`
- Modify: `src/nested_memvid_agent/routing/qualification_ledger.py`
- Modify: `src/nested_memvid_agent/agent_backup.py`
- Modify: `tests/test_agent_backup.py`
- Modify: `src/nested_memvid_agent/desktop_recovery.py`

**Interfaces:**

- Owner-only key path: `<state-directory>/.routing-integrity.key`.
- Produce: `AuthenticatedPayload.sign(payload)` and `.verify(envelope)`.
- Envelope: algorithm `hmac-sha256`, key ID, payload digest, authentication tag.
- Invariant: key is generated atomically once, permission/owner checked, backed up with matching SQLite state, and never stored in SQLite, logs, API responses, or Memvid.

- [ ] **Step 1: Write failing tamper/restart/backup tests**

```python
def test_signed_receipt_survives_restart_and_rejects_tampering(tmp_path: Path) -> None:
    signer = ControlPlaneIntegrity(tmp_path)
    envelope = signer.sign({"receipt_id": "receipt_1", "qualified": True})
    assert ControlPlaneIntegrity(tmp_path).verify(envelope)
    envelope["payload"]["qualified"] = False
    assert not ControlPlaneIntegrity(tmp_path).verify(envelope)


def test_backup_keeps_state_and_routing_key_together(tmp_path: Path) -> None:
    manifest = create_agent_backup_with_routing_receipt(tmp_path)
    assert "state/agent.db" in manifest.paths
    assert "state/.routing-integrity.key" in manifest.paths
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_control_plane_integrity.py \
  tests/test_agent_backup.py
```

Expected: signer/key backup absent.

- [ ] **Step 3: Implement using existing private artifact patterns**

Follow the atomic key publication/recovery pattern already used for `.validation-integrity.key`, but keep the routing key in the state directory and do not import memory-layer internals. Use `hmac.compare_digest`. Refuse symlink, wrong owner, group/world readable, malformed base64, wrong length, or ambiguous temp/final key states.

Backups/restores must verify the database and key belong to the same manifest before replacing either. Desktop recovery reports `routing_integrity_key_missing_or_mismatched`; it does not generate a new key over existing signed receipts.

- [ ] **Step 4: Run integrity, backup, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_control_plane_integrity.py \
  tests/test_agent_backup.py \
  tests/test_private_artifact_permissions.py \
  tests/test_desktop_recovery.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/control_plane_integrity.py \
  src/nested_memvid_agent/routing/qualification_ledger.py \
  src/nested_memvid_agent/agent_backup.py \
  src/nested_memvid_agent/desktop_recovery.py \
  tests/test_control_plane_integrity.py \
  tests/test_agent_backup.py
git commit -m "feat: authenticate routing qualification receipts"
```

---

## Phase 2: Build an Exact Qualification Draft

### Task 4: Add shipped deterministic corpus fixtures and validators

**Files:**

- Create: `src/nested_memvid_agent/qualification_fixtures/v1/manifest.json`
- Create: `src/nested_memvid_agent/qualification_fixtures/v1/routing_guardrails.json`
- Create: `src/nested_memvid_agent/qualification_fixtures/v1/cost_accounting.json`
- Create: `src/nested_memvid_agent/qualification_fixtures/v1/abstention.json`
- Create: `src/nested_memvid_agent/routing/qualification_corpus.py`
- Create: `tests/test_flock_qualification_corpus.py`
- Modify: `pyproject.toml`
- Modify: `MANIFEST.in`
- Modify: `tests/test_packaging_deployment.py`

**Interfaces:**

- Produce immutable fixture `CorpusItem`s with fixed task contracts, expected outcome categories, and trusted deterministic validators.
- Fixtures cover schema, hard filters, replay, abstention, failure categories, usage accounting, and cap admission.
- Invariant: fixture-only evidence is marked `synthetic` and cannot satisfy live production activation.

- [ ] **Step 1: Write failing fixture integrity tests**

```python
def test_shipped_fixture_manifest_is_complete_and_digest_bound() -> None:
    corpus = load_shipped_qualification_corpus()
    assert {item.fixture_id for item in corpus.items} == {
        "routing_guardrails_v1",
        "cost_accounting_v1",
        "abstention_v1",
    }
    assert corpus.digest == EXPECTED_V1_CORPUS_DIGEST


def test_fixture_evidence_cannot_claim_live_provider_qualification() -> None:
    result = evaluate_scope(fixture_only_attempts())
    assert result.qualified is False
    assert "real_project_evidence_required" in result.reasons
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_corpus.py \
  tests/test_packaging_deployment.py
```

Expected: corpus package absent.

- [ ] **Step 3: Add stable fixture package**

Each fixture includes:

- schema/version and stable ID;
- exact prompt/task contract;
- low/medium/high risk label;
- required capabilities;
- deterministic target/replay inputs;
- acceptance validator ID and parameters;
- expected outcome/failure/abstention category;
- fixture file digest.

Load through package resources so frozen builds include the same bytes. Validate every file before use. Reject unregistered validator names.

- [ ] **Step 4: Run corpus and package tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_corpus.py \
  tests/test_packaging_deployment.py \
  tests/test_learned_routing.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/qualification_fixtures \
  src/nested_memvid_agent/routing/qualification_corpus.py \
  tests/test_flock_qualification_corpus.py \
  pyproject.toml MANIFEST.in tests/test_packaging_deployment.py
git commit -m "feat: ship deterministic Flock qualification corpus"
```

### Task 5: Import only repeatable owner-selected real project tasks

**Files:**

- Create: `src/nested_memvid_agent/routing/qualification_real_tasks.py`
- Create: `tests/test_flock_qualification_real_tasks.py`
- Modify: `src/nested_memvid_agent/state_store.py`
- Modify: `src/nested_memvid_agent/project_policy.py`
- Modify: `src/nested_memvid_agent/context_frames.py`
- Modify: `tests/test_context_frames.py`
- Modify: `tests/test_adaptive_flock_project_policy.py`

**Interfaces:**

- Consume: project ID plus selected completed task/run evidence IDs.
- Produce: immutable `CorpusItem`s with project/tree digest, exact task contract, acceptance plan, privacy exposure approval, and authenticated validation references.
- Invariant: an item is diagnostic-only unless its outcome is independently validated and replay-comparable.
- Invariant: no child frame, direct lookup, imported capsule, or evidence reference may cross the selected project boundary.

- [ ] **Step 1: Write failing project-isolation and acceptance tests**

```python
def test_cross_project_task_cannot_enter_corpus(
    importer: RealTaskCorpusImporter,
) -> None:
    with pytest.raises(ValueError, match="selected project"):
        importer.import_tasks(
            project_id="project_a",
            task_ids=["task_from_project_b"],
        )


def test_untrusted_or_self_reported_success_is_diagnostic_only(
    importer: RealTaskCorpusImporter,
) -> None:
    item = importer.import_tasks(
        project_id="project_a",
        task_ids=["task_without_validation"],
    )[0]
    assert item.actionable is False
    assert item.exclusion_reasons == ("trusted_acceptance_evidence_missing",)
```

Add child-frame and direct ID lookup tests that attempt to smuggle project B evidence into project A.

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_real_tasks.py \
  tests/test_context_frames.py \
  tests/test_adaptive_flock_project_policy.py
```

Expected: importer absent.

- [ ] **Step 3: Implement import and safety classification**

Require:

- selected task belongs to a selected-project run;
- low/medium risk for actionable qualification;
- authenticated test/review/validation receipt bound to exact repository/tree/diff;
- immutable acceptance plan;
- no registered secret value in prompt/contract/artifacts;
- explicit owner approval for each target privacy class;
- repeatability classification (`read_only`, `isolated_worktree`, or `qualified_containment`);
- path ceiling and project authority digest.

High-risk tasks and tasks lacking trusted acceptance remain visible diagnostics only. Do not mutate their original runs/tasks.

- [ ] **Step 4: Run project/context/full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_real_tasks.py \
  tests/test_context_frames.py \
  tests/test_adaptive_flock_project_policy.py \
  tests/test_projects.py \
  tests/test_security_boundary.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/qualification_real_tasks.py \
  src/nested_memvid_agent/state_store.py \
  src/nested_memvid_agent/project_policy.py \
  src/nested_memvid_agent/context_frames.py \
  tests/test_flock_qualification_real_tasks.py \
  tests/test_context_frames.py \
  tests/test_adaptive_flock_project_policy.py
git commit -m "feat: import project-isolated real qualification tasks"
```

### Task 6: Snapshot all eligible targets, prices, policies, and authority

**Files:**

- Create: `src/nested_memvid_agent/routing/qualification_snapshot.py`
- Create: `src/nested_memvid_agent/routing/qualification_preview.py`
- Create: `tests/test_flock_qualification_preview.py`
- Modify: `src/nested_memvid_agent/routing/service.py`
- Modify: `src/nested_memvid_agent/routing/router.py`
- Modify: `tests/test_adaptive_flock_preview.py`
- Modify: `tests/test_agent_routing_guardrails.py`

**Interfaces:**

- Consume: project, selected task families, policy, hybrid corpus, current provider profiles/targets, and current project authority.
- Produce: read-only `QualificationPreview` with exact scope list, all eligible targets per scope, exclusions, price/freshness warnings, matrix size, estimated reserved-cost range, and preview digest.
- Invariant: use the normal hard eligibility filters, not a parallel approximation.

- [ ] **Step 1: Write failing all-target and drift tests**

```python
def test_preview_includes_every_target_eligible_for_any_selected_scope(
    previewer: QualificationPreviewService,
) -> None:
    preview = previewer.preview(draft_with_two_scopes())
    assert preview.target_snapshot.target_ids == (
        "local_qwen",
        "lan_deepseek",
        "cloud_frontier",
    )
    assert preview.excluded_targets["stale_lan"] == ("target_stale",)


def test_start_rejects_inventory_drift_after_preview(
    previewer: QualificationPreviewService,
) -> None:
    preview = previewer.preview(draft())
    mutate_target_model()
    with pytest.raises(ValueError, match="target_inventory_changed"):
        previewer.revalidate_for_start(preview)
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_preview.py \
  tests/test_adaptive_flock_preview.py \
  tests/test_agent_routing_guardrails.py
```

Expected: preview service absent.

- [ ] **Step 3: Expose/reuse deterministic eligibility evaluation**

Refactor the current private eligibility path only enough to return:

```python
@dataclass(frozen=True)
class EligibilityEvaluation:
    target: ModelTarget
    eligible: bool
    reason_codes: tuple[str, ...]
```

Both ordinary routing and qualification call this exact function. Snapshot target/provider IDs, adapters, model IDs, endpoints/trust/locality, enabled/health/freshness, capabilities with provenance, privacy/network constraints, quality/limits, prices/currency/source/time, and configuration digests.

Require at least two eligible targets per comparative scope. Unknown billed price is a start blocker for that target. Explicit non-billed local zero pricing is accepted with provenance. If a selected target is excluded, show why; do not silently shrink owner intent.

- [ ] **Step 4: Run preview, guardrail, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_preview.py \
  tests/test_adaptive_flock_preview.py \
  tests/test_agent_routing_guardrails.py \
  tests/test_adaptive_flock_provider_update_semantics.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass and ordinary routing decisions are unchanged for the same inputs.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/qualification_snapshot.py \
  src/nested_memvid_agent/routing/qualification_preview.py \
  src/nested_memvid_agent/routing/service.py \
  src/nested_memvid_agent/routing/router.py \
  tests/test_flock_qualification_preview.py \
  tests/test_adaptive_flock_preview.py \
  tests/test_agent_routing_guardrails.py
git commit -m "feat: preview exact all-target Flock qualification"
```

### Task 7: Implement transactional hard-cap admission

**Files:**

- Create: `src/nested_memvid_agent/routing/qualification_budget.py`
- Create: `tests/test_flock_qualification_budget.py`
- Modify: `src/nested_memvid_agent/routing/qualification_ledger.py`

**Interfaces:**

- Default immutable maximum: `50_000_000` micro-USD.
- Produce: `estimate_attempt_reserve`, `admit_attempt`, `settle_attempt`, and `lower_effective_stop_cap`.
- Admission condition:

```text
known_actual_spend
+ unresolved_cost_reserve
+ admitted_inflight_reserve
+ projected_attempt_reserve
<= min(immutable_max_spend, effective_stop_cap)
```

- Invariant: reserve and attempt transition happen in one SQLite transaction.

- [ ] **Step 1: Write failing exact-exhaustion and unknown-usage tests**

```python
def test_exact_cap_allows_equal_reservation_and_rejects_next(
    budget: QualificationBudget,
) -> None:
    budget.admit(attempt("a", reserve=40_000_000))
    budget.admit(attempt("b", reserve=10_000_000))
    with pytest.raises(BudgetAdmissionRejected, match="hard_cap_exhausted"):
        budget.admit(attempt("c", reserve=1))


def test_missing_usage_keeps_reservation_as_unresolved_cost(
    budget: QualificationBudget,
) -> None:
    budget.admit(attempt("a", reserve=2_500_000))
    state = budget.settle("a", usage=None, actual_cost=None)
    assert state.known_actual_spend_micros == 0
    assert state.unresolved_cost_reserve_micros == 2_500_000
    assert state.cost_coverage == 0.0


def test_owner_cannot_raise_cap_after_start(budget: QualificationBudget) -> None:
    with pytest.raises(ValueError, match="cannot be raised"):
        budget.set_effective_stop_cap(60_000_000)
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_budget.py
```

Expected: budget service absent.

- [ ] **Step 3: Implement conservative reserve accounting**

Compute reserve from snapshotted input/output per-million micro-USD prices and immutable token ceilings, rounding up. Local explicit non-billed price yields zero known reserve. Unknown price rejects admission before provider contact.

On settlement:

- reported usage and valid snapshot -> replace reserve with exact rounded-up actual;
- missing usage -> move full reserve to unresolved;
- actual above reserve -> record `budget_projection_overrun`, charge actual, stop admissions, and make affected scopes non-qualifying;
- transport failure with confirmed zero tokens -> release reserve only when the provider adapter proves no request was accepted;
- ambiguous transport outcome -> keep reserve unresolved.

Cost coverage is:

```text
terminal live attempt units with attributable known cost
--------------------------------------------------------
all terminal live attempt units
```

Synthetic fixtures do not increase production cost coverage.

- [ ] **Step 4: Run budget/race/full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_budget.py \
  tests/test_adaptive_flock_run_manager.py \
  tests/test_provider_resilience.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/qualification_budget.py \
  src/nested_memvid_agent/routing/qualification_ledger.py \
  tests/test_flock_qualification_budget.py
git commit -m "feat: enforce exact Flock qualification spend caps"
```

---

## Phase 3: Execute the Hybrid Corpus Durably

### Task 8: Define an isolated qualification executor and deterministic fake

**Files:**

- Create: `src/nested_memvid_agent/routing/qualification_executor.py`
- Create: `src/nested_memvid_agent/routing/qualification_workspace.py`
- Create: `tests/test_flock_qualification_executor.py`
- Create: `tests/_qualification_fakes.py`
- Modify: `src/nested_memvid_agent/routing/coordinator.py`
- Modify: `src/nested_memvid_agent/routing/native_worker.py`
- Modify: `tests/test_native_worker.py`

**Interfaces:**

- Produce: `QualificationExecutor.execute(AttemptLease) -> AttemptEvidence`.
- Attempt lease binds run/case/target, exact task contract, project/tree digest, target/price/policy/config digests, budget reservation, containment mode, and idempotency key.
- Production adapter uses existing routing/provider/tool/validation services with `direct_target_id`; it does not bypass eligibility.
- Invariant: candidate code executes only in a qualified containment path or isolated worktree allowed by the corpus item.

- [ ] **Step 1: Write failing direct-target and containment tests**

```python
def test_executor_forces_matrix_target_through_normal_eligibility(
    executor: QualificationExecutor,
) -> None:
    evidence = executor.execute(lease(target_id="target_b"))
    assert evidence.actual_target_id == "target_b"
    assert evidence.route_decision.hard_filter_reasons == ()


def test_candidate_code_never_falls_back_to_host_when_containment_missing(
    executor: QualificationExecutor,
) -> None:
    with pytest.raises(QualificationAttemptBlocked, match="containment_required"):
        executor.execute(code_task_lease(containment_available=False))
    assert executor.provider.calls == []
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_executor.py \
  tests/test_native_worker.py
```

Expected: executor absent.

- [ ] **Step 3: Implement the interface and fake first**

The deterministic fake emits exact provider attempts, token usage, cost, latency, validation, failure category, and evidence references from fixture inputs. The production adapter:

1. verifies attempt lease/digests;
2. stages read-only or isolated-worktree project state;
3. calls current eligibility and direct-target routing;
4. persists route decision/lease before provider execution;
5. invokes the existing provider/tool loop under task capability ceilings;
6. runs trusted validators/review;
7. records bounded evidence;
8. leaves the attempt workspace for receipt-bound cleanup/review.

Never reuse the candidate model’s self-report as validation.

- [ ] **Step 4: Run executor, routing, containment, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_executor.py \
  tests/test_native_worker.py \
  tests/test_agent_routing_guardrails.py \
  tests/test_skill_containment.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/qualification_executor.py \
  src/nested_memvid_agent/routing/qualification_workspace.py \
  src/nested_memvid_agent/routing/coordinator.py \
  src/nested_memvid_agent/routing/native_worker.py \
  tests/test_flock_qualification_executor.py \
  tests/_qualification_fakes.py \
  tests/test_native_worker.py
git commit -m "feat: execute qualification attempts through governed routing"
```

### Task 9: Implement the durable qualification run manager

**Files:**

- Create: `src/nested_memvid_agent/routing/qualification_runner.py`
- Create: `tests/test_flock_qualification_runner.py`
- Modify: `src/nested_memvid_agent/routing/qualification_ledger.py`
- Modify: `src/nested_memvid_agent/server.py`

**Interfaces:**

- Produce: create/ready/start/pause/resume/cancel/recover lifecycle.
- Fair matrix order: round-robin by case, then target, with stable ID tie-breaks.
- Configurable concurrency is bounded by provider profile concurrency, project containment capacity, and a server maximum.
- Invariant: restart never repeats a running/ambiguous provider request automatically.

- [ ] **Step 1: Write failing lifecycle, fairness, and recovery tests**

```python
def test_matrix_admission_is_stable_and_round_robin_by_target(
    runner: QualificationRunner,
) -> None:
    runner.start(run_with_cases(2, targets=("a", "b", "c")))
    assert runner.executor.started_attempts == [
        ("case_1", "a"),
        ("case_1", "b"),
        ("case_1", "c"),
        ("case_2", "a"),
        ("case_2", "b"),
        ("case_2", "c"),
    ]


def test_restart_does_not_repeat_ambiguous_attempt(
    state_with_running_attempt: AgentStateStore,
) -> None:
    runner = QualificationRunner.recover(
        state_with_running_attempt,
        executor=recording_executor(),
    )
    assert runner.executor.calls == []
    assert runner.get_attempt("attempt_1").status == "ambiguous"
    assert "owner_reconciliation_required" in runner.get("qual_1").blockers
```

Test `running -> pausing -> paused`, lower cap while running, cancellation, worker failure, UI disconnect, and terminal receipt initiation.

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_runner.py
```

Expected: runner absent.

- [ ] **Step 3: Implement lease-based execution**

Persist an attempt as `reserved` with budget reservation before submitting work. Transition to `running` only when the executor owns it. Persist each provider/validation result before admitting the next attempt.

Pause stops new admission and waits for bounded in-flight attempts. Cancel does the same, terminalizes pending attempts cancelled, preserves evidence, and creates a non-qualifying cancelled receipt. A provider exception terminalizes only its attempt with a typed category unless state integrity fails.

On sidecar startup:

- `reserved` but never dispatched -> release reservation and return to pending;
- `running` with no definitive receipt -> mark ambiguous, retain reserve, require owner reconciliation;
- `pausing` -> finish as paused;
- terminal runs stay immutable.

- [ ] **Step 4: Run runner, chaos, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_runner.py \
  tests/test_chaos_recovery.py \
  tests/test_run_backpressure.py \
  tests/test_provider_resilience.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass and shutdown leaves no worker threads.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/qualification_runner.py \
  src/nested_memvid_agent/routing/qualification_ledger.py \
  src/nested_memvid_agent/server.py \
  tests/test_flock_qualification_runner.py
git commit -m "feat: run durable hybrid Flock qualifications"
```

### Task 10: Normalize provider usage, cost, and failure evidence

**Files:**

- Create: `src/nested_memvid_agent/routing/qualification_evidence.py`
- Create: `tests/test_flock_qualification_evidence.py`
- Modify: `src/nested_memvid_agent/routing/run_manager.py`
- Modify: `src/nested_memvid_agent/routing/coordinator.py`
- Modify: `src/nested_memvid_agent/routing/ledger.py`
- Modify: `tests/test_adaptive_flock_run_manager.py`
- Modify: `tests/test_provider_resilience.py`

**Interfaces:**

- Produce: normalized `ProviderAttemptEvidence` with subject/provider/profile/model, request ID digest, accepted/ambiguous state, input/output/cached/reasoning tokens when available, snapshotted prices, known/unresolved cost, latency, and typed failure.
- Failure categories: `provider_outage`, `provider_rate_limit`, `capability_failure`, `contract_failure`, `task_quality_failure`, `validation_failure`, `guardrail_failure`, `cancelled`, `budget_rejected`, and `unknown`.
- Invariant: raw provider errors are redacted/bounded before persistence.

- [ ] **Step 1: Write failing failure-separation tests**

```python
def test_provider_outage_does_not_become_task_quality_failure() -> None:
    evidence = normalize_provider_attempt(timeout_receipt())
    assert evidence.failure_category == "provider_outage"
    examples = build_route_examples([evidence.to_learning_payload()])
    state = LearnedRouterState.from_examples(examples, LearnedRouterConfig())
    assert state.target_scores["target_a"].validation_rate == 0.0
    assert state.target_scores["target_a"].effective_sample_size == 0.0


def test_capability_and_contract_failures_remain_distinct() -> None:
    assert normalize_provider_attempt(tool_call_mismatch()).failure_category == "capability_failure"
    assert normalize_provider_attempt(invalid_task_contract()).failure_category == "contract_failure"
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_evidence.py \
  tests/test_adaptive_flock_run_manager.py \
  tests/test_provider_resilience.py
```

Expected: normalization module absent or category gaps exposed.

- [ ] **Step 3: Implement one normalization path**

Reuse the same normalization from ordinary Adaptive Flock outcomes and qualification attempts. Add evidence references to provider receipts rather than copying raw bodies. Preserve exact target and route lease identity through fallback/escalation. Capability failure may select a stronger already eligible target on a later attempt; contract failure returns to replanning and is not hidden as fallback.

- [ ] **Step 4: Run routing/provider/full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_evidence.py \
  tests/test_adaptive_flock_run_manager.py \
  tests/test_provider_resilience.py \
  tests/test_agent_routing_metadata.py \
  tests/test_agent_routing_cancellation.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/qualification_evidence.py \
  src/nested_memvid_agent/routing/run_manager.py \
  src/nested_memvid_agent/routing/coordinator.py \
  src/nested_memvid_agent/routing/ledger.py \
  tests/test_flock_qualification_evidence.py \
  tests/test_adaptive_flock_run_manager.py \
  tests/test_provider_resilience.py
git commit -m "feat: normalize attributable Flock attempt evidence"
```

---

## Phase 4: Replay and Finalize Immutable Receipts

### Task 11: Add per-scope metrics and exact qualification evaluation

**Files:**

- Create: `src/nested_memvid_agent/routing/qualification_evaluator.py`
- Create: `tests/test_flock_qualification_evaluator.py`
- Modify: `src/nested_memvid_agent/routing/learned_router.py`
- Modify: `tests/test_learned_routing.py`
- Modify: `tests/test_adaptive_flock_learned_runtime.py`

**Interfaces:**

- Consume: exact ordered terminal attempt evidence for one scope and snapshotted thresholds/config.
- Produce: `ScopeQualificationResult` with qualified/abstained/deterministic-only state, selected learned target, static target, support, confidence, utility components/delta, cost coverage, savings/regret, guardrails, and explicit reasons.
- Invariant: reuse `LearnedRouterState`, `evaluate_shadow`, and existing decay semantics.

- [ ] **Step 1: Write failing threshold matrix tests**

```python
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("four_total_examples", "sparse_evidence"),
        ("two_selected_target_examples", "sparse_target_evidence"),
        ("confidence_0_69", "low_confidence"),
        ("margin_0_079", "insufficient_utility_margin"),
        ("cost_coverage_0_79", "insufficient_cost_coverage"),
        ("one_guardrail_violation", "guardrail_violation"),
        ("high_risk", "high_risk_deterministic_only"),
        ("one_target", "comparative_target_coverage_missing"),
        ("fixture_only", "real_project_evidence_required"),
    ],
)
def test_scope_abstains_with_exact_reason(mutation: str, reason: str) -> None:
    result = evaluate_scope(mutated_evidence(mutation))
    assert result.qualified is False
    assert reason in result.reasons
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_evaluator.py \
  tests/test_learned_routing.py
```

Expected: evaluator absent.

- [ ] **Step 3: Implement transparent metrics**

Keep existing target utility math. Extend public metric projection rather than changing ranking without evidence. A scope qualifies only if:

- low or medium risk;
- exact corpus/project binding;
- at least two real eligible targets were actually evaluated;
- every snapshotted eligible target has required coverage or the scope abstains;
- total/selected-target support pass;
- confidence and utility margin pass;
- live cost coverage passes;
- zero hard guardrail violations;
- trusted real project acceptance exists;
- no budget overrun/state integrity/replay blocker.

Do not compress these into a single score.

- [ ] **Step 4: Run evaluator/learned/full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_evaluator.py \
  tests/test_learned_routing.py \
  tests/test_adaptive_flock_learned_runtime.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass and existing route selection snapshots remain stable.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/qualification_evaluator.py \
  src/nested_memvid_agent/routing/learned_router.py \
  tests/test_flock_qualification_evaluator.py \
  tests/test_learned_routing.py \
  tests/test_adaptive_flock_learned_runtime.py
git commit -m "feat: evaluate transparent qualification thresholds"
```

### Task 12: Replay twenty identical projections and finalize terminal receipts

**Files:**

- Create: `src/nested_memvid_agent/routing/qualification_replay.py`
- Create: `src/nested_memvid_agent/routing/qualification_receipt.py`
- Create: `tests/test_flock_qualification_replay.py`
- Create: `tests/test_flock_qualification_receipt.py`
- Modify: `src/nested_memvid_agent/routing/qualification_runner.py`
- Modify: `src/nested_memvid_agent/routing/qualification_ledger.py`

**Interfaces:**

- Produce: 20 replay records with projection digest; qualification requires one unique digest and 20 passes.
- Produce authenticated terminal receipt for every `completed`, `failed`, or `cancelled` run.
- Only `completed` may contain qualified scopes.
- Invariant: the exact ordered evidence set and config digest are replay inputs; database query order is never implicit.

- [ ] **Step 1: Write failing order/drift/terminal tests**

```python
def test_twenty_replays_are_identical(replayer: QualificationReplayer) -> None:
    result = replayer.replay(evidence_fixture(), repeats=20)
    assert result.completed_repeats == 20
    assert result.unique_projection_digests == 1
    assert result.passed is True


def test_single_projection_drift_blocks_scope() -> None:
    result = QualificationReplayer(clock=drifting_clock()).replay(
        evidence_fixture(),
        repeats=20,
    )
    assert result.passed is False
    assert result.unique_projection_digests == 2
    assert "replay_drift" in result.reasons


def test_cancelled_receipt_cannot_contain_qualified_scope() -> None:
    with pytest.raises(ValueError, match="cancelled receipt"):
        build_terminal_receipt(status="cancelled", scopes=[qualified_scope()])
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_replay.py \
  tests/test_flock_qualification_receipt.py
```

Expected: modules absent.

- [ ] **Step 3: Implement deterministic replay and receipt finalization**

Sort evidence once by stable `(scope_digest, case_id, target_id, attempt_ordinal, attempt_id)` and include that ordered manifest digest in every replay. Freeze the reference time from the run snapshot so decay does not change between repeats.

Receipt payload includes:

- run/status/owner/project/timestamps/build/schema IDs;
- scope/corpus/target/price/policy/learned/project authority digests;
- immutable max and effective cap revision history;
- known spend, unresolved reserve, cost coverage;
- case/attempt/failure/guardrail summaries linked to raw evidence IDs;
- all per-scope metrics/results/reasons;
- all 20 replay projection digests;
- payload digest and authentication envelope.

Finalize in one transaction that writes receipt, links run, marks terminal, and appends terminal event. If signing or persistence fails, leave the run `failed` with a separate recovery blocker; never invent an unsigned qualifying receipt.

- [ ] **Step 4: Run replay, receipt, runner, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_replay.py \
  tests/test_flock_qualification_receipt.py \
  tests/test_flock_qualification_runner.py \
  tests/test_control_plane_integrity.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/qualification_replay.py \
  src/nested_memvid_agent/routing/qualification_receipt.py \
  src/nested_memvid_agent/routing/qualification_runner.py \
  src/nested_memvid_agent/routing/qualification_ledger.py \
  tests/test_flock_qualification_replay.py \
  tests/test_flock_qualification_receipt.py
git commit -m "feat: finalize replayed Flock qualification receipts"
```

---

## Phase 5: Create and Enforce Exact Owner Grants

### Task 13: Add activation preview and transactional grant creation

**Files:**

- Create: `src/nested_memvid_agent/routing/activation_service.py`
- Create: `tests/test_flock_activation_service.py`
- Modify: `src/nested_memvid_agent/routing/qualification_ledger.py`

**Interfaces:**

- Produce: `preview_activation(receipt_id, scope_digests)` and `activate_scopes(..., expected_revisions)`.
- Store one immutable grant per exact scope even when one owner action selects several.
- Initial transition is `active`; later states are `suspended`, `revoked`, or `superseded`.
- Invariant: transaction revalidates receipt authentication and every current binding after preview.

- [ ] **Step 1: Write failing qualification/authority/race tests**

```python
def test_qualification_receipt_does_not_create_authority(
    service: ActivationService,
) -> None:
    receipt = completed_qualified_receipt()
    assert service.list_grants(receipt_id=receipt.receipt_id) == []


def test_multi_scope_activation_is_all_or_nothing(
    service: ActivationService,
) -> None:
    with pytest.raises(ActivationConflict, match="project_authority_changed"):
        service.activate_scopes(
            activation_request(scopes=("scope_a", "scope_b_stale")),
        )
    assert service.list_grants() == []


def test_only_owner_principal_can_confirm(service: ActivationService) -> None:
    with pytest.raises(PermissionError, match="owner confirmation required"):
        service.activate_scopes(agent_principal_request())
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_activation_service.py
```

Expected: activation service absent.

- [ ] **Step 3: Implement exact activation packets**

Preview shows project/task family/risk/capability scope, static/learned targets, alternatives, support, confidence, utility, cost coverage, replay, guardrails, inventory/prices, authority change, suspension conditions, and revocation behavior.

Activation transaction:

1. verify owner principal and expected receipt revision/digest;
2. verify HMAC receipt;
3. verify completed/qualified selected scopes;
4. recompute project authority, target inventory, price, policy, and learned config digests;
5. require global master permit but do not derive authority from it;
6. insert one base grant and one active transition per scope;
7. supersede an older exact-scope grant in the same transaction;
8. append activation event.

- [ ] **Step 4: Run activation/ledger/full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_activation_service.py \
  tests/test_flock_qualification_ledger.py \
  tests/test_control_plane_integrity.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/activation_service.py \
  src/nested_memvid_agent/routing/qualification_ledger.py \
  tests/test_flock_activation_service.py
git commit -m "feat: create exact owner-approved routing grants"
```

### Task 14: Evaluate effective grants and append automatic suspensions

**Files:**

- Create: `src/nested_memvid_agent/routing/activation_evaluator.py`
- Create: `tests/test_flock_activation_evaluator.py`
- Modify: `src/nested_memvid_agent/routing/activation_service.py`
- Modify: `src/nested_memvid_agent/routing/qualification_ledger.py`
- Modify: `src/nested_memvid_agent/lan_discovery_service.py`

**Interfaces:**

- Produce: `ActivationEvaluation(effective, grant_id, receipt_id, reason_codes, learned_state)`.
- Evaluate at every new route decision.
- Automatically append `suspended` transition for material binding drift.
- Ephemeral transport outage returns normal routing failure/fallback and does not rewrite historical quality or suspend solely by itself.

- [ ] **Step 1: Write one test per binding class**

```python
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("receipt_tampered", "receipt_authentication_failed"),
        ("evidence_decayed", "evidence_below_threshold"),
        ("project_authority_changed", "project_authority_changed"),
        ("privacy_changed", "privacy_binding_changed"),
        ("target_inventory_changed", "target_inventory_changed"),
        ("model_changed", "target_inventory_changed"),
        ("endpoint_changed", "target_inventory_changed"),
        ("price_changed", "price_snapshot_changed"),
        ("policy_changed", "routing_policy_changed"),
        ("learned_config_changed", "learned_configuration_changed"),
        ("target_ineligible", "target_hard_ineligible"),
        ("replay_failed", "replay_verification_failed"),
        ("global_kill_switch", "global_learned_authority_disabled"),
    ],
)
def test_material_drift_suspends_grant(mutation: str, reason: str) -> None:
    evaluator = evaluator_with_active_grant()
    apply_mutation(mutation)
    result = evaluator.evaluate(task_contract())
    assert result.effective is False
    assert reason in result.reason_codes
    assert latest_transition().status == "suspended"
```

Add a provider-outage test asserting the grant remains active.

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_activation_evaluator.py
```

Expected: evaluator absent.

- [ ] **Step 3: Implement evaluation in deterministic order**

Evaluate:

1. current grant transition;
2. global/scope kill switches;
3. exact project/family/risk/capability scope;
4. low/medium risk;
5. receipt authentication and raw evidence links;
6. project/privacy authority;
7. policy/learned configuration;
8. inventory/endpoint/model/trust/capabilities/prices;
9. current hard eligibility;
10. decayed support/confidence/utility/cost coverage;
11. deterministic replay.

Return all safe reason codes but no secret material. Append suspension with expected latest transition revision. Concurrent evaluators may race; one wins and the others reload the same terminal state.

- [ ] **Step 4: Run evaluator, LAN drift, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_activation_evaluator.py \
  tests/test_lan_discovery_service.py \
  tests/test_adaptive_flock_provider_update_semantics.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/activation_evaluator.py \
  src/nested_memvid_agent/routing/activation_service.py \
  src/nested_memvid_agent/routing/qualification_ledger.py \
  src/nested_memvid_agent/lan_discovery_service.py \
  tests/test_flock_activation_evaluator.py
git commit -m "feat: suspend stale learned-routing authority"
```

### Task 15: Gate the existing coordinator and preserve sticky leases

**Files:**

- Modify: `src/nested_memvid_agent/routing/coordinator.py`
- Modify: `src/nested_memvid_agent/routing/ledger.py`
- Modify: `src/nested_memvid_agent/routing/ledger_records.py`
- Modify: `src/nested_memvid_agent/routing/ledger_serialization.py`
- Modify: `src/nested_memvid_agent/routing/runtime.py`
- Modify: `src/nested_memvid_agent/routing/run_manager.py`
- Modify: `tests/test_adaptive_flock_learned_runtime.py`
- Create: `tests/test_flock_grant_runtime.py`
- Modify: `tests/test_adaptive_flock_env_config.py`
- Modify: `tests/test_agent_routing_cancellation.py`

**Interfaces:**

- `DurableRoutingCoordinator` consumes an `ActivationEvaluator`.
- Persist static, learned shadow, actual decision, grant/receipt binding, and effective/abstention reason before provider execution.
- No effective grant -> static assignment, mission continues.
- Existing route decision reuse preserves its original grant/lease binding.

- [ ] **Step 1: Write failing static-fallback and sticky-lease tests**

```python
def test_adaptive_mode_without_grant_uses_static_and_records_reason() -> None:
    durable = coordinator_with_learned_winner_but_no_grant().assign(
        base_config(),
        task(),
        subagent_id=None,
        attempt=1,
    )
    assert durable.assignment.decision.selected_target.target_id == "static_target"
    assert durable.record.activation_effective is False
    assert durable.record.activation_reason == "durable_grant_required"


def test_revocation_affects_new_lease_not_existing_attempt() -> None:
    coordinator = coordinator_with_active_grant()
    first = coordinator.assign(base_config(), task(), subagent_id=None, attempt=1)
    revoke(first.record.activation_grant_id)
    reused = coordinator.assign(base_config(), task(), subagent_id=None, attempt=1)
    second = coordinator.assign(base_config(), task(), subagent_id=None, attempt=2)
    assert reused.record.decision_id == first.record.decision_id
    assert reused.assignment.decision.selected_target == first.assignment.decision.selected_target
    assert second.record.activation_effective is False
```

Add high-risk tests that an active malformed grant still cannot choose learned routing.

- [ ] **Step 2: Run and verify current env-only behavior fails**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_grant_runtime.py \
  tests/test_adaptive_flock_learned_runtime.py \
  tests/test_adaptive_flock_env_config.py
```

Expected: current replay environment flag can authorize learned routing without a durable grant.

- [ ] **Step 3: Insert the evaluator after static eligibility**

Keep current static assignment and shadow evaluation. Before applying learned target:

```python
authority = self.activation_evaluator.evaluate(
    contract=static_assignment.contract,
    static_decision=static_assignment.decision,
    learned_shadow=shadow,
    policy_id=self.policy_id,
)
if not authority.effective:
    assignment = static_assignment
else:
    assignment = self._apply_authorized_learned_target(...)
```

Record grant/receipt/reason fields in `record_decision`. The environment flag remains only a global permit in `ActivationEvaluator`; set alone produces `durable_grant_required`.

- [ ] **Step 4: Run all Adaptive Flock and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_grant_runtime.py \
  tests/test_adaptive_flock_control_plane.py \
  tests/test_adaptive_flock_env_config.py \
  tests/test_adaptive_flock_learned_runtime.py \
  tests/test_adaptive_flock_preview.py \
  tests/test_adaptive_flock_project_policy.py \
  tests/test_adaptive_flock_run_manager.py \
  tests/test_adaptive_flock_scheduler_hook.py \
  tests/test_agent_routing_cancellation.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass; learned runtime tests now seed exact grants.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/coordinator.py \
  src/nested_memvid_agent/routing/ledger.py \
  src/nested_memvid_agent/routing/ledger_records.py \
  src/nested_memvid_agent/routing/ledger_serialization.py \
  src/nested_memvid_agent/routing/runtime.py \
  src/nested_memvid_agent/routing/run_manager.py \
  tests/test_flock_grant_runtime.py \
  tests/test_adaptive_flock_learned_runtime.py \
  tests/test_adaptive_flock_env_config.py \
  tests/test_agent_routing_cancellation.py
git commit -m "feat: require durable grants for learned routing"
```

### Task 16: Add owner revocation, supersession, and kill switches

**Files:**

- Modify: `src/nested_memvid_agent/routing/activation_service.py`
- Modify: `src/nested_memvid_agent/routing/activation_evaluator.py`
- Create: `tests/test_flock_activation_transitions.py`
- Modify: `src/nested_memvid_agent/runtime_settings.py`
- Modify: `src/nested_memvid_agent/effective_settings.py`
- Modify: `tests/test_effective_settings.py`

**Interfaces:**

- Produce revision-checked `revoke(grant_id)`, scope kill switch, and global learned-authority master setting.
- Revoked cannot return to active; requalification/new activation is required.
- Requalifying same exact scope supersedes the old grant through append-only transitions.

- [ ] **Step 1: Write failing transition tests**

```python
def test_revoked_grant_cannot_be_reactivated(service: ActivationService) -> None:
    grant = active_grant(service)
    service.revoke(grant.grant_id, expected_revision=1)
    with pytest.raises(ValueError, match="fresh qualification"):
        service.activate_existing(grant.grant_id)


def test_environment_change_cannot_undo_revocation() -> None:
    grant = revoked_grant()
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")
    assert evaluator().evaluate(task_for(grant)).effective is False
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_activation_transitions.py \
  tests/test_effective_settings.py
```

Expected: transition/service settings incomplete.

- [ ] **Step 3: Implement append-only transitions and settings projection**

Expose configured/effective global and per-scope authority through Settings/Flock. A global off switch changes effective routing immediately for new leases but does not rewrite grant history. Re-enable restores only grants that remain active/current and whose bindings still verify; revoked/suspended grants stay ineffective.

- [ ] **Step 4: Run transition/settings/full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_activation_transitions.py \
  tests/test_flock_activation_evaluator.py \
  tests/test_effective_settings.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/routing/activation_service.py \
  src/nested_memvid_agent/routing/activation_evaluator.py \
  src/nested_memvid_agent/runtime_settings.py \
  src/nested_memvid_agent/effective_settings.py \
  tests/test_flock_activation_transitions.py \
  tests/test_effective_settings.py
git commit -m "feat: add revocable learned-routing authority"
```

---

## Phase 6: Expose Owner APIs and Durable Events

### Task 17: Add strict qualification and activation routes

**Files:**

- Create: `src/nested_memvid_agent/server_flock_routes.py`
- Create: `tests/test_server_flock_routes.py`
- Modify: `src/nested_memvid_agent/server.py`
- Modify: `tests/test_server_security_headers.py`

**Interfaces:**

- `POST /api/flock/qualifications/preview`
- `POST /api/flock/qualifications`
- `GET /api/flock/qualifications`
- `GET /api/flock/qualifications/{run_id}`
- `POST /api/flock/qualifications/{run_id}/start`
- `POST /api/flock/qualifications/{run_id}/pause`
- `POST /api/flock/qualifications/{run_id}/resume`
- `POST /api/flock/qualifications/{run_id}/cancel`
- `POST /api/flock/qualifications/{run_id}/lower-cap`
- `GET /api/flock/qualifications/{run_id}/receipt`
- `GET /api/flock/qualifications/{run_id}/events`
- `POST /api/flock/activations/preview`
- `POST /api/flock/activations`
- `GET /api/flock/activations`
- `GET /api/flock/activations/{grant_id}/evaluate`
- `POST /api/flock/activations/{grant_id}/revoke`

All mutation schemas forbid extras, reject raw secrets, require expected revisions, and use owner auth.

- [ ] **Step 1: Write failing API contract tests**

```python
def test_preview_defaults_to_editable_fifty_dollar_cap(client: TestClient) -> None:
    response = client.post(
        "/api/flock/qualifications/preview",
        json=qualification_preview_request(),
    )
    assert response.status_code == 200
    assert response.json()["budget"]["maximum_spend_micros"] == 50_000_000
    assert response.json()["budget"]["maximum_spend_usd"] == "50.00"


def test_running_cap_can_lower_but_not_raise(client: TestClient) -> None:
    assert lower_cap(client, "40.00").status_code == 200
    response = lower_cap(client, "50.00")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "qualification_cap_cannot_increase"


def test_agent_principal_cannot_activate(client_as_agent: TestClient) -> None:
    response = client_as_agent.post(
        "/api/flock/activations",
        json=activation_request(),
    )
    assert response.status_code == 403
```

- [ ] **Step 2: Run and verify route failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_flock_routes.py \
  tests/test_server_security_headers.py
```

Expected: routes absent.

- [ ] **Step 3: Implement thin route adapters**

Keep business logic in preview/runner/activation services. SSE supports `Last-Event-ID`, replays persisted bounded events, and uses periodic heartbeat comments only. A renderer disconnect does not change run state.

Return `409` with stable code/current revision for conflicts/drift; `422` for schema; `400` for invalid scope/corpus/cap; `403` for non-owner activation; `404` for unknown IDs. Never include secret refs beyond already public opaque `secret://` metadata, and never raw values.

- [ ] **Step 4: Run route, auth, runner, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_flock_routes.py \
  tests/test_server_security_headers.py \
  tests/test_flock_qualification_runner.py \
  tests/test_flock_activation_service.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/server_flock_routes.py \
  src/nested_memvid_agent/server.py \
  tests/test_server_flock_routes.py \
  tests/test_server_security_headers.py
git commit -m "feat: expose owner-controlled Flock qualification API"
```

---

## Phase 7: Build Qualification and Activation in Flock

### Task 18: Add typed Flock qualification client and reconnectable state

**Files:**

- Create: `web/src/flock/qualification/types.ts`
- Create: `web/src/flock/qualification/api.ts`
- Create: `web/src/flock/qualification/api.test.ts`
- Create: `web/src/flock/qualification/useQualificationRun.ts`
- Create: `web/src/flock/qualification/useQualificationRun.test.ts`
- Create: `web/src/flock/activation/types.ts`
- Create: `web/src/flock/activation/api.ts`
- Create: `web/src/flock/activation/api.test.ts`
- Modify: `web/src/flock/types.ts`

**Interfaces:**

- Produce typed draft/preview/run/event/receipt/scope/grant/evaluation contracts.
- SSE accelerates state; GET remains authority after reconnect.
- Money inputs remain strings until server parsing; never use a JS float as the mutation authority.

- [ ] **Step 1: Write failing request and reconnect tests**

```ts
it("sends the owner-entered cap as decimal text", async () => {
  await previewQualification({ ...draft, maximumSpendUsd: "37.25" });
  expect(lastJsonBody().maximum_spend_usd).toBe("37.25");
});

it("never offers a cap increase for a running run", () => {
  const actions = qualificationActions(runningRun({ effectiveStop: "40.00" }));
  expect(actions).toContain("lower_cap");
  expect(actions).not.toContain("raise_cap");
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- flock/qualification flock/activation
```

Expected: modules absent.

- [ ] **Step 3: Implement typed client/hooks**

Preserve all abstention/suspension reason codes. Never infer qualified from `status === "completed"`; read each scope result. Never infer effective from `grant.status === "active"`; use evaluation.

- [ ] **Step 4: Run tests/build**

Run:

```bash
npm --prefix web test -- flock
npm --prefix web run build
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/flock/qualification web/src/flock/activation web/src/flock/types.ts
git commit -m "feat: add typed Flock qualification client"
```

### Task 19: Build qualification draft, running, and result views

**Files:**

- Create: `web/src/flock/qualification/QualificationWorkspace.tsx`
- Create: `web/src/flock/qualification/QualificationWorkspace.test.tsx`
- Create: `web/src/flock/qualification/QualificationDraft.tsx`
- Create: `web/src/flock/qualification/QualificationDraft.test.tsx`
- Create: `web/src/flock/qualification/TargetMatrix.tsx`
- Create: `web/src/flock/qualification/CorpusReview.tsx`
- Create: `web/src/flock/qualification/QualificationProgress.tsx`
- Create: `web/src/flock/qualification/QualificationResults.tsx`
- Create: `web/src/flock/qualification/ScopeResultCard.tsx`
- Create: `web/src/flock/qualification/qualification.css`
- Modify: `web/src/flock/FlockWorkspace.tsx`

**Interfaces:**

- Draft: project/families/capabilities/policy/corpus/all-target preview/budget/deadlines/concurrency.
- Running: spend/reserve/remaining, cost coverage, case/attempt progress, provider status, pause/cancel, partial evidence.
- Results: qualified/abstained/deterministic-only, support, confidence, utility, cost, replay, guardrails, exact reasons, Evidence drill-down.
- Invariant: no control raises the cap after start.

- [ ] **Step 1: Write failing owner-flow tests**

```ts
it("defaults to $50 and lets the owner change it before launch", async () => {
  render(<QualificationDraft fixture={previewFixture} />);
  const cap = screen.getByLabelText("Maximum provider spend");
  expect(cap).toHaveValue("50.00");
  await user.clear(cap);
  await user.type(cap, "35.00");
  await user.click(screen.getByRole("button", { name: "Refresh preview" }));
  expect(lastJsonBody().maximum_spend_usd).toBe("35.00");
});

it("shows every eligible target and every exclusion reason", () => {
  render(<TargetMatrix preview={allTargetPreview} />);
  expect(screen.getAllByRole("row")).toHaveLength(
    allTargetPreview.targets.length + allTargetPreview.exclusions.length + 1
  );
});

it("does not call a completed run qualified", () => {
  render(<QualificationResults receipt={completedWithAbstentions} />);
  expect(screen.getByText("Evidence collection completed")).toBeVisible();
  expect(screen.getByText("2 scopes abstained")).toBeVisible();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- Qualification
```

Expected: UI absent.

- [ ] **Step 3: Implement the complete qualification flow**

Require an explicit preview review before start. Show immutable max and current lowerable stop cap separately. Unknown usage shows “cost unresolved,” never `$0`. Budget exhaustion shows “new attempts stopped; completed evidence retained.”

Display each threshold individually. Raw attempt/provider receipts are linked under Evidence/Advanced and redacted. High-risk scopes say deterministic-only.

- [ ] **Step 4: Run renderer/API/full gates**

Run:

```bash
npm --prefix web test -- Qualification FlockWorkspace
npm --prefix web run build
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_flock_routes.py \
  tests/test_flock_qualification_runner.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/flock/qualification web/src/flock/FlockWorkspace.tsx
git commit -m "feat: add bounded Flock qualification workspace"
```

### Task 20: Build exact activation, suspension, and revocation views

**Files:**

- Create: `web/src/flock/activation/ActivationsWorkspace.tsx`
- Create: `web/src/flock/activation/ActivationsWorkspace.test.tsx`
- Create: `web/src/flock/activation/ActivationPacket.tsx`
- Create: `web/src/flock/activation/ActivationPacket.test.tsx`
- Create: `web/src/flock/activation/GrantCard.tsx`
- Create: `web/src/flock/activation/GrantCard.test.tsx`
- Create: `web/src/flock/activation/activation.css`
- Modify: `web/src/flock/FlockWorkspace.tsx`
- Modify: `web/src/routing/RoutingCenter.tsx`

**Interfaces:**

- Show qualified/selectable, abstained/reasons, deterministic-only, and stale-invalid scopes.
- Activation packet shows exact authority change and requires explicit owner confirmation.
- Grant card shows active/effective/inactive/suspended/revoked, exact scope, receipt, binding health, reason, route decisions, revoke/requalify.

- [ ] **Step 1: Write failing activation/revocation tests**

```ts
it("activates only explicitly selected qualified scopes", async () => {
  render(<ActivationPacket preview={threeScopesOneAbstained} />);
  await user.click(screen.getByRole("checkbox", { name: /scope a/i }));
  expect(screen.getByRole("checkbox", { name: /scope b abstained/i })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: /I understand/i }));
  await user.click(screen.getByRole("button", { name: "Activate 1 scope" }));
  expect(lastJsonBody().scope_digests).toEqual(["scope_a"]);
});

it("shows active-but-ineffective with the server reason", () => {
  render(<GrantCard grant={activeButDriftedGrant} />);
  expect(screen.getByText("Suspension pending")).toBeVisible();
  expect(screen.getByText("Target inventory changed")).toBeVisible();
});

it("warns that revocation affects new leases", async () => {
  render(<GrantCard grant={effectiveGrant} />);
  await user.click(screen.getByRole("button", { name: "Revoke" }));
  expect(screen.getByText(/new route leases immediately/i)).toBeVisible();
  expect(screen.getByText(/in-flight attempt keeps its existing route lease/i)).toBeVisible();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix web test -- Activations ActivationPacket GrantCard
```

Expected: views absent.

- [ ] **Step 3: Implement owner-only controls**

Do not offer “reactivate.” Suspended/revoked cards offer Requalify. Link ordinary route decisions to grant/receipt evidence. Keep activation and target/provider enablement separate.

- [ ] **Step 4: Run renderer, server, and full suites**

Run:

```bash
npm --prefix web test -- flock routing
npm --prefix web run build
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_flock_routes.py \
  tests/test_flock_activation_service.py \
  tests/test_flock_activation_evaluator.py \
  tests/test_flock_grant_runtime.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/src/flock/activation web/src/flock/FlockWorkspace.tsx \
  web/src/routing/RoutingCenter.tsx
git commit -m "feat: add scoped Flock activation controls"
```

---

## Phase 8: Prove Safety, Determinism, and Live Utility

### Task 21: Add the no-authority-expansion and no-policy-memory test matrix

**Files:**

- Create: `tests/test_flock_authority_boundaries.py`
- Create: `tests/test_flock_no_policy_memory.py`
- Create: `tests/evals/adaptive_flock_qualification/authority_matrix.json`
- Modify: `tests/test_memory_promotion_gates.py`
- Modify: `tests/test_mutation_gate.py`
- Modify: `docs/TEST_MATRIX.md`
- Modify: `docs/SECURITY.md`

**Interfaces:**

- Test every boundary: tools, MCP, skills, plugins, network, workspace, secrets, budget, approvals, privacy, containment, task graph, memory, high risk.
- Assert qualification/activation create no direct memory writes and no policy promotion signal.

- [ ] **Step 1: Write the failing matrix test**

```python
@pytest.mark.parametrize("case", load_authority_matrix())
def test_learned_selection_never_expands_task_authority(case: AuthorityCase) -> None:
    before = effective_authority(case.task)
    result = route_with_active_grant(case)
    after = effective_authority(result.assignment)
    assert after <= before
    assert result.assignment.contract.digest == case.task_contract_digest


def test_qualification_and_activation_write_no_memvid_records(memory_spy: MemorySpy) -> None:
    run_completed_qualification_and_activation()
    assert memory_spy.writes == []
    assert memory_spy.policy_signals == []
```

- [ ] **Step 2: Run and verify failures/gaps**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_authority_boundaries.py \
  tests/test_flock_no_policy_memory.py \
  tests/test_memory_promotion_gates.py \
  tests/test_mutation_gate.py
```

Expected: any unguarded boundary or missing spy hook fails.

- [ ] **Step 3: Fix at authoritative layers**

Do not filter merely in the GUI. Tighten contract compilation, eligibility, activation evaluator, executor, mutation gate, or memory promotion boundary as indicated.

- [ ] **Step 4: Run all security/static/full gates**

Run:

```bash
uv run python -m compileall -q src tests
uv run ruff check src tests
uv run mypy src
uv run bandit -q -r src -lll -iii
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_flock_authority_boundaries.py \
  tests/test_flock_no_policy_memory.py \
  tests/evals/adaptive_flock_qualification \
  tests/test_memory_promotion_gates.py \
  tests/test_mutation_gate.py \
  docs/TEST_MATRIX.md docs/SECURITY.md
git commit -m "test: prove Flock cannot expand authority"
```

### Task 22: Add deterministic qualification and installed GUI journeys

**Files:**

- Create: `tests/test_flock_qualification_determinism.py`
- Create: `scripts/run_flock_qualification_determinism.py`
- Create: `desktop/e2e/flock-qualification.spec.ts`
- Modify: `.github/workflows/determinism.yml`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**

- Deterministic report schema: `kestrel.flock_qualification_determinism.v1`.
- Required: 20 complete repeats, one receipt projection digest, zero flake, zero guardrail violations.
- Desktop E2E: draft $50 -> edit -> preview -> start with mock -> pause/resume -> complete -> activation preview -> activate -> route -> revoke -> static fallback.

- [ ] **Step 1: Write failing 20-repeat gate**

```python
def test_determinism_runner_requires_twenty_identical_receipts(tmp_path: Path) -> None:
    report = run_qualification_determinism(tmp_path, repeats=20)
    assert report["completed_repeats"] == 20
    assert report["unique_receipt_projection_digests"] == 1
    assert report["guardrail_violations"] == 0
    assert report["passed"] is True
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_determinism.py
npm --prefix desktop run e2e -- flock-qualification
```

Expected: runner/E2E absent.

- [ ] **Step 3: Implement deterministic runner and installed fixture**

Freeze clock, IDs, evidence order, target inventory, prices, build ID, and provider outputs. The report must bind source commit and configuration digest. Desktop E2E uses only deterministic mock targets and cannot be labeled production provider qualification.

- [ ] **Step 4: Run deterministic and GUI gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_qualification_determinism.py
uv run python scripts/run_flock_qualification_determinism.py \
  --repeats 20 \
  --output .tmp/flock-qualification-determinism.json
npm --prefix web test
npm --prefix web run build
npm --prefix desktop test
npm --prefix desktop run e2e -- flock-qualification
```

Expected: 20/20 one digest and E2E passes.

- [ ] **Step 5: Commit**

```bash
git add tests/test_flock_qualification_determinism.py \
  scripts/run_flock_qualification_determinism.py \
  desktop/e2e/flock-qualification.spec.ts \
  .github/workflows/determinism.yml \
  .github/workflows/ci.yml
git commit -m "test: gate deterministic Flock qualification"
```

### Task 23: Add live-provider qualification runner and release evidence contract

**Files:**

- Create: `scripts/run_flock_live_qualification.py`
- Create: `tests/test_flock_live_qualification_runner.py`
- Create: `tests/integration/test_flock_live_qualification.py`
- Create: `docs/FLOCK_QUALIFICATION_OPERATIONS.md`
- Modify: `docs/RELEASE_CHECKLIST.md`
- Modify: `docs/PRODUCTION_OPERATIONS.md`

**Interfaces:**

- Consume: owner-created qualification draft/receipt and exact installed sidecar endpoint; secrets only by Secret Broker refs.
- Produce: redacted release evidence report bound to source commit, installed artifact digest, platform/architecture, provider profile/model subject digests, project/tree digest, receipt digest, grants, costs, replay, and guardrails.
- Live gate requires at least two real eligible targets and owner-selected real project corpus.

- [ ] **Step 1: Write failing evidence-verifier tests**

```python
def test_live_report_rejects_mock_only_or_one_target_evidence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="two real eligible targets"):
        verify_live_report(one_target_report())
    with pytest.raises(ValueError, match="mock evidence cannot certify"):
        verify_live_report(mock_only_report())


def test_live_report_is_bound_to_installed_artifact() -> None:
    report = valid_live_report()
    report["installed_artifact_digest"] = "b" * 64
    with pytest.raises(ValueError, match="artifact digest"):
        verify_live_report(report, expected_artifact_digest="a" * 64)
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_live_qualification_runner.py
```

Expected: runner/verifier absent.

- [ ] **Step 3: Implement explicit, non-default live runner**

Require command arguments for run ID, expected receipt ID/digest, installed artifact digest, output path, and explicit confirmation. Read no raw credential CLI argument. Fetch the immutable receipt through authenticated local API, verify it, verify two real targets, real task evidence, cost coverage, 20/20 replay, zero guardrails, and exact grants.

Never auto-activate from the runner. Activation remains a separate owner GUI/API action.

- [ ] **Step 4: Run deterministic verifier and gated live integration**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_flock_live_qualification_runner.py
RUN_FLOCK_LIVE_QUALIFICATION=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  uv run pytest -q tests/integration/test_flock_live_qualification.py
```

Expected: deterministic verifier passes; live integration runs only with explicit configured providers/project/containment and produces a redacted report.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_flock_live_qualification.py \
  tests/test_flock_live_qualification_runner.py \
  tests/integration/test_flock_live_qualification.py \
  docs/FLOCK_QUALIFICATION_OPERATIONS.md \
  docs/RELEASE_CHECKLIST.md \
  docs/PRODUCTION_OPERATIONS.md
git commit -m "test: define live Flock qualification evidence"
```

---

## Final Verification

- [ ] Run exact final source gates:

```bash
uv lock --check
uv run python -m compileall -q src tests scripts
uv run ruff check scripts src tests
uv run mypy src
uv run bandit -q -r src -lll -iii
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
npm --prefix web run licenses:check
npm --prefix web run test:typecheck
npm --prefix web test
npm --prefix web run build
npm --prefix desktop run test:typecheck
npm --prefix desktop test
npm --prefix desktop run e2e -- flock-qualification
```

- [ ] Run 20-repeat deterministic qualification at final `HEAD`:

```bash
uv run python scripts/run_flock_qualification_determinism.py \
  --repeats 20 \
  --output .tmp/flock-qualification-determinism.json
```

Verify report: 20 completed, one projection digest, no flake, no guardrail violation, exact source commit.

- [ ] Run gated Memvid regression even though Flock writes no memory:

```bash
RUN_MEMVID_INTEGRATION=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/integration/test_memvid_backend_integration.py \
  tests/integration/test_memvid_memory_system.py \
  tests/integration/test_memvid_context_frames.py
```

- [ ] Inspect schema and authority hooks:

```bash
rg -n "ROUTING_SCHEMA_VERSION = 4" src/nested_memvid_agent/routing/ledger_schema.py
rg -n "durable_grant_required|activation_grant_id|activation_reason" \
  src/nested_memvid_agent/routing
rg -n "MemoryLayer\\.POLICY|allow_policy_writes|memory\\.add|memory\\.learn" \
  src/nested_memvid_agent/routing src/nested_memvid_agent/server_flock_routes.py
git diff --check
git status --short
```

Expected: no qualification/activation memory write path and no policy-write reference beyond explicit negative guards/tests.

- [ ] Exercise the exact owner journey in the built Desktop app:

  - Flock draft defaults to `$50.00`;
  - owner changes cap before preview/start;
  - preview includes every currently eligible target and every exclusion;
  - hybrid corpus distinguishes fixtures, actionable real tasks, and diagnostics;
  - running view never offers cap increase;
  - missing usage shows unresolved cost and retains reserve;
  - pause/resume/cancel survive renderer reconnect;
  - completed does not imply every scope qualified;
  - results expose each threshold/reason and raw evidence links;
  - no authority exists before explicit activation;
  - activation packet binds exact scope/receipt/inventory/prices/policy/config/project authority;
  - high risk cannot activate;
  - learned route decision links to grant/receipt;
  - drift suspends and returns new decisions to static;
  - revocation affects new leases and cannot be undone by environment flags;
  - existing route lease stays sticky;
  - no tool/permission/workspace/network/secret/budget/approval/memory boundary expands.

- [ ] Run a separate explicitly authorized installed-artifact live qualification with at least two real targets. Preserve the receipt and release evidence; do not include raw secrets or source content in the report.

- [ ] Record final commit SHA, routing v3-to-v4 migration receipt, deterministic report digest, GUI E2E receipt, and live installed-artifact evidence in the program index.

## Completion Criteria

- The owner can preview and launch a bounded hybrid-corpus qualification from Flock.
- Every currently configured eligible target is snapshotted and evaluated or the scope explicitly abstains.
- The pre-run cap defaults to USD 50, can be changed before start, cannot rise during execution, and is conservatively enforced.
- Missing pricing blocks billed admission; missing usage remains unresolved cost, never zero.
- Every terminal run has immutable authenticated evidence and explicit reasons.
- Twenty replay projections are identical.
- Only exact low/medium-risk scopes can qualify.
- Qualification alone grants no authority.
- Explicit owner activation creates one exact append-only grant per scope.
- Every new learned route requires a current effective grant in addition to the global permit.
- Drift, decay, stale targets, policy/config changes, replay failure, or kill switches produce durable suspension and static fallback.
- Revocation is immediate for new leases and cannot silently reroute existing attempts.
- High-risk routing remains deterministic.
- Qualification/activation write no policy memory and expand no authority boundary.
- Mock evidence proves behavior; only separately gated real-provider evidence on exact installed bytes supports a production activation claim.
