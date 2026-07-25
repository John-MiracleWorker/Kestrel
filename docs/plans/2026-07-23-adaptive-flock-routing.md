# Kestrel Adaptive Flock: Heterogeneous Agent Routing Plan

**Date:** 2026-07-23  
**Status:** Implementation-ready architecture and delivery plan  
**Working name:** Adaptive Flock  
**Repository:** `John-MiracleWorker/Kestrel`  
**Frozen baseline:** `main` at `65573be7dbb6f772860a5a0a98ab4de4e3292920`  
**Scope:** Single-user, single-node, local-first Kestrel runtime. Hosted multi-tenant routing, billing, and distributed cluster scheduling are explicitly out of scope for the first implementation.

This document authorizes no provider purchase, credential migration, production rollout, commit to `main`, or weakening of Kestrel's existing safety gates. It defines how to implement, validate, and stage the feature.

---

## 1. Executive decision

Kestrel should not become another generic OpenAI-compatible proxy or a clone of a universal model router.

Kestrel should become an **adaptive engineering runtime that assembles a temporary agent team for each objective and assigns each task to the cheapest eligible model or native worker predicted to complete it successfully**.

The core loop is:

```text
user objective
    -> durable task graph
    -> explicit task contract
    -> candidate capability filter
    -> route decision and attempt lease
    -> isolated subagent execution
    -> evidence-backed acceptance validation
    -> accept, retry, escalate, or re-plan
    -> durable routing outcome
    -> guarded learning from repeated verified outcomes
```

The powerful model is used where judgment is valuable: decomposition, ambiguity resolution, architecture, recovery, and final review. Smaller or local models are used where the work has been narrowed into a bounded contract: repository search, repetitive edits, test execution, mechanical refactors, documentation updates, and other low-ambiguity tasks.

The implementation must be **task-aware**, not merely role-to-model mapping. `frontend` or `worker` is not enough information to route safely. A UI redesign needing vision and broad product judgment is different from changing a spacing token in three files. The task contract, required capabilities, permissions, risk, context size, budget, and prior verified outcomes must all participate.

### Recommended product positioning

> Kestrel is a local-first adaptive engineering runtime that assembles, routes, governs, verifies, and teaches heterogeneous agent teams.

### Recommended implementation boundary

Adaptive Flock is a native Kestrel subsystem. It should use the existing provider adapters, Secret Broker, task DAG, subagent scheduler, worktree isolation, exact-call approvals, reviewer artifacts, task capsules, tracing, and nested-learning gates.

It should not introduce a parallel scheduler, a second credential vault, a second task database, or a second memory system.

---

## 2. Current repository assessment

The repository already contains most of the difficult execution and safety substrate. The feature is viable because the required seams exist today.

### 2.1 Existing strengths to preserve

#### Durable orchestration

`RunManager` already owns:

- durable run admission and recovery;
- root and child task records;
- ready-task filtering;
- subagent records and worker claims;
- bounded scheduler cycles;
- approval pause and resume;
- worker heartbeat and execution fencing;
- task/subagent terminal transitions;
- worktree isolation;
- acceptance validation;
- tracing and run events;
- task capsule completion.

The durable graph wrapper already defines planner, executor, reviewer, recovery, memory-promotion, and finalizer nodes in `graph_runtime.py`.

#### Useful task metadata already exists

`TaskNodeRecord` already stores:

- title and goal;
- planner/worker/reviewer profile;
- dependencies;
- required tools;
- risk;
- acceptance criteria;
- attempt count;
- failure reason;
- diagnosis;
- retry strategy;
- plan and result payloads.

This is enough to seed a first task-contract compiler without replacing the task model.

#### Existing provider layer

`llm/factory.py` already builds:

- deterministic mock;
- OpenAI Responses;
- OpenAI-compatible endpoints;
- LM Studio;
- local Ollama;
- Ollama Cloud;
- OpenRouter;
- DeepSeek;
- Kimi;
- Anthropic;
- Grok;
- Gemini;
- Codex CLI.

It also already wraps providers with operational health/circuit-breaker behavior and supports one explicit retryable fallback.

Adaptive Flock should select among configured provider/model targets and compile the selection into a per-worker `AgentConfig`. It should not reimplement every provider protocol.

#### Existing evidence gates

Kestrel's acceptance validation deliberately refuses to treat an unrelated successful tool call as proof that tests or validation passed. That is exactly the right foundation for outcome-based routing.

Model self-confidence must not be the primary reward signal. Test receipts, lint receipts, file/diff constraints, reviewer artifacts, structured output validation, tool outcomes, and explicit user correction are stronger signals.

#### Existing learning architecture

`learned_routing.py` already demonstrates the repository's preferred learning posture:

- shadow mode first;
- constrained activation;
- minimum evidence support;
- utility margin;
- confidence threshold;
- guardrail-admissible choices only;
- abstention when evidence is weak;
- serializable model state;
- deterministic replay evaluation.

Adaptive Flock should reuse this philosophy and supporting patterns, but it should have a separate routing model. Memory-layer routing and agent/model assignment are different domains and should not share one class or one target space.

#### Existing secret boundary

The Secret Broker exposes metadata publicly and resolves raw values only at runtime. Provider profile metadata can therefore live in SQLite while API keys remain in the existing vault/keyring and are referenced by `secret://...` handles.

#### Existing isolation and approval boundaries

Subagent work can already run in a Git worktree. High-risk tools remain capability-gated and exact-call approved. Routing must never grant tools, expand filesystem scope, bypass approval, or weaken the current OCI-only validation contract.

### 2.2 Exact insertion point

The current subagent execution path in `RunManager._run_subagent()` is effectively:

```python
config, worker_isolation = self._worker_config(...)
agent = self._build_agent(config)
result = agent.chat(...)
validation = _validate_task_completion(task, result, ...)
```

The routing seam belongs immediately before agent construction:

```python
contract = self.routing.compile_task_contract(...)
assignment = self.routing.select_assignment(...)
routed_config = self.routing.apply_assignment(config, assignment)
config, worker_isolation = self._worker_config(routed_config, ...)
agent = self._build_agent(config)
```

The final ordering may isolate before or after applying the model assignment, but the following must be true:

1. the task contract is compiled before an external model call;
2. the route decision is durably persisted before the assigned provider is invoked;
3. the route is pinned for one subagent attempt;
4. workspace/tool permissions come from the task and run policy, not the selected provider;
5. the result is validated before a route outcome is marked successful.

### 2.3 Architectural debt to avoid increasing

`run_manager.py` is already the central lifecycle coordinator and is very large. Routing algorithms, candidate catalogs, scoring, outcome aggregation, and learning must live in dedicated modules. `RunManager` should call a cohesive service and remain authoritative for lifecycle transitions.

Likewise, `web/src/App.tsx` already centralizes substantial workbench state and hard-coded provider suggestions. The routing UI should be implemented as dedicated components and API modules rather than adding another large inline panel.

---

## 3. Goals and non-goals

### 3.1 Goals

1. Assign different provider/model targets to different graph nodes and subagents in the same run.
2. Use a strong orchestrator for ambiguous planning while allowing bounded work to run on cheaper or local models.
3. Select targets from explicit task requirements, not vendor names hard-coded into task roles.
4. Preserve one assigned target for the duration of a task attempt.
5. Separate transient provider fallback from evidence-triggered capability escalation.
6. Validate every completed task through existing acceptance criteria and trusted evidence.
7. Record an inspectable explanation of every routing decision.
8. Learn from verified outcomes in shadow mode before making learned decisions authoritative.
9. Keep credentials inside the existing Secret Broker and provider/native-agent trust boundaries.
10. Preserve deterministic mock tests and all existing safety invariants.
11. Support direct operator overrides without pretending the router made the decision.
12. Make cost, latency, local/cloud use, retry, escalation, and success visible in the workbench.

### 3.2 Non-goals for the first release

1. A public universal LLM proxy for unrelated clients.
2. Hundreds of provider integrations.
3. Automatic extraction or reuse of unsupported subscription OAuth tokens.
4. Multi-tenant billing or per-organization policy.
5. Distributed scheduling across multiple Kestrel hosts.
6. Fully dynamic LLM-authored DAG mutation.
7. Automatic merging of multiple competing worker branches.
8. Unrestricted autonomous self-modification.
9. A black-box router that cannot explain its decision.
10. Reinforcement learning directly against live repositories without a shadow and replay phase.

Dynamic DAG revision, native-agent fan-out, candidate branch comparison, and distributed workers can follow after the core routing contract is proven.

---

## 4. Terminology

### Provider profile

A configured access path to one provider account or local endpoint. It contains non-secret metadata and a Secret Broker reference when credentials are required.

Examples:

- local Ollama at a specific URL;
- LM Studio on localhost;
- one OpenAI API account;
- one OpenRouter account;
- one Kimi API account;
- a Codex CLI host profile.

### Model target

A routable model deployment attached to one provider profile. It describes model-level capabilities, cost metadata, locality, tags, concurrency limits, and operator trust.

Examples:

- `local-qwen-coder-small`;
- `cloud-kimi-frontend`;
- `frontier-architecture`;
- `independent-reviewer`.

Target IDs are stable local identifiers. The underlying vendor model name may change without rewriting task policies.

### Native worker target

A later-stage target representing a whole agent runtime rather than one LLM provider call, such as a structured Codex worker. Native workers are explicitly separate from model targets because their lifecycle, permissions, artifacts, and cancellation semantics differ.

### Task contract

A normalized, bounded description of what a worker must do and what capabilities, permissions, evidence, and limits are required.

### Route policy

The operator-approved constraints and weights used to choose among eligible targets.

### Route decision

An immutable record of candidate filtering, scores, selected target, reason, policy revision, and predicted cost/success for one attempt.

### Route lease

The binding between one subagent attempt and one selected target. It prevents model roulette during a tool loop.

### Route outcome

The observed result of an attempt: validation status, failure category, usage, cost, latency, retries, escalation, artifacts, and reward components.

### Transport fallback

A retry or switch caused by a retryable operational provider failure before useful task state is produced.

### Capability escalation

A new attempt on a stronger target after evidence shows the prior attempt did not satisfy the task contract.

These must remain separate in code and telemetry.

---

## 5. Target architecture

```text
┌──────────────────────────────────────────────────────────────────┐
│ User / channel / routine objective                               │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Durable graph runtime                                            │
│ Planner -> Executor -> Scheduler/Subagents -> Reviewer/Recovery  │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Task Contract Compiler                                           │
│ role, family, ambiguity, tools, modality, context, risk, budget  │
│ permissions, acceptance evidence, locality/privacy               │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Adaptive Flock Routing Service                                   │
│ 1. hard capability/policy filter                                 │
│ 2. deterministic baseline score                                  │
│ 3. optional learned residual in shadow/constrained mode           │
│ 4. immutable decision + attempt lease                            │
└───────────────┬───────────────────────────────┬──────────────────┘
                ▼                               ▼
┌───────────────────────────┐     ┌───────────────────────────────┐
│ Provider/model inventory  │     │ Operational state             │
│ capabilities, costs, tags │     │ health, circuit, quota, load  │
└───────────────────────────┘     └───────────────────────────────┘
                │                               │
                └───────────────┬───────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Assigned subagent attempt                                        │
│ route-specific AgentConfig + existing tools/approval/worktree     │
└───────────────────────────────┬──────────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│ Evidence-backed acceptance validator                             │
└───────────────┬───────────────────────────────┬──────────────────┘
                │ pass                          │ fail
                ▼                               ▼
┌───────────────────────────┐     ┌───────────────────────────────┐
│ record successful outcome │     │ retry, escalate, or re-plan   │
└───────────────┬───────────┘     └───────────────┬───────────────┘
                └─────────────────┬───────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ SQLite route history -> episodic summary -> guarded procedural   │
│ learning / behavior-delta proposal after repeated evidence       │
└──────────────────────────────────────────────────────────────────┘
```

---

## 6. Non-negotiable invariants

1. **Routing never grants authority.** A selected model receives only the tools, workspace, scopes, and approvals already permitted for that task.
2. **No secret values in SQLite, events, spans, route decisions, capsules, prompts, or support bundles.** Only secret references, configured status, and redacted fingerprints may appear.
3. **One route lease per task attempt.** Do not switch models between tool rounds in an active attempt.
4. **Transport fallback and quality escalation are different state transitions.** They have different causes, records, and retry budgets.
5. **Evidence outranks self-confidence.** A worker claiming success is not success.
6. **Risk can force a stronger floor.** High-risk architecture, security, policy, broad mutation, or ambiguous tasks must not be cheap-first solely to save money.
7. **Review independence is configurable.** A reviewer can be required to use a different provider profile or model family from the implementer.
8. **Provider health is an eligibility input, not a permission input.** A healthy provider still cannot violate privacy, capability, or trust policy.
9. **Current run snapshots remain immutable.** Route decisions are explicit child-attempt records; they do not silently rewrite the run's original configuration snapshot.
10. **Subagent turns remain internal transcript scope.** Routing must not convert internal worker output into native user authority.
11. **Memory policy remains unchanged.** Raw telemetry belongs in SQLite. Durable procedural lessons require repeated validated outcomes. Policy changes remain explicitly gated.
12. **Mock behavior stays deterministic.** Candidate order, tie-breaking, score calculation, fallback, and escalation must be reproducible under tests.
13. **A direct operator target is honored or fails closed.** It is never silently overridden and mislabeled as automatic routing.
14. **Disabled routing reproduces current behavior.** Existing provider/model execution must remain the compatibility baseline.
15. **Failure to route must not strand lifecycle state.** The task/subagent must reach a deterministic blocked or failed outcome with a machine-readable reason.

---

## 7. Data model

The first implementation should use explicit typed domain objects and durable SQLite records. Avoid stuffing an undocumented routing blob into `TaskNodeRecord.result` and calling it architecture.

### 7.1 Task contract

Add `src/nested_memvid_agent/routing/contracts.py`:

```python
@dataclass(frozen=True)
class AgentTaskContract:
    schema_version: int
    task_id: str
    run_id: str
    role: str
    task_family: str
    objective: str

    complexity: float
    ambiguity: float
    autonomy_level: str
    risk: str

    required_tools: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    required_modalities: tuple[str, ...]
    minimum_context_tokens: int | None
    structured_output_required: bool

    filesystem_scope: tuple[str, ...]
    mutation_scope: str
    acceptance_criteria: tuple[str, ...]
    acceptance_evidence: tuple[str, ...]

    privacy_class: str
    local_preferred: bool
    local_required: bool
    maximum_cost_usd: float | None
    maximum_latency_seconds: float | None

    preferred_target_tags: tuple[str, ...]
    forbidden_target_tags: tuple[str, ...]
    preferred_provider_profiles: tuple[str, ...]
    forbidden_provider_profiles: tuple[str, ...]
```

Values derived from an LLM are advisory unless they narrow behavior. Deterministic task metadata, tool policy, risk, workspace policy, and operator settings remain authoritative.

### 7.2 Provider profile

Add a durable `provider_profiles` table and typed record:

```python
@dataclass(frozen=True)
class ProviderProfileRecord:
    profile_id: str
    display_name: str
    adapter: str
    auth_kind: str
    base_url: str | None
    secret_ref: str | None
    enabled: bool
    locality: str
    trust_class: str
    max_concurrency: int
    metadata: dict[str, Any]
    revision: int
    created_at: str
    updated_at: str
```

`auth_kind` begins with:

- `none`;
- `api_key`;
- `official_cli`;
- `external_gateway`.

The first implementation can use `none`, `api_key`, and existing `codex-cli`. Unsupported subscription-token proxying is not part of the design.

### 7.3 Model target

Add a `model_targets` table:

```python
@dataclass(frozen=True)
class ModelTargetRecord:
    target_id: str
    provider_profile_id: str
    model: str
    display_name: str
    enabled: bool
    model_family: str
    capability_tags: tuple[str, ...]
    role_affinities: tuple[str, ...]
    task_family_affinities: tuple[str, ...]
    max_context_tokens: int | None
    supports_tools: bool
    supports_json: bool
    supports_vision: bool
    supports_reasoning: bool
    supports_streaming: bool
    cost_input_per_million: float | None
    cost_output_per_million: float | None
    quality_tier: int
    latency_tier: int
    operator_priority: int
    metadata: dict[str, Any]
    revision: int
    created_at: str
    updated_at: str
```

Protocol capabilities currently exposed by `ProviderCapabilities` should remain provider-adapter facts. Model-specific capabilities belong on `ModelTargetRecord`. Do not pretend every model behind one adapter has identical context, modality, tool, or reasoning support.

### 7.4 Route policy

```python
@dataclass(frozen=True)
class RoutePolicyRecord:
    policy_id: str
    name: str
    mode: str
    enabled: bool
    weights: dict[str, float]
    constraints: dict[str, Any]
    escalation: dict[str, Any]
    revision: int
    created_at: str
    updated_at: str
```

Modes:

- `off`: current static run provider/model;
- `shadow`: calculate and persist counterfactual choices but execute the static target;
- `constrained`: route only eligible low-risk task families using deterministic policy and sufficiently supported learned residuals;
- `adaptive`: route all policy-eligible tasks, still under hard deterministic guardrails.

### 7.5 Route decision

```python
@dataclass(frozen=True)
class RouteDecisionRecord:
    decision_id: str
    run_id: str
    task_id: str
    subagent_id: str | None
    attempt: int
    status: str
    mode: str
    policy_id: str
    policy_revision: int
    contract_digest: str
    candidate_snapshot: tuple[dict[str, Any], ...]
    selected_target_id: str
    selected_profile_id: str
    selected_provider: str
    selected_model: str
    selection_kind: str
    predicted_success: float | None
    estimated_cost_usd: float | None
    score: float
    reason_codes: tuple[str, ...]
    router_version: str
    created_at: str
    started_at: str | None
    finished_at: str | None
```

Candidate snapshots must be bounded and redacted. Record target IDs, scores, eligibility, and reason codes—not prompts, credentials, raw provider errors, or arbitrary user text.

`selection_kind` should distinguish:

- `static_compatibility`;
- `operator_override`;
- `deterministic_router`;
- `learned_constrained`;
- `transport_fallback`;
- `capability_escalation`.

### 7.6 Route outcome

```python
@dataclass(frozen=True)
class RouteOutcomeRecord:
    outcome_id: str
    decision_id: str
    run_id: str
    task_id: str
    subagent_id: str | None
    attempt: int
    execution_status: str
    validation_passed: bool
    validation_codes: tuple[str, ...]
    failure_category: str | None
    provider_failure_code: str | None
    latency_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    actual_cost_usd: float | None
    tool_count: int
    changed_file_count: int | None
    retry_count: int
    escalated: bool
    reward_components: dict[str, float]
    outcome_labels: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    created_at: str
```

Provider outage and task-quality failure must be separate dimensions. A rate limit should reduce operational availability for that profile but should not be treated as proof that the model is poor at TypeScript.

### 7.7 Existing record extensions

Add nullable compatibility fields to `TaskNodeRecord`:

- `task_contract: dict[str, Any] | None`;
- `routing_policy_id: str | None`;
- `routing_override: dict[str, Any] | None`.

Add nullable fields to `SubagentRunRecord`:

- `route_decision_id: str | None`;
- `route_attempt: int`;
- `assigned_target_id: str | None`;
- `assigned_provider: str | None`;
- `assigned_model: str | None`.

The authoritative full details remain in route tables. These denormalized fields make task graph reads and the workbench efficient.

### 7.8 Schema migration

The frozen baseline is schema version 19. Use the next available forward-only version, expected to be 20 if no intervening migration lands.

Migration requirements:

1. preserve every current run, task, subagent, approval, trace, routine, and memory ledger row;
2. default existing rows to no contract and no route decision;
3. represent legacy execution as `static_compatibility` when surfaced;
4. add indexes for run/task/target/outcome lookup;
5. enforce foreign keys or equivalent application checks for route decisions and outcomes;
6. never migrate secret values into SQLite;
7. include migration and downgrade-refusal tests.

---

## 8. Provider and target inventory

### 8.1 Do not overload flat `AgentConfig`

`AgentConfig` and `RuntimeSettings` currently represent one active provider/model plus one fallback. They should continue to represent the default/static runtime configuration.

A variable-sized provider pool does not belong as dozens of new flat fields in `AgentConfig` or one giant JSON environment variable.

Recommended split:

- SQLite: provider profile and model target metadata;
- Secret Broker: API keys and other secret values;
- runtime settings: routing mode, default policy, budgets, and feature flags;
- per-run config snapshot: static defaults and routing policy revision;
- per-attempt route record: actual assigned target.

### 8.2 Provider profile service

Add `routing/registry.py` with a `ProviderRegistry` responsible for:

- CRUD over provider profiles and model targets;
- validation of adapter names against the existing provider factory;
- resolution of Secret Broker references only when constructing a provider;
- health lookup through the existing provider health registry;
- model discovery through the existing model catalog where available;
- deterministic target ordering;
- profile revision and enable/disable behavior;
- bounded connectivity tests;
- redacted public payloads.

### 8.3 Provider factory integration

Add a helper that compiles a selected target into a worker-specific configuration:

```python
def apply_model_target(
    base: AgentConfig,
    profile: ProviderProfileRecord,
    target: ModelTargetRecord,
) -> AgentConfig:
    return replace(
        base,
        provider=profile.adapter,
        model=target.model,
        base_url=profile.base_url,
        api_key_env=profile.secret_ref,
        fallback_provider=None,
        fallback_model=None,
        fallback_base_url=None,
        fallback_api_key_env=None,
    )
```

The existing `secret_resolver` already accepts a name or reference. A `secret://...` reference can therefore flow into provider construction without exposing the value.

Fallback should be attached by an explicit route-attempt policy, not inherited accidentally from the parent run's global fallback.

### 8.4 Capability sources

Capabilities can come from four layers, in descending authority:

1. hard adapter facts from code;
2. live provider/model discovery or certification;
3. operator-reviewed target metadata;
4. static suggestions.

The router must not infer critical capabilities from a model name substring alone. Name heuristics may suggest initial metadata in setup, but the operator must review it or certification must verify it.

### 8.5 Initial target templates

Ship templates, not vendor lock-in:

- `orchestrator-high-reasoning`;
- `architecture-high-reasoning`;
- `frontend-visual`;
- `coding-general`;
- `coding-bounded-local`;
- `repository-scout-local`;
- `documentation-low-cost`;
- `review-independent`;
- `local-only-general`.

A user may bind `frontend-visual` to Kimi today and another model later. Task policies depend on tags and capabilities, not a permanent vendor name.

---

## 9. Task contract compiler

Add `routing/contracts.py` and `routing/contract_compiler.py`.

### 9.1 Deterministic inputs

Start from data Kestrel already trusts:

- task profile;
- task title and goal;
- required tools;
- risk;
- acceptance criteria;
- acceptance evidence modes;
- dependencies;
- retry strategy;
- run autonomy mode;
- workspace and local/cloud policy;
- capability enablement;
- tool specs;
- repository metadata that can be calculated locally;
- explicit operator override.

### 9.2 Optional planner classification

When semantic orchestration is enabled, the planner may propose bounded routing guidance for existing task IDs:

```json
{
  "task_id": "task_...",
  "task_family": "frontend_implementation",
  "complexity": 0.55,
  "ambiguity": 0.35,
  "required_capabilities": ["tools", "structured_output"],
  "preferred_target_tags": ["frontend", "coding"],
  "minimum_context_tokens": 32000
}
```

The proposal may narrow or enrich classification. It may not:

- lower deterministic risk;
- remove required tools;
- remove acceptance criteria;
- grant write scope;
- change approval requirements;
- override local-only policy;
- name a secret;
- force a disabled target.

Invalid planner guidance is discarded and recorded as a bounded status code.

### 9.3 First task-family taxonomy

Keep the initial taxonomy small and observable:

- `planning`;
- `architecture`;
- `security_review`;
- `repository_inspection`;
- `frontend_design`;
- `frontend_implementation`;
- `backend_implementation`;
- `bounded_code_change`;
- `mechanical_refactor`;
- `test_and_validation`;
- `documentation`;
- `research`;
- `review`;
- `recovery`;
- `general`.

Do not start with fifty categories and no data.

### 9.4 Complexity and ambiguity

Complexity and ambiguity are separate:

- **Complexity** estimates the reasoning/context/tool burden.
- **Ambiguity** estimates how much judgment remains unspecified.

A repetitive 40-file rename can be operationally large but low ambiguity. A six-line concurrency fix can be small but high ambiguity. The former may suit a bounded local worker; the latter may require a stronger model.

---

## 10. Routing algorithm

Add `routing/router.py`, `routing/policy.py`, and `routing/scoring.py`.

### 10.1 Stage 0: explicit override

If a task or run contains an operator-approved direct target:

1. validate that the target exists and is enabled;
2. apply all hard capability, privacy, trust, and permission filters;
3. execute it as `operator_override`;
4. fail closed if invalid unless the operator explicitly allowed automatic fallback;
5. record that no automatic selection occurred.

### 10.2 Stage 1: hard eligibility filter

A target is removed when any required condition fails:

- profile or target disabled;
- missing credential/configuration;
- provider circuit open with no allowed probe;
- concurrency exhausted;
- context window too small;
- required tool protocol unsupported;
- structured output required but unsupported;
- vision required but unsupported;
- local-only task routed to cloud;
- provider/profile forbidden by policy;
- target trust class below risk floor;
- estimated worst-case cost exceeds a hard task budget;
- native worker requested but only model providers are eligible;
- target lacks an operator-required tag;
- reviewer diversity constraint violated;
- provider or model certification state fails the policy floor.

Hard filters return machine-readable reason codes.

### 10.3 Stage 2: deterministic baseline score

For each eligible target calculate normalized components:

- prior verified success for task family;
- prior verified success for repository/language/tool pattern;
- role affinity;
- task-family affinity;
- complexity fit;
- ambiguity fit;
- context headroom;
- operational health;
- recent latency;
- estimated cost;
- local preference;
- operator priority;
- cache/session affinity where safe;
- reviewer independence;
- recent failure rate;
- escalation history.

Recommended conceptual formula:

```text
score =
    success_weight * predicted_success
  + affinity_weight * role_and_task_fit
  + health_weight * operational_health
  + context_weight * context_headroom
  + locality_weight * local_preference
  + diversity_weight * reviewer_independence
  + operator_weight * operator_priority
  - cost_weight * normalized_estimated_cost
  - latency_weight * normalized_latency
  - failure_weight * recent_failure_rate
```

Weights live in a revisioned route policy. Tie-breaking is deterministic by score, quality tier, operator priority, and stable target ID.

Do not present fabricated precision. Early `predicted_success` is a smoothed heuristic based on sparse evidence and must be labeled accordingly.

### 10.4 Stage 3: learned residual

The first learned router must be a small, inspectable residual over the deterministic policy, not a free-form LLM deciding which vendor receives the prompt.

Recommended behavior:

- train from route outcomes;
- group by bounded feature buckets;
- estimate utility and success with minimum support;
- remain in shadow mode initially;
- abstain below support, confidence, or utility-margin thresholds;
- never make a hard-filtered target admissible;
- serialize model state and training-data window metadata;
- support deterministic replay.

A contextual bandit can be evaluated later. It should not be the first production implementation.

### 10.5 Stage 4: decision persistence and route lease

Before provider construction:

1. persist the task contract or its canonical digest;
2. persist the candidate snapshot;
3. persist the selected route decision;
4. atomically bind it to the subagent attempt;
5. emit `routing.selected`;
6. start a `route` span;
7. then construct and invoke the provider.

The target remains pinned for the attempt even if another target becomes cheaper or healthier mid-loop.

---

## 11. Role-aware behavior

Roles are routing hints, not fixed vendor assignments.

### Orchestrator/planner

Use a high reasoning and context floor for:

- decomposition;
- architecture decisions;
- ambiguous debugging;
- writing task contracts;
- recovery after repeated failure;
- deciding whether work should be delegated.

### Repository scout

Prefer local or low-cost targets when the contract is read-only and bounded:

- map modules;
- locate symbols;
- identify tests;
- summarize files;
- collect candidate evidence.

### Bounded coding worker

Prefer a local coding target when:

- files or directories are constrained;
- the intended change is explicit;
- acceptance commands are named;
- no architecture decision remains;
- tool calling and context requirements fit;
- one failed retry can safely escalate.

### Frontend/visual worker

Select a target with the required combination of:

- vision when screenshots/designs are involved;
- frontend coding affinity;
- adequate context;
- structured tool use;
- visual or UI outcome history.

A configured Kimi target may be preferred, but the rule must be capability and outcome based, not `if UI then Kimi`.

### Reviewer

Support policy constraints:

```yaml
review:
  require_different_target: true
  require_different_model_family: true
  prefer_different_provider_profile: true
```

The reviewer remains bound by evidence. A second model's opinion does not replace tests or signed repair artifacts.

### Security/architecture worker

Apply a strong minimum quality/trust tier and generally skip cheap-first routing. Cost optimization is subordinate to risk and ambiguity.

---

## 12. Execution wiring

### 12.1 New service

Add `src/nested_memvid_agent/routing/service.py`:

```python
class AgentRoutingService:
    def compile_contract(...): ...
    def preview(...): ...
    def assign(...): ...
    def start_attempt(...): ...
    def apply_assignment(...): ...
    def record_outcome(...): ...
    def next_action(...): ...
```

Dependencies:

- `AgentStateStore`;
- provider/target registry;
- existing provider health registry;
- Secret Broker resolver only through provider construction;
- `RunEventBus`;
- `SpanRecorder`;
- route policy store;
- optional learned residual.

### 12.2 Server construction

In `server.py`:

1. construct the Secret Broker as today;
2. construct `AgentStateStore` as today;
3. construct provider registry and route policy store using state + Secret Broker metadata boundary;
4. construct `AgentRoutingService`;
5. inject it into `RunManager`;
6. register routing API routes;
7. register provider secret environment names or secret references without exposing values.

CLI construction must use the same factory path so CLI, API, channels, routines, and web runs behave consistently.

### 12.3 Run initialization

Extend `_ensure_primary_task_graph()`:

- keep the deterministic starter DAG;
- compile a deterministic initial contract for every child task;
- store the route policy ID/revision on the root plan;
- do not select child routes at run creation because health, quota, and dependencies may change before execution;
- allow an explicit run-level target override to flow into task routing policy.

### 12.4 Planner node

Phase 1 behavior:

- the planner continues to use the static run provider/model;
- provider-refined semantic guidance may include bounded task classification fields.

Later behavior:

- route the planner node under an `orchestrator` policy;
- persist its route decision like any other model-bearing node;
- preserve one planner target for one planning attempt;
- do not let planner routing rewrite risk or permissions.

### 12.5 Scheduler and subagent creation

`create_subagent()` should not require the caller to name a provider. It should accept optional routing guidance or direct override while preserving the current API.

Before a worker thread starts, either:

- create a pending route assignment atomically with the task claim; or
- create it inside `_run_subagent()` immediately after confirming the task claim.

The second option requires fewer changes and is recommended for the first slice.

### 12.6 `_run_subagent()`

Recommended flow:

```python
subagent = state.get_subagent_run(subagent_id)
task = state.get_task_node(task_id)

contract = routing.compile_contract(run, task, subagent, config)
assignment = routing.assign(
    run=run,
    task=task,
    subagent=subagent,
    contract=contract,
    attempt=max(task.attempt_count, 0) + 1,
)

routed_config = routing.apply_assignment(config, assignment)
routed_config, worker_isolation = self._worker_config(...)

routing.start_attempt(assignment)
agent = self._build_agent(routed_config)
result = agent.chat(...)
validation = _validate_task_completion(task, result, ...)
outcome = routing.record_outcome(...)
action = routing.next_action(task, assignment, outcome)
```

Do not immediately convert every acceptance failure into a terminal generic exception. The routing service and scheduler need a typed decision:

- `complete`;
- `retry_same_target`;
- `escalate_target`;
- `replan`;
- `blocked`;
- `fail`.

Lifecycle transitions remain in `RunManager` and `AgentStateStore`.

### 12.7 Primary executor and reviewer

Add an optional assignment resolver to `GraphRuntimeServices` rather than having graph nodes know about provider tables.

```python
resolve_agent_config(
    config: AgentConfig,
    *,
    run_id: str,
    role: str,
    task: TaskNodeRecord | None,
) -> AgentAssignment
```

Roll this out after subagent routing is stable. The first release should route scheduler/subagent workers only, because that gives the largest cost benefit with the smallest blast radius.

### 12.8 Provider call receipts

Current `LLMResponse.usage` is optional and provider identity is not a first-class normalized receipt. Add a provider call receipt or equivalent bounded metadata:

```python
@dataclass(frozen=True)
class ProviderCallReceipt:
    profile_id: str
    target_id: str
    provider: str
    model: str
    request_kind: str
    started_at: str
    latency_seconds: float
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    fallback_from: str | None
```

Aggregate receipts into the `AgentTurnResult` or durable trace so route outcomes can calculate actual attempt usage without scraping arbitrary provider raw payloads.

---

## 13. Retry, fallback, escalation, and recovery

### 13.1 Transport fallback

Transport fallback is allowed when:

- the provider error is classified retryable;
- no unsafe side effect occurred;
- no provider-side continuation state must be preserved;
- the route policy names an eligible fallback;
- the retry budget remains.

Record the fallback as a child of the original decision or a new decision linked by `supersedes_decision_id`. Do not hide it inside an opaque provider raw payload.

### 13.2 Same-target retry

Allow one same-target retry when:

- failure is narrow and actionable;
- the retry strategy is materially different;
- the task remains within budget;
- the previous attempt did not create unresolved side effects;
- validator feedback can be compactly supplied.

The existing changed-strategy gate should remain authoritative.

### 13.3 Capability escalation

Escalate to the next eligible quality tier when:

- acceptance validation fails after the allowed bounded retry;
- tool-call/control output remains invalid;
- context/capability limits are discovered;
- the worker reports a blocker corroborated by evidence;
- the reviewer rejects the result with valid evidence;
- the task's observed ambiguity is higher than the initial contract.

Escalation creates a new attempt and a new route decision. It must not silently mutate the active lease.

### 13.4 Re-plan

Return to the orchestrator when:

- two capability tiers fail;
- failures indicate the task contract itself is wrong;
- dependencies or file scopes must change;
- risk classification must increase;
- the work should be split into smaller tasks;
- a native agent rather than a model worker is required.

Initially, re-plan may produce a blocked/failure artifact for operator review because fully dynamic DAG rewriting is not yet implemented. Dynamic plan revision can be a later phase.

### 13.5 Failure packet

Retries and escalation should receive a bounded, structured failure packet:

```json
{
  "attempt": 1,
  "target_id": "local-coder-small",
  "expected": ["targeted tests pass", "only two files change"],
  "observed": ["typecheck failed: missing exported property"],
  "validation_codes": ["typecheck_failed"],
  "changed_files": ["web/src/Card.tsx", "web/src/Card.test.tsx"],
  "retry_instruction": "Correct the exported type without changing other files."
}
```

Never dump an entire raw transcript into the retry prompt.

---

## 14. Outcome learning and memory integration

### 14.1 Raw telemetry location

Raw route decisions and outcomes belong in SQLite because they are control-plane facts, counters, and receipts.

Do not write every candidate score or token count into permanent `.mv2` memory.

### 14.2 Episodic memory

Write an episodic summary only for meaningful events:

- successful completion after escalation;
- repeated failure on one target/task family;
- operator override and correction;
- a new verified routing pattern;
- a routing safety block;
- reviewer diversity catching a defect.

### 14.3 Procedural memory

After repeated verified outcomes, propose a procedural lesson such as:

> For bounded TypeScript changes under three files with named tests and no architecture decision, target `local-coder-small` succeeds reliably. Use it first and escalate after one evidence-backed failed retry.

Promotion requirements should include:

- minimum distinct task/run count;
- validation evidence;
- no unresolved safety incidents;
- confidence and outcome rates;
- target and policy revision context;
- explicit provenance.

### 14.4 Policy memory and behavior deltas

A learned route pattern must not directly rewrite global routing policy.

Changes such as “all frontend work should use target X” or “security review can use a lower tier” are behavior changes. They must flow through behavior-delta proposal, replay, MutationGate, operator review where required, and rollback support.

### 14.5 Separate learned router

Create `routing/learner.py` with concepts modeled after `OutcomeCalibratedRouter`:

- `AgentRoutingExample`;
- `AgentRoutingPrediction`;
- `AgentRouteStats`;
- `AgentRoutingEvaluation`;
- shadow and constrained modes;
- serialization and replay.

Do not import memory-layer targets into agent routing or make `OutcomeCalibratedRouter` generic through a tangle of type erasure. Shared small utilities can be extracted only after both implementations prove the abstraction.

### 14.6 Reward components

Keep reward dimensions inspectable:

```text
completion reward
+ acceptance-validation reward
+ user confirmation/correction reward
- unsafe or out-of-scope mutation penalty
- unnecessary escalation penalty
- normalized cost penalty
- normalized latency penalty
```

Suggested initial labels:

- `validated_success`;
- `validated_success_after_retry`;
- `validated_success_after_escalation`;
- `acceptance_failed`;
- `provider_unavailable`;
- `invalid_tool_protocol`;
- `out_of_scope_change`;
- `review_rejected`;
- `user_corrected`;
- `user_accepted`;
- `cancelled`;
- `outcome_unknown`.

Do not collapse all failures into one negative reward.

---

## 15. Security and trust model

### 15.1 Credentials

- Store provider metadata in SQLite.
- Store API keys in the existing Secret Broker.
- Store only `secret://...` references on provider profiles.
- Resolve secrets just in time during provider construction.
- Register resolved values with the existing redaction boundary.
- Never send one provider's credential to another target or plugin.
- Do not include raw authentication errors in route outcomes.

### 15.2 Official CLI/native agent profiles

The current `codex-cli` provider is a host-process response provider with explicit trust-domain restrictions. Adaptive Flock must preserve those restrictions.

Later native workers should have a separate target kind and lifecycle adapter. Do not disguise an entire Codex session as an ordinary chat-completion target.

### 15.3 Permissions

A model target has no authority to expand:

- enabled tool set;
- exact-call approval policy;
- workspace path;
- worktree isolation;
- file allowlist;
- network access;
- secret access;
- remote Git/GitHub mutation settings;
- policy-memory authority.

The task and runtime establish permissions first. Routing only selects an eligible executor.

### 15.4 Local/cloud privacy

Add privacy classes:

- `local_required`;
- `local_preferred`;
- `approved_cloud`;
- `any`.

Default repository tasks may be `approved_cloud` unless the operator chooses local-only. Tasks involving sensitive file patterns or configured paths can be deterministically upgraded to `local_required`.

### 15.5 Review diversity

Independent review reduces correlated mistakes but also increases data exposure. A different-provider requirement must still obey the same privacy class and operator allowlist.

### 15.6 Route plugin boundary

The first implementation should not execute arbitrary routing plugins in-process. Route policies and target metadata are data, not executable extension code.

If third-party scoring plugins are later supported, use the repository's existing extension containment principles: signed/reviewed provenance, explicit capabilities, no secrets by default, bounded input/output, and process/WASI/container isolation.

---

## 16. API design

Create `src/nested_memvid_agent/server_routing_routes.py`.

### Read endpoints

```text
GET /api/routing/status
GET /api/routing/providers
GET /api/routing/providers/{profile_id}
GET /api/routing/targets
GET /api/routing/targets/{target_id}
GET /api/routing/policies
GET /api/routing/policies/{policy_id}
GET /api/routing/outcomes
GET /api/runs/{run_id}/routing
GET /api/runs/{run_id}/tasks/{task_id}/routing
```

### Mutation endpoints

All mutations require the normal owner/API-auth boundary and revision checks:

```text
POST   /api/routing/providers
PUT    /api/routing/providers/{profile_id}
DELETE /api/routing/providers/{profile_id}
POST   /api/routing/providers/{profile_id}/probe

POST   /api/routing/targets
PUT    /api/routing/targets/{target_id}
DELETE /api/routing/targets/{target_id}

POST   /api/routing/policies
PUT    /api/routing/policies/{policy_id}
POST   /api/routing/preview
POST   /api/runs/{run_id}/tasks/{task_id}/routing-override
```

Provider secret values should continue to use the Secret Broker routes. Provider creation accepts a secret reference, not a raw key, unless a single transaction is explicitly implemented through the Secret Broker service without echoing the value.

### Preview endpoint

`POST /api/routing/preview` must be side-effect free. It returns:

- compiled task contract;
- eligible and rejected targets;
- reason codes;
- scores;
- proposed target;
- estimated cost range;
- policy revision;
- whether a learned residual would abstain.

It must not call the selected model.

### Existing request compatibility

`CreateRunRequest.provider` and `.model` continue to work as a run-level direct/static override.

Add optional fields only after compatibility tests:

- `routing_policy_id`;
- `routing_mode`;
- `maximum_cost_usd`;
- `local_required`.

`SubagentRequest` may later add:

- `routing_policy_id`;
- `target_id` direct override;
- `maximum_cost_usd`.

---

## 17. CLI design

Add a `nest-agent routing` command family:

```text
nest-agent routing status
nest-agent routing providers list
nest-agent routing providers add
nest-agent routing providers inspect <profile_id>
nest-agent routing providers enable <profile_id>
nest-agent routing providers disable <profile_id>
nest-agent routing providers probe <profile_id>

nest-agent routing targets list
nest-agent routing targets add
nest-agent routing targets inspect <target_id>

nest-agent routing policies list
nest-agent routing policies inspect <policy_id>
nest-agent routing preview --task-id <task_id>
nest-agent routing report --run-id <run_id>
nest-agent routing eval --scenario <path>
```

CLI output must support `--json`. Secret entry should use the existing Secret Broker path and avoid command-line arguments that leak through process listings or shell history.

---

## 18. Workbench design

Do not extend the current provider selector into an unreadable mega-form.

Add dedicated components, for example:

```text
web/src/routing/RoutingCenter.tsx
web/src/routing/ProviderProfilesPanel.tsx
web/src/routing/ModelTargetsPanel.tsx
web/src/routing/RoutePolicyEditor.tsx
web/src/routing/RouteDecisionCard.tsx
web/src/routing/RouteOutcomeTable.tsx
web/src/routing/RoutingPreview.tsx
web/src/routing/api.ts
web/src/routing/types.ts
```

### Task graph additions

Every task/subagent card should show:

- role and task family;
- selected provider profile and model target;
- automatic, override, fallback, or escalation badge;
- why it was selected;
- candidates rejected by hard constraints;
- attempt number;
- estimated and actual cost when available;
- latency and token usage;
- worktree/isolation status;
- validation status and evidence;
- escalation history;
- current route lease.

### Routing Center

Provide:

1. provider profiles with configured/validated/healthy state;
2. model targets with tags, capabilities, quality/cost/latency tiers;
3. policy editor with readable guardrails and weights;
4. shadow-mode comparison dashboard;
5. task-family success/cost table;
6. local-versus-cloud usage;
7. route failure and escalation reasons;
8. operator override history.

### Events

Add to the UI event allowlist and friendly labels:

- `routing.contract_compiled`;
- `routing.previewed`;
- `routing.selected`;
- `routing.attempt_started`;
- `routing.transport_fallback`;
- `routing.retry_scheduled`;
- `routing.escalated`;
- `routing.replan_requested`;
- `routing.outcome_recorded`;
- `routing.shadow_decision`;
- `routing.guardrail_blocked`.

Add `route` as a trace span type.

---

## 19. Observability and operations

### Metrics

Extend operational metrics with bounded labels only:

- routing mode;
- configured/enabled/healthy profile counts;
- enabled target counts by locality and quality tier;
- decisions by selection kind;
- outcomes by validation status and failure category;
- escalation rate;
- same-target retry rate;
- transport fallback rate;
- average estimated and actual cost;
- local/cloud task ratio;
- per-task-family success rate;
- route-decision latency;
- tasks blocked because no eligible target exists.

Never use arbitrary task text, repository paths, provider error bodies, or model-generated labels as metric dimensions.

### Alerts

Add alerts for:

- `routing_no_eligible_targets`;
- `routing_all_profiles_degraded`;
- `routing_escalation_rate_high`;
- `routing_cost_budget_exhausted`;
- `routing_outcome_backlog`;
- `routing_model_state_stale`;
- `routing_shadow_regression`.

### Support bundle

Include:

- routing mode and policy revision;
- redacted provider/target inventory;
- counts by decision/outcome status;
- bounded recent reason codes;
- learned-router metadata and training window;
- no prompts, secret values, raw provider responses, or `.mv2` contents.

---

## 20. File-level implementation map

### New backend modules

```text
src/nested_memvid_agent/routing/__init__.py
src/nested_memvid_agent/routing/models.py
src/nested_memvid_agent/routing/contracts.py
src/nested_memvid_agent/routing/contract_compiler.py
src/nested_memvid_agent/routing/registry.py
src/nested_memvid_agent/routing/policy.py
src/nested_memvid_agent/routing/scoring.py
src/nested_memvid_agent/routing/router.py
src/nested_memvid_agent/routing/service.py
src/nested_memvid_agent/routing/outcomes.py
src/nested_memvid_agent/routing/learner.py
src/nested_memvid_agent/routing/evaluation.py
src/nested_memvid_agent/server_routing_routes.py
scripts/eval_agent_routing.py
```

### Backend modifications

```text
src/nested_memvid_agent/config.py
src/nested_memvid_agent/runtime_settings.py
src/nested_memvid_agent/state_store.py
src/nested_memvid_agent/run_manager.py
src/nested_memvid_agent/graph_runtime.py
src/nested_memvid_agent/app_factory.py
src/nested_memvid_agent/agent.py
src/nested_memvid_agent/runtime_models.py
src/nested_memvid_agent/llm/base.py
src/nested_memvid_agent/llm/factory.py
src/nested_memvid_agent/llm/model_catalog.py
src/nested_memvid_agent/llm/resilience.py
src/nested_memvid_agent/server.py
src/nested_memvid_agent/server_models.py
src/nested_memvid_agent/operational_metrics.py
src/nested_memvid_agent/support_bundle.py
src/nested_memvid_agent/task_capsule.py
src/nested_memvid_agent/tracing.py
src/nested_memvid_agent/cli.py
```

`llm/resilience.py` may need only read access from routing; do not distort circuit-breaker code into a routing engine.

### New tests

```text
tests/test_agent_routing_contracts.py
tests/test_agent_routing_registry.py
tests/test_agent_routing_policy.py
tests/test_agent_routing_state.py
tests/test_agent_routing_service.py
tests/test_agent_routing_integration.py
tests/test_agent_routing_learning.py
tests/test_server_routing_routes.py
tests/evals/agent_routing/*.json
```

### Existing test modifications

```text
tests/test_state_store.py
tests/test_full_agent_runtime.py
tests/test_semantic_orchestration.py
tests/test_run_backpressure.py
tests/test_provider_resilience.py
tests/test_runtime_settings.py
tests/test_server.py
web/src/App.test.tsx
web/src/runActivity.test.ts
```

### New web modules

```text
web/src/routing/RoutingCenter.tsx
web/src/routing/ProviderProfilesPanel.tsx
web/src/routing/ModelTargetsPanel.tsx
web/src/routing/RoutePolicyEditor.tsx
web/src/routing/RouteDecisionCard.tsx
web/src/routing/RouteOutcomeTable.tsx
web/src/routing/RoutingPreview.tsx
web/src/routing/api.ts
web/src/routing/types.ts
```

### Documentation updates after implementation

```text
README.md
PROJECT_MANIFEST.md
docs/FULL_AGENT_SPEC.md
docs/RUNTIME_WIRING.md
docs/IMPLEMENTATION_STATUS.md
docs/IMPLEMENTATION_PIPELINE.md
docs/TEST_MATRIX.md
docs/SECURITY.md
docs/PRODUCTION_OPERATIONS.md
CHANGELOG.md
.env.example
```

---

## 21. Delivery phases

## Phase 0 — Baseline and feature contract

**Goal:** Freeze current behavior and prevent routing work from breaking static execution.

### Changes

- Add this plan and architecture decision.
- Record baseline commit and schema version.
- Add static compatibility fixtures showing current run/subagent provider inheritance.
- Add a feature flag with routing disabled by default.
- Define canonical reason codes and schema versions.

### RED tests

- Routing disabled produces the same provider/model and task lifecycle as the baseline.
- Mock runs remain byte/determinism compatible where the current contract requires it.
- No routing tables are required for existing startup.

### Acceptance gate

Current compile, unit, golden, web test, and web build commands pass unchanged.

---

## Phase 1 — Provider profiles, model targets, and state migration

**Goal:** Create a safe inventory without changing execution.

### Files

- `state_store.py`;
- new routing models/registry modules;
- Secret Broker integration;
- model catalog integration;
- focused state and registry tests.

### Changes

- Add forward-only schema migration.
- Add provider profile/model target/policy/decision/outcome tables.
- Add revision-checked CRUD.
- Store secret references only.
- Add deterministic seeded targets for mock mode.
- Add provider/target public redaction payloads.
- Add health lookup and capability validation.

### RED tests

- Schema v19 migrates without data loss.
- Secret values never appear in SQLite bytes, API payloads, events, or support bundle fixtures.
- Disabled profiles/targets are not eligible.
- Deleting a referenced profile fails safely or disables it without orphaning history.
- Concurrent stale revisions return conflicts.
- Target ordering is deterministic.

### Acceptance gate

Provider inventory can be created, read, updated, disabled, and probed without changing run execution.

---

## Phase 2 — Task contracts and shadow router

**Goal:** Calculate explainable routes without executing them.

### Files

- contract compiler;
- deterministic policy/scoring/router;
- run task initialization;
- route preview API/CLI;
- shadow decision persistence.

### Changes

- Compile contracts from current task metadata.
- Add optional bounded planner classification.
- Implement hard filters and deterministic score.
- Persist shadow candidate snapshots and selected targets.
- Emit shadow events and spans.
- Keep actual execution on the static run target.

### RED tests

- Local-required tasks reject every cloud target.
- Tool-required tasks reject targets without tool support.
- Context requirements reject undersized targets.
- Risk floors reject low-trust/low-tier targets.
- Reviewer diversity rejects the implementer's model family when configured.
- Stable input produces stable scores and tie-breaks.
- Planner guidance cannot lower risk or remove tools/criteria.
- Shadow mode never changes the executed provider.

### Acceptance gate

A routing preview explains every eligible/rejected target and shadow decisions can be replayed deterministically.

---

## Phase 3 — Subagent route assignment

**Goal:** Route low-risk scheduler workers while preserving existing lifecycle and safety.

### Files

- `RunManager` injection and `_run_subagent()` seam;
- routing service;
- per-worker config compiler;
- route spans/events;
- subagent/task record extensions.

### Changes

- Assign a target after the task claim and before provider construction.
- Persist and bind one route lease to one subagent attempt.
- Apply the target to a copied `AgentConfig`.
- Preserve worker isolation, tools, approvals, and transcript scope.
- Add static/direct override behavior.
- Route only approved low-risk read-only or tightly bounded tasks in constrained mode.

### RED tests

- Two subagents in one run can use different providers/models.
- A route stays pinned through multiple tool rounds.
- A selected target cannot change workspace or tool enablement.
- Cancellation fences late route outcomes.
- Restart never replays an unknown in-flight provider call.
- Missing eligible target fails with a stable reason.
- Direct override is honored and labeled.
- Disabled mode retains current behavior.

### Acceptance gate

A deterministic integration fixture runs one planner/static parent plus two child workers on distinct mock targets and records correct route decisions/outcomes.

---

## Phase 4 — Validation-driven retry and escalation

**Goal:** Make inexpensive workers safe through evidence, not optimism.

### Files

- route outcomes;
- scheduler retry integration;
- provider call receipts;
- validation failure packet;
- escalation policy.

### Changes

- Record normalized usage, latency, and validation outcomes.
- Distinguish provider failure from task failure.
- Allow one changed-strategy same-target retry where safe.
- Escalate to a stronger eligible tier after evidence failure.
- Return re-plan when the contract is wrong.
- Enforce per-task and per-run budgets.

### RED tests

- Retryable provider outage triggers transport fallback, not a quality penalty.
- Acceptance failure triggers a new attempt, not an in-loop model swap.
- Unchanged strategy is blocked.
- Escalation cannot violate local-only or risk policy.
- Budget exhaustion blocks further attempts with an explicit reason.
- Tests/lint claims require trusted validation evidence.
- Unknown side-effect outcome prevents automatic replay.

### Acceptance gate

A small mock worker fails validation, retries with a changed strategy, escalates once, and completes with a full inspectable decision chain.

---

## Phase 5 — Planner, reviewer, and provider diversity

**Goal:** Route graph roles, not only child workers.

### Changes

- Add role assignment resolver to graph runtime services.
- Route semantic planner calls under an orchestrator policy.
- Route reviewer calls independently.
- Add different-target/family/profile constraints.
- Keep deterministic reviewer fallback.
- Record planner/reviewer route outcomes separately.

### RED tests

- Planner and reviewer can use different targets from the implementer.
- Review diversity never violates privacy policy.
- Invalid provider review falls back to deterministic evidence gate.
- Reviewer model opinion cannot prove tests passed without trusted receipts.

### Acceptance gate

A full graph fixture shows orchestrator -> local scout -> cloud implementer -> independent reviewer with evidence-preserving lifecycle.

---

## Phase 6 — API, CLI, workbench, and operations

**Goal:** Make routing understandable and operable.

### Changes

- Add routing routes and Pydantic models.
- Add CLI command family.
- Add Routing Center components.
- Extend task graph cards, event labels, traces, metrics, alerts, readiness, and support bundles.
- Remove duplicated hard-coded provider metadata where server catalogs can be authoritative.

### RED tests

- API mutations require auth and revision checks.
- Preview is read-only.
- UI renders selected route, reason, validation, and escalation.
- Support bundle contains no prompt/secret/raw response.
- Metrics use bounded labels.
- Provider profile disabling revokes future selection but does not corrupt historical records.

### Acceptance gate

An operator can configure profiles/targets, preview a decision, run a routed task, and inspect the complete outcome without reading raw database rows.

---

## Phase 7 — Outcome-calibrated learning

**Goal:** Personalize selection from verified local history.

### Changes

- Build routing examples from outcome records.
- Implement shadow residual and replay harness.
- Add minimum support, confidence, utility margin, and abstention.
- Add constrained activation for low-risk task families.
- Add episodic summaries and procedural lesson proposals.
- Route policy changes remain behavior-delta gated.

### RED tests

- Sparse evidence causes abstention.
- Hard-filtered targets never become eligible through learning.
- Provider outage does not become task-quality punishment.
- Replayed history produces deterministic model state.
- Learned residual can demonstrate utility lift on synthetic fixtures.
- Policy/high-risk behavior cannot auto-change through route outcomes.

### Acceptance gate

Shadow evaluation shows no guardrail violations and an explicitly measured utility/cost improvement before constrained mode is enabled.

---

## Phase 8 — Native agent workers and branch fan-out

**Goal:** Route whole jobs to structured native workers after model routing is stable.

### Changes

- Add `target_kind=native_agent`;
- define native worker lifecycle adapter;
- support structured start/status/steer/cancel/artifact collection;
- integrate Codex as a first native worker using an officially supported local interface;
- preserve worktree isolation and exact approval boundaries;
- add candidate branch review and merge proposal artifacts;
- do not auto-merge or publish remotely.

### RED tests

- Native worker cancellation is verified.
- Credentials remain in the correct trust domain.
- Artifacts bind to the expected worktree/branch/diff.
- Multiple workers cannot overwrite each other's branches.
- Merge proposal requires independent validation/review.

### Acceptance gate

Kestrel can delegate one bounded repository job to a native Codex worker, collect artifacts, validate them, and return a reviewable local result without remote mutation.

---

## Phase 9 — Evaluation and release gate

**Goal:** Prove the feature reduces cost without creating a cheaper failure machine.

### Required scenario families

- read-only repository inspection;
- mechanical refactor;
- bounded TypeScript change;
- frontend implementation;
- broad UI redesign requiring vision;
- architecture decision;
- security review;
- test-only validation;
- provider timeout/rate limit;
- insufficient context;
- local-only repository;
- invalid tool protocol;
- out-of-scope file mutation;
- reviewer disagreement;
- cancellation/restart;
- budget exhaustion.

### Baselines

Compare:

1. current static target;
2. deterministic Adaptive Flock policy;
3. shadow learned residual;
4. constrained learned policy when eligible.

### Metrics

- task acceptance success;
- trusted validation success;
- safety/permission violations;
- cost per successful task;
- latency per successful task;
- local/cloud task ratio;
- same-target retries;
- escalation rate;
- re-plan rate;
- false-success rate;
- operator correction rate;
- routing abstention rate.

### Proposed release targets

Targets must be confirmed after baseline collection, but the initial product objective is:

- zero routing-caused permission or approval bypasses;
- zero local-only cloud disclosures;
- no statistically meaningful degradation in acceptance success for eligible tasks;
- at least 30% lower cloud-model cost on the low-risk eligible fixture set;
- bounded escalation with no infinite route loops;
- deterministic mock replay;
- no secret leakage in state, logs, events, traces, API, UI, or support bundles.

---

## 22. Configuration defaults

Add conservative settings:

```text
enable_adaptive_flock = false
routing_mode = off
default_routing_policy = balanced
routing_max_attempts_per_task = 3
routing_max_same_target_retries = 1
routing_max_escalations_per_task = 1
routing_task_budget_usd = unset
routing_run_budget_usd = unset
routing_local_preference = 0.5
routing_require_independent_review = false
routing_learned_min_examples = 5
routing_learned_confidence_threshold = 0.70
routing_learned_activation_margin = 0.08
```

Recommended rollout defaults:

1. off after migration;
2. operator enables shadow mode;
3. constrained mode only for low-risk read-only and bounded worker tasks;
4. adaptive mode remains an explicit operator choice.

---

## 23. Example end-to-end run

Objective:

> Redesign the settings page, implement the approved design, update repetitive component props, run the frontend suite, and review the final diff.

### Planned tasks

1. **Architecture/planning**
   - high ambiguity;
   - broad context;
   - strong orchestrator target.

2. **Visual/frontend design**
   - vision and frontend affinity;
   - configured visual target, possibly a Kimi target;
   - read/write isolated worktree;
   - screenshot and accessibility acceptance evidence.

3. **Mechanical component updates**
   - low ambiguity;
   - exact file list and prop contract;
   - local coding target;
   - targeted tests and typecheck.

4. **Frontend validation**
   - tool-oriented;
   - local or low-cost target;
   - trusted test/lint/browser receipts.

5. **Independent review**
   - different model family/profile from the implementers;
   - read-only;
   - must cite diff and validation evidence.

### Failure behavior

If the local coding worker produces a type error:

1. record failed validation;
2. send one bounded failure packet to the same target with changed strategy;
3. if it fails again, escalate only that task;
4. do not rerun the design worker or replace the orchestrator;
5. record which target ultimately succeeded;
6. use the outcome as routing evidence only after validation.

This is the intended economic advantage: expensive intelligence makes decisions; inexpensive intelligence performs constrained work; evidence decides whether the bargain was real.

---

## 24. Risk register

### Misclassification routes hard work to a weak model

**Mitigation:** ambiguity and risk floors, constrained rollout, one bounded retry, evidence-triggered escalation, learned abstention.

### Cheap workers consume more total tokens than one strong worker

**Mitigation:** measure cost per successful task, cap attempts, include escalation cost in outcomes, penalize repeated failure.

### Stale cost/capability metadata

**Mitigation:** model target revisions, provider certification, operator review, timestamps, conservative unknown handling.

### Provider/model behavior changes silently

**Mitigation:** target version/revision history, periodic certification fixtures, route outcome drift alerts.

### Correlated reviewer mistakes

**Mitigation:** optional model-family/provider diversity plus deterministic evidence gates.

### Cross-provider privacy exposure

**Mitigation:** hard privacy filters before scoring, explicit provider allowlists, local-required mode.

### Credential sprawl

**Mitigation:** one Secret Broker, references only, just-in-time resolution, no plugin access.

### RunManager becomes more monolithic

**Mitigation:** dedicated routing service/modules; lifecycle remains in RunManager but algorithms do not.

### Workbench becomes harder to maintain

**Mitigation:** dedicated routing components and API module; server-authoritative provider/target catalog.

### Learning overfits one repository or user

**Mitigation:** separate global, repository, and task-family statistics; smoothing; minimum support; shadow evaluation; abstention.

### Learning weakens safety policy

**Mitigation:** learned residual can rank only hard-admissible targets; behavior changes remain MutationGate controlled.

### Route loops never terminate

**Mitigation:** per-task attempt/retry/escalation budgets, durable counters, explicit re-plan/terminal states.

---

## 25. Resolved architectural decisions

1. **Name:** Use `Adaptive Flock` as the feature name unless product naming changes later.
2. **Primary unit:** Route task attempts, not individual chat turns.
3. **First target scope:** Scheduler/subagent model providers; planner/reviewer routing follows.
4. **Provider metadata:** SQLite.
5. **Secrets:** Existing Secret Broker only.
6. **Execution engine:** Existing `RunManager`, graph runtime, and agent loop.
7. **Provider adapters:** Existing `llm` factory and adapters.
8. **Safety authority:** Existing capability, approval, worktree, validation, repair, and memory gates.
9. **Learning:** Separate agent-routing learner; shadow first.
10. **Dynamic DAG mutation:** Deferred until route execution and validation are stable.
11. **Native agents:** Separate target kind and later phase.
12. **Generic proxy:** Not part of this feature.
13. **Vendor mapping:** Operator-configured target templates, never permanent role-to-vendor rules.
14. **Reviewer evidence:** Independent model review supplements but never replaces trusted runtime evidence.

---

## 26. Definition of done

Adaptive Flock is complete for the supported single-user, single-node profile only when all of the following are true:

1. Multiple subagents in one run can durably use different configured targets.
2. Every task attempt has an inspectable task contract, route decision, route lease, and outcome.
3. Routing disabled preserves current static behavior.
4. Hard filters prevent capability, privacy, trust, budget, and reviewer-diversity violations.
5. Routing cannot expand tools, permissions, workspace, network, secret, or approval authority.
6. Transport fallback and capability escalation are separately modeled and tested.
7. Attempt routes remain sticky through tool loops.
8. Acceptance validation—not model self-report—determines success.
9. Retry/escalation budgets terminate deterministically.
10. Provider profiles use Secret Broker references and leak no secret values.
11. Restart, cancellation, approval wait, worker-claim, and route-outcome races are adversarially tested.
12. Route events, spans, metrics, API, CLI, and workbench views are redacted and useful.
13. Shadow replay demonstrates no guardrail violations.
14. Constrained routing meets the agreed cost/success target on the routing eval suite.
15. Learned routing abstains under insufficient evidence and cannot override deterministic guardrails.
16. Meaningful routing lessons enter Kestrel memory only through existing evidence and promotion gates.
17. Core compile, unit, golden, frontend, security, and optional provider integration gates pass on the exact final bytes.
18. Documentation truthfully updates implementation status; no planned feature is described as landed before tests prove it.

---

## 27. Recommended first implementation pull requests

Keep the work reviewable. Do not ship this as one heroic mega-PR.

### PR 1 — Routing domain and schema

- typed records;
- migration;
- provider profile/model target registry;
- Secret Broker references;
- deterministic mock inventory;
- state tests.

### PR 2 — Task contract and shadow preview

- contract compiler;
- hard filters;
- deterministic scorer;
- preview API/CLI;
- shadow persistence and tests.

### PR 3 — Subagent assignment

- RunManager service injection;
- route lease;
- per-worker config;
- events/spans;
- two-target deterministic integration fixture.

### PR 4 — Outcome receipts and escalation

- provider call receipts;
- route outcomes;
- typed retry/escalation;
- budget enforcement;
- adversarial lifecycle tests.

### PR 5 — Workbench and operations

- Routing Center;
- task graph route cards;
- metrics/readiness/support bundle;
- frontend tests.

### PR 6 — Planner/reviewer diversity

- graph role assignment;
- independent reviewer policy;
- deterministic evidence fallback.

### PR 7 — Learned shadow router

- route examples/model;
- replay harness;
- dashboard;
- constrained activation gate.

### PR 8 — Native Codex worker experiment

- separate native-worker contract;
- structured lifecycle;
- isolated artifact collection;
- no automatic merge or remote publication.

---

## 28. Validation commands

Every phase should continue to pass the current baseline:

```bash
python -m compileall -q src tests scripts
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
python -m ruff check scripts src tests
python -m mypy src
npm run test --prefix web
npm run build --prefix web
```

Add focused routing gates:

```bash
python -m pytest -q \
  tests/test_agent_routing_contracts.py \
  tests/test_agent_routing_registry.py \
  tests/test_agent_routing_policy.py \
  tests/test_agent_routing_state.py \
  tests/test_agent_routing_service.py \
  tests/test_agent_routing_integration.py \
  tests/test_agent_routing_learning.py \
  tests/test_server_routing_routes.py

python scripts/eval_agent_routing.py \
  --scenario-dir tests/evals/agent_routing \
  --mode shadow \
  --fail-on-guardrail-violation
```

Optional live-provider evaluation must remain explicitly gated, isolated, redacted, time/cost bounded, and non-mutating unless a dedicated approved fixture says otherwise.

---

## 29. Final recommendation

Build this feature.

Do it as **Kestrel's governed heterogeneous-agent assignment system**, not as a generic routing gateway. The repository already has the rare parts that make the idea defensible: durable task claims, isolated workers, exact-call approval, evidence-backed validation, recovery, task capsules, inspectable memory, and guarded learning.

The first valuable milestone is not a learned router. It is a deterministic, explainable, policy-safe route service that can assign two child tasks in one run to two different model targets and prove through acceptance evidence whether each assignment worked.

Once that foundation is trustworthy, Kestrel can learn something most universal routers cannot:

> which model or native worker actually succeeds on this user's repositories, under this task contract, with these tools, permissions, validators, and costs.

That is the feature's real moat.