# Adaptive Flock Production Qualification and Scoped Activation Design

Date: 2026-07-29  
Status: Owner approved for implementation planning
Target profile: one trusted owner, one local or privately networked Kestrel node  
Depends on: existing Adaptive Flock routing/ledger implementation and the
GUI-first Flock workspace

## 1. Purpose

Kestrel already implements:

- durable static, learned-shadow, and actual route comparisons;
- project/task-family/risk/capability-scoped calibration;
- provider-attempt usage and cost attribution;
- transport, capability, and contract escalation semantics;
- evidence-gated low/medium-risk learned routing;
- deterministic high-risk routing.

The remaining gap is not a second learned router. It is a production
qualification and owner-activation system that converts measured, replayable
evidence into narrow routing authority without relying on environment booleans
or informal operator judgment.

This design adds:

- bounded hybrid-corpus qualification runs;
- immutable qualification receipts;
- configurable hard spend caps;
- exact per-project/task-family activation grants;
- continuous effective-gate evaluation;
- automatic suspension and explicit revocation;
- first-class GUI controls and evidence.

## 2. Goals

- Qualify every currently configured eligible target that can participate
  safely.
- Use deterministic fixtures and owner-selected real project tasks.
- Default the total live-provider spend cap to USD 50 while allowing the owner
  to change it before launch.
- Keep the approved maximum immutable during execution; the owner may lower the
  effective stop cap, pause, or cancel.
- Require attributable usage and snapshotted prices; unknown cost is never
  treated as free.
- Activate only exact qualified project/task-family scopes.
- Require explicit owner activation after qualification.
- Keep high-risk routing deterministic.
- Fall back to static routing whenever learned authority is absent,
  insufficient, stale, suspended, or revoked.
- Preserve every attempt, abstention, failure category, decision, outcome, and
  activation transition for review.

## 3. Non-goals

This phase does not:

- make learned routing globally active;
- let routing expand tools, permissions, workspace, network, secrets, budget,
  or approval authority;
- allow high-risk learned activation;
- infer success from model self-report;
- activate a hard-filtered target;
- make missing pricing or usage equal zero;
- auto-enable a discovered local/LAN/cloud target;
- write policy memory from routing outcomes;
- claim that mocks certify production provider behavior;
- remove deterministic fallback or owner revocation.

## 4. Design principles

### 4.1 Evidence before authority

Qualification records what happened. Activation is a separate explicit owner
decision. Passing evidence does not activate itself.

### 4.2 Exact scope

A grant applies only to the exact combination of:

- project;
- task family;
- risk band;
- required capability set;
- routing policy;
- eligible target inventory;
- pricing snapshot;
- learned-router configuration;
- qualification receipt.

### 4.3 Abstention is correct behavior

Sparse evidence, incomplete cost coverage, low utility, replay drift, stale
providers, or hard-filter conflicts produce an abstention. They are not
qualification-system failures.

### 4.4 The deterministic runtime remains authoritative

The learned residual may choose among already eligible targets. It cannot
change the task contract or make a filtered target eligible.

## 5. Qualification workflow

### 5.1 Draft scope

The owner starts from the Flock / Qualification workspace and selects:

- one project;
- one or more supported low/medium-risk task families;
- the required capability sets represented in the corpus;
- a routing policy;
- the hybrid evidence corpus;
- the total spend cap;
- provider/task deadlines and concurrency within safe configured maxima.

High-risk families may be included for shadow comparison, but the UI marks them
ineligible for learned activation.

The server produces a read-only preview before launch.

### 5.2 Snapshot eligible targets

Qualification includes all currently configured targets that pass the normal
hard eligibility filters for at least one selected scope.

The immutable target snapshot records:

- target and provider IDs;
- model identifier;
- endpoint trust class;
- enabled/freshness state;
- capability evidence;
- privacy and network constraints;
- quality tier;
- configured limits;
- input/output token prices and currency;
- price source and timestamp;
- source/provider/model configuration digests.

At least two eligible targets must be available for a comparative scope.

A target without a trustworthy price may gather quality evidence, but its
attempts lack attributable cost. The scope cannot activate until aggregate
cost coverage reaches the configured threshold.

### 5.3 Build the hybrid corpus

The corpus contains two evidence classes.

#### Deterministic fixtures

- checked-in, immutable task inputs;
- fixed task contracts;
- deterministic mock or replayable provider fixtures where appropriate;
- trusted acceptance validators;
- stable expected outcome categories;
- no production credential requirement.

Fixtures prove schema, replay, guardrails, abstention, accounting, and runtime
behavior. They do not alone prove live-provider utility.

#### Owner-selected real tasks

- drawn from the selected project;
- bound to a repository/tree or evidence digest;
- assigned an immutable task family, risk, capability set, and acceptance plan;
- validated by trusted runtime/test/review evidence;
- stripped of credentials and bounded to the project path ceiling;
- explicitly approved for provider exposure under the target's privacy class.

Real tasks must be repeatable or replay-comparable. A task without a trusted
acceptance result can appear in diagnostics but cannot become an actionable
route example.

Tasks that execute candidate code require the configured qualified containment
path. Kestrel does not fall back to host execution merely to complete a
qualification.

### 5.4 Budget and admission

The product default is USD 50. The owner can change the default in Settings and
override it on each qualification draft.

The run snapshots:

- immutable maximum spend cap;
- initial effective stop cap;
- per-attempt and per-case admission ceilings;
- price snapshot;
- target inventory;
- maximum token/time/concurrency limits;
- admission-estimation method.

Before each provider attempt Kestrel checks:

- actual spend already recorded;
- cost of admitted in-flight attempts;
- conservative projected attempt cost;
- remaining cap.

If the next attempt could exceed the hard cap, it is not admitted.

The maximum cap cannot be raised after execution begins. The owner may:

- lower the effective stop cap, causing new admissions to stop at the lower
  value;
- pause after current bounded attempts finish;
- cancel new work while preserving all completed evidence.

Unreported usage or price produces unknown actual cost, not zero. The attempt
remains visible but reduces cost coverage and may prevent qualification.

Every lowered stop cap is revisioned and included in the terminal receipt. It
does not alter the immutable maximum that the owner approved at launch.

### 5.5 Execute in shadow

Qualification never begins in learned-authority mode.

For each case and target assignment Kestrel records:

- static decision;
- learned shadow decision;
- actual target;
- route lease;
- provider attempts and fallback;
- normalized token usage;
- attributable cost;
- latency;
- trusted acceptance result;
- failure category;
- guardrail/filter decisions;
- replay configuration digest.

Provider transport outages are measured separately and do not become
task-quality punishment.

### 5.6 Replay

The exact ordered evidence set is replayed through the learned-router compiler.
Release qualification requires twenty identical decision projections from the
same evidence and configuration.

Replay compares:

- eligible set;
- learned target or abstention;
- confidence;
- utility components and delta;
- cost coverage;
- activation/abstention reason;
- configuration and evidence digests.

Any drift blocks the scope.

## 6. Qualification thresholds

The initial production defaults preserve the existing learned-router contract:

- minimum examples per scope: `5`;
- minimum examples for the selected learned target: `3`;
- minimum confidence: `0.70`;
- minimum learned utility margin: `0.08`;
- minimum attributable cost coverage: `0.80`;
- decay half-life: `30` days;
- hard guardrail violations: exactly `0`;
- replay: `20/20` identical projections.

The effective thresholds are snapshot into every qualification receipt.

Changing a threshold creates a new qualification configuration digest. It does
not reinterpret an existing receipt or silently widen an active grant.

The Flock UI shows each metric and why a scope qualified or abstained. A single
opaque "AI score" is not sufficient evidence.

## 7. Durable control-plane records

All records live in the SQLite control plane, not Memvid canonical memory.
Schema changes are additive and migrate existing routing ledgers without
rewriting route outcomes.

### 7.1 Qualification run

Stores:

- ID, project, owner, status, timestamps, and revision;
- selected scope keys;
- corpus manifest and digest;
- target inventory and digest;
- price snapshot and digest;
- policy and learned-router configuration;
- configured budget and admission rules;
- source build/version;
- aggregate known/unknown cost;
- pause/cancel/failure state;
- terminal receipt ID.

States are:

- `draft`;
- `ready`;
- `running`;
- `pausing`;
- `paused`;
- `cancelled`;
- `failed`;
- `completed`.

`completed` means evidence collection and receipt finalization completed. It
does not mean every scope qualified.

Every terminal state writes an immutable receipt describing the evidence that
exists and why the run stopped. Only a `completed` run can contain qualified
scopes; failed and cancelled receipts are non-qualifying audit evidence.

### 7.2 Qualification case and attempt

Cases bind:

- corpus item;
- immutable task contract;
- acceptance plan;
- scope key;
- repository/evidence digest;
- privacy eligibility.

Attempts bind:

- case;
- target;
- route decision and lease;
- provider receipts;
- validation/review evidence;
- cost/usage;
- failure category;
- outcome;
- secret-safe raw-evidence references.

### 7.3 Qualification receipt

The immutable receipt contains:

- all authority and evidence digests;
- total known cost and coverage;
- replay results;
- guardrail results;
- per-scope metrics;
- qualified/abstained result;
- explicit reasons;
- source build and schema versions;
- creation timestamp and authenticated receipt digest.

Summary rows link to raw attempts and trusted acceptance evidence. A summary
cannot substitute for missing raw evidence.

### 7.4 Activation grant

An activation grant stores:

- receipt ID and digest;
- exact selected scope key;
- policy, target inventory, price, threshold, and relevant project-authority
  digests;
- owner confirmation metadata;
- created revision and timestamp;
- status and status reason;
- superseding/revocation relationship.

Grant states are:

- `active`;
- `suspended`;
- `revoked`;
- `superseded`.

Transitions are append-only and revision-checked.

## 8. Owner activation

After a completed run, the GUI lists each scope as:

- qualified and selectable;
- abstained with reasons;
- high-risk/deterministic-only;
- invalid because evidence is stale or authority drifted.

The owner selects one or more qualified scopes. The confirmation packet shows:

- project and task family;
- risk/capability scope;
- learned target and alternatives;
- confidence, support, utility, cost coverage, and replay;
- target and price snapshot;
- authority that will change;
- automatic suspension conditions;
- one-click revocation behavior.

The server revalidates the receipt and every binding under a transaction before
creating grants. The GUI sends an owner decision; an LLM cannot self-confirm
the packet.

The owner can activate several individually qualified scopes in one UI action,
but the database stores one exact grant per scope so each can suspend or revoke
independently.

## 9. Effective grant evaluation

Every new route decision checks the effective grant at decision time.

The grant is effective only when:

- it is active and current;
- global Adaptive Flock and learned-authority switches permit it;
- project, task family, risk, and capabilities match exactly;
- the task is low or medium risk;
- the current target remains hard eligible;
- relevant project authority and privacy policy match;
- routing policy and learned configuration match;
- target inventory and price snapshot match;
- decayed evidence still meets support/confidence/utility/cost thresholds;
- the qualification receipt and replay evidence verify;
- no global or scope kill switch is active.

An ineffective grant produces a static route plus a durable reason. It does not
fail the mission merely because learning abstained.

The existing environment flag
`NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED` is not sufficient to create
authority. It may remain as a global master permit during migration, but an
effective durable owner grant is additionally required.

## 10. Suspension and revocation

### 10.1 Automatic suspension

New learned decisions return to static routing when:

- receipt authentication or raw evidence verification fails;
- evidence decay drops below a threshold;
- relevant project privacy/capability authority changes;
- the target inventory, model, endpoint, trust, or price changes;
- learned-router or routing-policy configuration changes;
- a target becomes hard ineligible;
- deterministic replay no longer verifies;
- an operator or global kill switch blocks learned authority.

Suspension is durable and records an exact reason. Requalification and a new
owner decision are required when the binding changed materially.

An ephemeral provider transport outage does not rewrite historical task
quality. The router applies its normal eligible fallback/escalation semantics
and records the outage. Sustained inventory or configuration change can suspend
the affected grant.

### 10.2 Owner revocation

The owner can revoke a grant from Flock / Activations at any time.

Revocation:

- takes effect for new route leases immediately;
- appends an authenticated transition;
- cannot be undone by changing an environment variable;
- requires a fresh qualification/activation to regain authority.

Attempts with an existing route lease remain sticky through their tool loop.
The global emergency kill switch may stop/cancel work under its separate
contract; ordinary grant revocation does not silently swap an in-flight model.

## 11. Runtime decision flow

For each task attempt:

1. compile the immutable task contract;
2. apply capability, privacy, trust, budget, and reviewer-diversity filters;
3. calculate the exact scope key;
4. load and verify the effective activation grant;
5. evaluate the learned residual against current calibrated evidence;
6. choose an eligible learned target or abstain to static routing;
7. persist static, learned, and actual decisions before provider execution;
8. retain the route lease through the attempt;
9. record provider attempts, usage, cost, validation, outcome, and regret;
10. update decayed calibration without granting new authority.

Qualification and activation never let a model mutate the task contract or
graph.

## 12. GUI experience

The top-level **Flock** destination contains:

- Overview;
- Providers;
- Qualification;
- Activations;
- History.

### 12.1 Qualification draft

The GUI provides:

- project and task-family selector;
- hybrid corpus review;
- all-eligible-target preview;
- price/capability/freshness warnings;
- USD 50 default budget input with owner override;
- estimated range and hard-cap explanation;
- immutable preview receipt before launch.

### 12.2 Running state

The owner sees:

- spend and remaining cap;
- known/unknown cost coverage;
- case/attempt progress;
- provider availability;
- pause and cancel;
- partial evidence;
- zero-cost ambiguity warnings;
- no control that raises the cap mid-run.

### 12.3 Results

Per scope the UI shows:

- qualified, abstained, or deterministic-only;
- confidence;
- total/per-target support;
- utility lift and components;
- cost coverage;
- estimated savings/regret;
- replay result;
- guardrail result;
- exact abstention reason;
- evidence drill-down.

### 12.4 Activations

The UI shows:

- effective/inactive/suspended/revoked state;
- exact scope;
- receipt and current binding health;
- why learned authority is or is not effective;
- activate, revoke, and requalify actions;
- route decisions linked back to the grant and receipt.

Raw JSON remains under Evidence / Advanced.

## 13. API and service boundaries

The exact route names may follow current server module conventions, but the
service contract must expose:

- preview qualification;
- create/start qualification;
- get/list qualification runs;
- pause/resume/cancel;
- get immutable receipt;
- preview activation;
- create scoped grants with expected revision;
- list/evaluate grants;
- revoke a grant with expected revision;
- stream bounded qualification events.

All control-plane mutations require owner API authentication. Direct owner GUI
actions use revision-checked mutation contracts. Agent-invoked variants, if
added later, must use the existing capability and exact-call approval paths.

## 14. Failure handling

- Provider timeout preserves the attempt and uses the typed transport category.
- Capability failure does not punish target quality and requires stronger
  eligible escalation.
- Contract failure returns to replanning and cannot be hidden as provider
  fallback.
- Missing usage/price reduces cost coverage.
- Budget exhaustion stops new admissions and finalizes available evidence.
- Cancellation preserves completed attempts and produces a non-qualifying
  cancelled receipt/status.
- State or receipt verification failure blocks activation.
- UI disconnect does not stop the durable run; reconnect resumes event
  projection.
- Sidecar restart recovers the run state but never repeats an ambiguous provider
  request automatically.
- Cross-project evidence cannot enter a scope through child-frame expansion,
  direct lookup, or corpus import.

## 15. Security and memory boundaries

- Provider secrets remain Secret Broker references.
- Corpus and receipts reject registered secret values before persistence.
- Provider errors and responses are bounded and redacted.
- Qualification data stays in SQLite and evidence artifacts; it is not
  canonical Memvid memory.
- Meaningful routing lessons may enter normal memory only through existing
  evidence, provenance, confidence, validation, and promotion gates.
- No policy memory write is produced by qualification or activation.
- LAN targets retain their LAN trust/privacy label throughout qualification and
  routing.
- A target cannot gain tools, network, workspace, secrets, or approvals through
  learned selection.

## 16. Verification

### 16.1 Deterministic unit and integration tests

- schema migration from current routing databases;
- qualification lifecycle and revision races;
- target and price snapshot authentication;
- hybrid corpus digest and project isolation;
- budget admission, lowering, pause, cancellation, and exact exhaustion;
- missing usage/price and cost-coverage behavior;
- minimum support, confidence, margin, decay, and hard abstention;
- twenty identical replays;
- zero-guardrail requirement;
- high-risk deterministic routing;
- activation transaction and multi-scope creation;
- effective-grant evaluation;
- automatic suspension for every binding class;
- owner revocation and supersession;
- sticky existing leases;
- static fallback reasons;
- transport/capability/contract escalation separation;
- provider outage exclusion from task-quality punishment;
- no policy-memory write.

### 16.2 GUI tests

- draft preview and editable USD 50 default;
- no cap raise while running;
- all eligible targets with price/freshness warnings;
- progress, pause, cancel, and reconnect;
- qualified/abstained/deterministic-only states;
- exact activation packet;
- per-scope selection;
- suspension and revocation;
- Evidence / Advanced disclosure;
- accessible labels, keyboard navigation, contrast, and reduced motion.

### 16.3 Live and release evidence

Production activation requires:

- at least two real eligible targets in the scope;
- owner-approved real project corpus;
- attributable usage and measured price coverage;
- trusted acceptance evidence;
- exact live-provider subject/model/profile receipts;
- twenty-repeat replay;
- no guardrail violation;
- installed-artifact execution on the supported platform.

Mock tests and synthetic fixtures verify behavior but cannot certify real
provider utility.

## 17. Definition of done

This design is complete when:

1. the owner can launch a bounded hybrid-corpus qualification from the GUI;
2. all currently configured eligible targets are snapshot and evaluated;
3. the editable pre-run cap defaults to USD 50 and cannot rise mid-run;
4. missing usage or pricing cannot become free cost;
5. every result has raw evidence, immutable digests, and explicit reasons;
6. only low/medium-risk exact scopes can qualify;
7. twenty-repeat replay and all configured thresholds pass;
8. qualification alone creates no authority;
9. owner activation creates revisioned exact grants;
10. drift, stale evidence, or guardrail failure returns new decisions to static
    routing;
11. revocation is immediate for new leases and cannot silently reroute existing
    attempts;
12. high-risk routing remains deterministic;
13. no memory, capability, secret, workspace, network, or approval boundary is
    expanded;
14. deterministic, GUI, live-provider, and installed-artifact gates pass on the
    exact final bytes.
