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
- does not claim that one target is universally the “best” model.

The target metadata is a deterministic fixture for explaining the routing
contract. Production targets remain operator-configured.

## Modes

| Mode | Meaning |
| --- | --- |
| `off` | Preserve the static provider/model execution path. |
| `shadow` | Record a counterfactual route without making it actionable. |
| `constrained` | Make eligible deterministic routes actionable within the configured policy. |
| `adaptive` | Route policy-eligible tasks under the same hard guardrails. |

You can inspect how the report changes without executing a provider:

```bash
python scripts/demo_adaptive_flock.py --mode constrained --json
python scripts/demo_adaptive_flock.py --mode adaptive --json
```

## Safe runtime rollout

Adaptive Flock is disabled by default. The environment template exposes:

```dotenv
NEST_AGENT_ENABLE_ADAPTIVE_FLOCK=false
NEST_AGENT_ADAPTIVE_FLOCK_MODE=shadow
NEST_AGENT_ADAPTIVE_FLOCK_POLICY=balanced
```

Keep the runtime in `shadow` while validating configured targets and reviewing
recorded decisions. Move to an actionable mode only after the policy,
capabilities, locality, budgets, and reviewer-independence settings match your
operating boundary.

To report a routing decision that looks wrong, use the **Adaptive Flock routing
trace** issue form. To propose support for a target configuration without
sharing credentials, use **Provider profile request**.
