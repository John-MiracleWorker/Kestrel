# Service Ownership

This document defines the intended and current ownership boundaries for the main Kestrel services.

| Surface                                                                                    | Primary owner                  | What belongs there                                                             |
| ------------------------------------------------------------------------------------------ | ------------------------------ | ------------------------------------------------------------------------------ |
| Auth, sessions, channel ingress, delivery semantics, rate limits, and webhook verification | Gateway                        | Accept external traffic, normalize it, authenticate it, and route it onward.   |
| Planning, memory, approvals, task orchestration, model routing, and task state             | Brain                          | Decide what should happen, persist task state, and expose domain APIs.         |
| Sandboxed side effects and execution artifacts                                             | Hands                          | Run risky actions in explicit sandboxes and emit structured execution records. |
| Human-facing chat, task, and operator interfaces                                           | Web                            | Render state from Gateway and Brain without owning business rules.             |
| Cross-service contracts                                                                    | Shared proto and typed schemas | Keep service boundaries explicit and additive.                                 |

## Current exceptions to clean up

- The web adapter still owns web-specific token streaming transport, but it now builds requests from the shared ingress contract instead of assembling ad-hoc Brain payloads.
- The Gateway memory graph route still queries Postgres directly. That data belongs behind a Brain-owned API and is the first boundary cleanup target in the memory phase.
- Mobile support is not a separate first-class service boundary yet. Current support is limited to Gateway push-registration and sync helpers backed by Brain RPCs.

## Phase sequencing rule

- Gateway changes must stop at normalized ingress, auth context, channel routing, and response delivery.
- Brain changes must stop at reasoning, policy, persistence, task lifecycle, and model or memory decisions.
- Hands changes must stop at explicit execution, sandbox policy, artifacts, and audit events.
