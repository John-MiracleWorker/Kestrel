# Adaptive Flock Routing Demo

Kestrel's Adaptive Flock subsystem selects an eligible model target for each
task role while preserving the task's existing authority boundary. This
deterministic demo makes that routing behavior inspectable without requiring
credentials, network access, model downloads, or paid providers.

## Run it

From a source checkout:

```bash
python scripts/demo_adaptive_flock.py
```

For a stable machine-readable report:

```bash
python scripts/demo_adaptive_flock.py --json
```

The default mode is `shadow`. It computes and explains each route, but every
decision is marked non-actionable.

## Production-truth reconciliation (S11 / PR #311)

This demo is a **contract-level exercise**. It does not change, and never
claims, production routing authority:

- In the shipped v0.6 runtime, learned routing is **inert by design**:
  neither `server.py` nor `cli.py` wires an activation evaluator, so every
  real decision falls back to the deterministic static path with
  `durable_grant_required`. The `constrained` / `adaptive` modes shown here
  describe the routing contracts only; they are not wired into production
  execution.
- The only v0.6 learned-authority class (AUTH-002) is an **exact,
  owner-activated, low-risk summarizer scope** bound to a qualification
  receipt that meets the existing thresholds. Nothing else may ever carry
  v0.6 learned authority.
- Qualification, shadow observation, and this demo grant **zero authority**.
  A live grant requires an explicit owner activation decision
  (`tests/test_v06_authority_class.py` pins the class guard).
- The JSON report carries a `production_truth` block that states this
  explicitly (`wired_activation_evaluator: false`, `live_grant: false`,
  `deterministic_fallback_reason: durable_grant_required`), so a rendered
  report can never be mistaken for production authority.

## What the demo proves

The scenario presents four fictional targets to Kestrel's real routing
contracts and deterministic scorer:

| Target | Intended role | Relevant properties |
| --- | --- | --- |
| `planner-cloud` | Planner | Large context, structured output, reasoning |
| `coder-local` | Executor | Local, tool-capable, zero demo cost |
| `reviewer-independent` | Reviewer | Separate provider profile and model family |
| `tiny-local` | Rejection example | Too little context and no tool support |

The report shows:

1. the planner routed to `planner-cloud`;
2. the local-required implementation routed to `coder-local`;
3. `tiny-local` rejected with explicit reason codes;
4. the review routed to `reviewer-independent`; and
5. the executor rejected as its own reviewer by target and model-family
   independence rules.

Candidate scores are considered only after the hard eligibility filters pass.
A lower-cost or higher-affinity target cannot outrank a failed locality,
capability, context, risk, budget, health, or review-independence constraint.

## What the demo does not do

This is a routing demonstration, not a multi-model execution benchmark. It:

- makes no provider or model calls;
- does not execute any selected assignment;
- does not discover installed models automatically;
- does not modify the workspace;
- does not create commits, push branches, or merge changes; and
- does not claim that one target is universally the "best" model, and does
  not claim any production execution authority.

The target metadata is a deterministic fixture for explaining the routing
contract. Production targets remain operator-configured.

## Modes

The modes are **contract-level**: they describe how the routing contracts
score and mark decisions, not what the production runtime executes.

| Mode | Meaning |
| --- | --- |
| `off` | Preserve the static provider/model execution path. |
| `shadow` | Record a counterfactual route without making it actionable. |
| `constrained` | Mark eligible deterministic routes actionable within the configured policy (contract-level only). |
| `adaptive` | Route policy-eligible tasks under the same hard guardrails (contract-level only). |

You can inspect how the report changes without executing a provider:

```bash
python scripts/demo_adaptive_flock.py --mode constrained --json
python scripts/demo_adaptive_flock.py --mode adaptive --json
```

## Production authority is a separate, owner-activated decision

Adaptive Flock is disabled by default. The environment template exposes:

```dotenv
NEST_AGENT_ENABLE_ADAPTIVE_FLOCK=false
NEST_AGENT_ADAPTIVE_FLOCK_MODE=shadow
NEST_AGENT_ADAPTIVE_FLOCK_POLICY=balanced
```

These environment settings are a **global permit only** — they never create
authority by themselves. In v0.6, learned routing remains inert in the
shipped runtime: a live grant would require an exact, owner-activated,
low-risk summarizer scope (AUTH-002), a qualified receipt meeting existing
thresholds, and a wired activation evaluator. Drift, suspension, a kill
switch, or revocation immediately restores deterministic routing for new
decisions (AUTH-003).

To report a routing decision that looks wrong, use the **Adaptive Flock
routing trace** issue form. To propose support for a target configuration
without sharing credentials, use **Provider profile request**.
