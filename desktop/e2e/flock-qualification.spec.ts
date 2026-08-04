/**
 * Adaptive Flock qualification -> activation -> routing -> revocation
 * installed-renderer e2e journey (Adaptive Flock plan, Task 22).
 *
 * IMPORTANT: every byte comes from deterministic in-page MOCK fixtures
 * (provider "mock" / model "mock"). This spec is NOT a production provider
 * qualification: it never touches a live model, network, or real provider
 * credentials, and the fixture throws loudly on any request outside its
 * table so a regression fails instead of silently reaching the ambient
 * network.
 *
 * Fixture layering: `demoFixtureInitScript()` (./fixtures.ts) is installed
 * first and answers the workbench boot GETs; the second init script defined
 * below captures that `window.fetch` and answers ONLY the flock/routing
 * surface (`/api/flock/*`, `/api/routing/preview`, and the routing inventory
 * GETs the Routing Center needs), delegating everything else to the captured
 * base fetch. The in-page state machine enforces the wire contract: exact
 * `expected_revision` semantics (409 on mismatch), terminal-receipt gating,
 * and receipt-digest/run-revision binding on activation.
 *
 * Journey (single owner flow, hash routes only):
 *  qualification draft $50.00 -> edit per-attempt ceiling -> preview ->
 *  review + create/start -> pause/resume -> completed receipt ->
 *  activation preview -> activate 1 scope -> learned route preview ->
 *  revoke -> static-fallback route preview.
 */
import { expect, test, type Page } from "@playwright/test";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { demoFixtureInitScript } from "./fixtures";

const here = dirname(fileURLToPath(import.meta.url));
const indexPath = resolve(here, "../../web/dist/index.html");

test.skip(
  !existsSync(indexPath),
  "web/dist is not built; run `npm --prefix web run build` first",
);

/* ------------------------------------------------------------------------ */
/* Deterministic mock fixture constants (Adaptive Flock Task 22).           */
/* ------------------------------------------------------------------------ */

const FIXTURE_TIME = "2026-07-31T00:00:00Z";
const RUN_ID = `qual_${"a1".repeat(12)}`;
const RECEIPT_ID = `rcpt_${"c".repeat(24)}`;
const GRANT_ID = `grant_${"b".repeat(24)}`;
const SCOPE_DIGEST = "1".repeat(64);
const RECEIPT_DIGEST = "2".repeat(64);
const CORPUS_DIGEST = "3".repeat(64);
const TARGET_DIGEST = "4".repeat(64);
const PRICE_DIGEST = "5".repeat(64);
const POLICY_DIGEST = "6".repeat(64);
const LEARNED_DIGEST = "7".repeat(64);
const AUTHORITY_DIGEST = "8".repeat(64);
const THRESHOLDS_DIGEST = "9".repeat(64);
const BUILD_DIGEST = "a".repeat(64);
const PREVIEW_DIGEST = "b".repeat(64);
const STATIC_TARGET = "mock-static";
const LEARNED_TARGET = "mock-learned";
const PROVIDER_PROFILE_ID = "mock-provider";

function json(payload: unknown): string {
  return JSON.stringify(payload);
}

const scopePayload = Object.freeze({
  project_id: "project-1",
  task_family: "code_repair",
  risk: "low",
  capability_key: "generation",
  policy_id: "balanced",
  policy_revision: 1,
  target_ids: [STATIC_TARGET, LEARNED_TARGET],
  target_inventory_digest: TARGET_DIGEST,
  price_digest: PRICE_DIGEST,
  learned_config_digest: LEARNED_DIGEST,
  project_authority_digest: AUTHORITY_DIGEST,
});

const qualificationPreview = Object.freeze({
  schema: "kestrel.flock.qualification_preview.v1",
  created_at: FIXTURE_TIME,
  scopes: [scopePayload],
  excluded_scopes: {},
  target_snapshot_digest: TARGET_DIGEST,
  target_ids: [STATIC_TARGET, LEARNED_TARGET],
  excluded_targets: {},
  start_blockers: {},
  warnings: {},
  matrix_size: 2,
  estimated_reserved_cost_range: [1_000_000, 2_000_000],
  policy_digest: POLICY_DIGEST,
  corpus_digest: CORPUS_DIGEST,
  project_authority_digest: AUTHORITY_DIGEST,
  target_inventory_digest: TARGET_DIGEST,
  learned_config_digest: LEARNED_DIGEST,
  budget: {
    maximum_spend_micros: 50_000_000,
    maximum_spend_usd: "50.00",
    estimated_reserved_cost_range_micros: [1_000_000, 2_000_000],
  },
  preview_digest: PREVIEW_DIGEST,
});

const runBase = Object.freeze({
  run_id: RUN_ID,
  status: "running",
  revision: 2,
  owner_principal: "owner:local-runtime:v1",
  scope_digest: SCOPE_DIGEST,
  corpus_digest: CORPUS_DIGEST,
  target_digest: TARGET_DIGEST,
  price_digest: PRICE_DIGEST,
  policy_digest: POLICY_DIGEST,
  learned_digest: LEARNED_DIGEST,
  project_authority_digest: AUTHORITY_DIGEST,
  thresholds_digest: THRESHOLDS_DIGEST,
  build_digest: BUILD_DIGEST,
  caps: {
    max_spend_micros: 50_000_000,
    max_spend_usd: "50.00",
    effective_stop_cap_micros: 50_000_000,
    effective_stop_cap_usd: "50.00",
    attempt_ceiling_micros: 7_500_000,
    attempt_ceiling_usd: "7.50",
  },
  spend: {
    actual_spend_micros: 0,
    actual_spend_usd: "0.00",
    unresolved_reserve_micros: 0,
    inflight_reserve_micros: 0,
  },
  blockers: [],
  created_at: FIXTURE_TIME,
  updated_at: FIXTURE_TIME,
  started_at: FIXTURE_TIME,
  finished_at: null,
  terminal_reason: null,
});

const scopeResult = Object.freeze({
  scope_digest: SCOPE_DIGEST,
  state: "qualified",
  qualified: true,
  static_target_id: STATIC_TARGET,
  selected_target_id: LEARNED_TARGET,
  total_support: 12,
  selected_target_support: 9,
  confidence: 0.82,
  static_utility: 0.4,
  learned_utility: 0.55,
  utility_delta: 0.15,
  cost_coverage: 0.9,
  estimated_savings_usd: 0.42,
  estimated_regret_usd: null,
  guardrail_violations: 0,
  evaluated_target_ids: [STATIC_TARGET, LEARNED_TARGET],
  reasons: [],
  router_state: { replay_runs: 20, replay_successes: 20 },
  thresholds_digest: THRESHOLDS_DIGEST,
});

const terminalReceipt = Object.freeze({
  receipt_id: RECEIPT_ID,
  run_id: RUN_ID,
  receipt_type: "run_terminal",
  payload_digest: RECEIPT_DIGEST,
  payload: {
    schema: "kestrel.flock_qualification_terminal_receipt.v1",
    status: "completed",
    terminal_reason: "qualification_complete",
    qualifying: true,
    run: { run_id: RUN_ID },
    digests: {},
    caps: {},
    spend: {},
    attempts_terminal: 4,
    attempts_succeeded: 4,
    failure_summary: {},
    guardrail_violations: 0,
    attempts: [],
    scopes: [scopeResult],
    replay: { passed: true, unique_projection_digests: 1 },
    details: {},
  },
  created_at: FIXTURE_TIME,
});

const activationScopePreview = Object.freeze({
  scope_digest: SCOPE_DIGEST,
  project_id: "project-1",
  task_family: "code_repair",
  risk: "low",
  capabilities: ["generation"],
  static_target_id: STATIC_TARGET,
  selected_target_id: LEARNED_TARGET,
  alternative_target_ids: [],
  total_support: 12,
  selected_target_support: 9,
  confidence: 0.82,
  static_utility: 0.4,
  learned_utility: 0.55,
  utility_delta: 0.15,
  cost_coverage: 0.9,
  estimated_savings_usd: 0.42,
  guardrail_violations: 0,
  reasons: [],
  qualified: true,
});

const activationPreviewBase = Object.freeze({
  receipt_id: RECEIPT_ID,
  run_id: RUN_ID,
  run_revision: 0, // replaced in-page with the fixture's current run revision
  owner_principal: "owner:local-runtime:v1",
  receipt_digest: RECEIPT_DIGEST,
  scopes: [activationScopePreview],
  replay: { passed: true, unique_projection_digests: 1 },
  target_snapshot: { targets: [STATIC_TARGET, LEARNED_TARGET] },
  price_snapshot: { currency: "usd" },
  binding_digests: {
    target_inventory: TARGET_DIGEST,
    price: PRICE_DIGEST,
    policy: POLICY_DIGEST,
    learned: LEARNED_DIGEST,
    project_authority: AUTHORITY_DIGEST,
  },
  binding_changes: {
    target_inventory: false,
    price: false,
    policy: false,
    learned: false,
    project_authority: false,
  },
  authority_changed: false,
  suspension_conditions: [
    "target_inventory_changed",
    "receipt_authentication_failed",
  ],
  revocation_behavior:
    "New route leases immediately lose the learned route; an in-flight attempt keeps its existing lease.",
});

const grantPayload = Object.freeze({
  grant_id: GRANT_ID,
  run_id: RUN_ID,
  target_id: LEARNED_TARGET,
  scope: scopePayload,
  scope_digest: SCOPE_DIGEST,
  policy_id: "balanced",
  policy_revision: 1,
  qualification_receipt_id: RECEIPT_ID,
  created_by: "owner:local-runtime:v1",
  created_at: FIXTURE_TIME,
});

function routingTarget(targetId: string) {
  return {
    target_id: targetId,
    provider_profile_id: PROVIDER_PROFILE_ID,
    provider: "mock",
    model: "mock",
    enabled: true,
    locality: "local",
    trust_class: "standard",
    capability_tags: ["worker"],
    role_affinities: ["worker"],
    task_family_affinities: ["code_repair"],
    max_context_tokens: 32_000,
    supports_tools: true,
    supports_json: false,
    supports_vision: false,
    supports_reasoning: false,
    supports_streaming: true,
    quality_tier: 2,
    latency_tier: 2,
    operator_priority: 0,
    estimated_cost_usd: 0,
    input_cost_per_million_usd: null,
    output_cost_per_million_usd: null,
    health: "healthy",
    recent_failure_rate: 0,
    predicted_success: null,
    metadata: {},
    revision: 1,
    created_at: FIXTURE_TIME,
    updated_at: FIXTURE_TIME,
  };
}

const routingProviders = Object.freeze([
  {
    profile_id: PROVIDER_PROFILE_ID,
    display_name: "Fixture mock provider",
    adapter: "mock",
    base_url_configured: false,
    secret_configured: false,
    enabled: true,
    locality: "local",
    trust_class: "standard",
    max_concurrency: 1,
    metadata: {},
    revision: 1,
    created_at: FIXTURE_TIME,
    updated_at: FIXTURE_TIME,
  },
]);

const routingTargets = Object.freeze([
  routingTarget(STATIC_TARGET),
  routingTarget(LEARNED_TARGET),
]);

const routingPolicies = Object.freeze([
  {
    policy_id: "balanced",
    enabled: true,
    quality_weight: 1,
    affinity_weight: 1,
    health_weight: 1,
    context_weight: 1,
    locality_weight: 1,
    operator_weight: 1,
    cost_weight: 1,
    latency_weight: 1,
    failure_weight: 1,
    require_different_target_for_review: false,
    require_different_model_family_for_review: false,
    prefer_different_provider_for_review: false,
    minimum_quality_by_risk: {},
    revision: 1,
    created_at: FIXTURE_TIME,
    updated_at: FIXTURE_TIME,
  },
]);

/**
 * In-page flock/routing fixture layered over the Demo fixture. Handles only
 * the flock qualification/activation endpoints plus the Routing Center
 * inventory and route preview; every other request is delegated to the
 * previously installed Demo `window.fetch`. Mutations validate exact
 * expected-revision semantics and return 409 on mismatch so the journey
 * proves the GUI sends correct revisions.
 */
function flockQualificationFixtureInitScript(): string {
  return `
    (() => {
      const baseFetch = window.fetch;
      const preview = ${json(qualificationPreview)};
      const runBase = ${json(runBase)};
      const receipt = ${json(terminalReceipt)};
      const activationPreview = ${json(activationPreviewBase)};
      const grant = ${json(grantPayload)};
      const providers = ${json(routingProviders)};
      const targets = ${json(routingTargets)};
      const policies = ${json(routingPolicies)};
      const runId = ${json(RUN_ID)};
      const receiptId = ${json(RECEIPT_ID)};
      const grantId = ${json(GRANT_ID)};
      const scopeDigest = ${json(SCOPE_DIGEST)};
      const receiptDigest = ${json(RECEIPT_DIGEST)};
      const staticTarget = ${json(STATIC_TARGET)};
      const learnedTarget = ${json(LEARNED_TARGET)};
      const providerProfileId = ${json(PROVIDER_PROFILE_ID)};
      const fixtureTime = ${json(FIXTURE_TIME)};

      window.__kestrelE2eRequestedPaths = window.__kestrelE2eRequestedPaths || [];
      const record = (entry) => {
        window.__kestrelE2eRequestedPaths.push(entry);
      };
      const jsonResponse = (payload, status = 200) =>
        new Response(JSON.stringify(payload), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      const conflict = (detail) => jsonResponse({ detail }, 409);

      const run = { status: "none", revision: 0, resumed: false, readsAfterResume: 0 };
      const grantState = { status: "none", transitions: 0 };

      const buildRun = (status, revision) => {
        const terminal =
          status === "completed" || status === "cancelled" || status === "failed";
        return {
          ...runBase,
          status,
          revision,
          updated_at: fixtureTime,
          started_at: status === "draft" ? null : fixtureTime,
          finished_at: terminal ? fixtureTime : null,
          terminal_reason:
            status === "completed"
              ? "qualification_complete"
              : status === "cancelled"
                ? "owner_cancelled"
                : status === "failed"
                  ? "worker_error"
                  : null,
        };
      };
      const currentRun = () => buildRun(run.status, run.revision);

      const buildTransition = (transitionType, sequence) => ({
        transition_id: grantId + ":" + sequence,
        grant_id: grantId,
        sequence,
        transition_type: transitionType,
        reason: transitionType === "revoked" ? "owner_revocation" : "owner_activation",
        receipt_id: transitionType === "revoked" ? null : receiptId,
        created_at: fixtureTime,
      });

      const buildEvaluation = () => {
        const revoked = grantState.status === "revoked";
        return {
          grant_id: grantId,
          run_id: runId,
          scope_digest: scopeDigest,
          status: revoked ? "revoked" : "active",
          effective: !revoked,
          reason_codes: revoked ? ["grant_revoked"] : [],
          receipt_authenticates: true,
          binding_changes: {
            target_inventory: false,
            price: false,
            policy: false,
            learned: false,
            project_authority: false,
          },
          latest_transition: buildTransition(revoked ? "revoked" : "activated", revoked ? 2 : 1),
          transition_count: revoked ? 2 : 1,
        };
      };

      const buildRoutePreview = (taskId) => {
        const learned = grantState.status === "active";
        const fallbackCodes =
          grantState.status === "revoked"
            ? ["grant_revoked", "durable_grant_required"]
            : ["durable_grant_required"];
        return {
          schema: "kestrel.adaptive_flock.route_preview.v1",
          task: {
            task_id: taskId,
            run_id: "run_e2e_flock",
            title: "Fixture flock route task",
            status: "pending",
          },
          contract: {},
          decision: {
            mode: learned ? "adaptive" : "off",
            policy_id: "balanced",
            contract_digest: ${json(THRESHOLDS_DIGEST)},
            selected_target_id: learned ? learnedTarget : staticTarget,
            selected_provider_profile_id: providerProfileId,
            selected_provider: "mock",
            selected_model: "mock",
            selection_kind: learned ? "learned" : "static",
            score: learned ? 0.91 : 0.5,
            reason_codes: learned ? [] : fallbackCodes,
            actionable: learned,
            candidates: [
              {
                target_id: staticTarget,
                provider_profile_id: providerProfileId,
                provider: "mock",
                model: "mock",
                eligible: true,
                score: 0.5,
                reason_codes: learned ? ["static_fallback_available"] : ["static_fallback"],
                components: {},
              },
              {
                target_id: learnedTarget,
                provider_profile_id: providerProfileId,
                provider: "mock",
                model: "mock",
                eligible: learned,
                score: learned ? 0.91 : null,
                reason_codes: learned ? ["durable_grant_active"] : fallbackCodes,
                components: {},
              },
            ],
          },
        };
      };

      const lifecycle = (action, body) => {
        const exact =
          typeof body.expected_revision === "number" &&
          body.expected_revision === run.revision &&
          Object.keys(body).length === 1;
        const legal =
          (action === "start" && run.status === "draft") ||
          (action === "pause" && run.status === "running") ||
          (action === "resume" && run.status === "paused") ||
          (action === "cancel" &&
            (run.status === "draft" ||
              run.status === "ready" ||
              run.status === "running" ||
              run.status === "paused"));
        if (!exact || !legal) {
          return conflict("flock_qualification_revision_conflict");
        }
        run.revision += 1;
        if (action === "start") {
          run.status = "running";
        } else if (action === "pause") {
          run.status = "paused";
        } else if (action === "resume") {
          run.status = "running";
          run.resumed = true;
          run.readsAfterResume = 0;
        } else {
          run.status = "cancelled";
        }
        return jsonResponse(currentRun());
      };

      const handleFlock = (method, pathname, body) => {
        if (method === "POST" && pathname === "/api/flock/qualifications/preview") {
          if (body.maximum_spend_usd !== "50.00") {
            return jsonResponse({ detail: "flock_preview_cap_mismatch" }, 400);
          }
          return jsonResponse(preview);
        }
        if (method === "POST" && pathname === "/api/flock/qualifications") {
          const scopeOk =
            body.scope &&
            JSON.stringify(body.scope.target_ids) ===
              JSON.stringify([staticTarget, learnedTarget]);
          const exact =
            run.status === "none" &&
            body.maximum_spend_usd === "50.00" &&
            body.attempt_ceiling_usd === "7.50" &&
            scopeOk &&
            Array.isArray(body.corpus) &&
            body.corpus.length === 1;
          if (!exact) {
            return jsonResponse({ detail: "flock_qualification_create_invalid" }, 400);
          }
          run.status = "draft";
          run.revision = 1;
          return jsonResponse(currentRun(), 201);
        }
        if (method === "GET" && pathname === "/api/flock/qualifications") {
          return jsonResponse({ runs: run.status === "none" ? [] : [currentRun()] });
        }
        if (method === "POST" && pathname === "/api/flock/activations/preview") {
          const exact =
            body.receipt_id === receiptId &&
            JSON.stringify(body.scope_digests) === JSON.stringify([scopeDigest]);
          if (!exact || run.status !== "completed") {
            return jsonResponse({ detail: "flock_receipt_unknown" }, 404);
          }
          return jsonResponse({ ...activationPreview, run_revision: run.revision });
        }
        if (method === "POST" && pathname === "/api/flock/activations") {
          const exact =
            grantState.status === "none" &&
            body.receipt_id === receiptId &&
            JSON.stringify(body.scope_digests) === JSON.stringify([scopeDigest]) &&
            body.expected_receipt_digest === receiptDigest &&
            body.expected_run_revision === run.revision &&
            typeof body.bindings === "object" &&
            body.bindings !== null;
          if (!exact) {
            return conflict("flock_activation_binding_conflict");
          }
          grantState.status = "active";
          grantState.transitions = 1;
          return jsonResponse(
            {
              grants: [grant],
              transitions: [buildTransition("activated", 1)],
              superseded: [],
            },
            201,
          );
        }
        if (method === "GET" && pathname === "/api/flock/activations") {
          return jsonResponse({
            grants: grantState.status === "none" ? [] : [grant],
          });
        }
        const runMatch = pathname.match(
          /^\\/api\\/flock\\/qualifications\\/(qual_[0-9a-f]{24})(?:\\/(start|pause|resume|cancel|lower-cap|receipt|events))?$/,
        );
        if (runMatch) {
          if (runMatch[1] !== runId || run.status === "none") {
            return jsonResponse({ detail: "flock_run_unknown" }, 404);
          }
          const action = runMatch[2] ?? "";
          if (method === "GET" && action === "") {
            if (run.status === "running" && run.resumed) {
              run.readsAfterResume += 1;
              if (run.readsAfterResume >= 4) {
                run.status = "completed";
                run.revision += 1;
              }
            }
            return jsonResponse(currentRun());
          }
          if (method === "GET" && action === "events") {
            return jsonResponse({ detail: "stream unavailable in fixture" }, 503);
          }
          if (method === "GET" && action === "receipt") {
            if (run.status !== "completed") {
              return conflict("flock_run_not_terminal");
            }
            return jsonResponse(receipt);
          }
          if (
            method === "POST" &&
            (action === "start" || action === "pause" || action === "resume" || action === "cancel")
          ) {
            return lifecycle(action, body);
          }
          throw new Error("e2e_fixture_refused_mutation:" + method + " " + pathname);
        }
        const grantMatch = pathname.match(
          /^\\/api\\/flock\\/activations\\/(grant_[0-9a-f]{24})(?:\\/(evaluate|revoke))?$/,
        );
        if (grantMatch) {
          if (grantMatch[1] !== grantId || grantState.status === "none") {
            return jsonResponse({ detail: "flock_grant_unknown" }, 404);
          }
          const action = grantMatch[2] ?? "";
          if (method === "GET" && action === "evaluate") {
            return jsonResponse(buildEvaluation());
          }
          if (method === "POST" && action === "revoke") {
            const exact =
              grantState.status === "active" &&
              body.expected_revision === grantState.transitions &&
              typeof body.reason === "string" &&
              Object.keys(body).length === 2;
            if (!exact) {
              return conflict("flock_activation_revision_conflict");
            }
            grantState.status = "revoked";
            grantState.transitions = 2;
            return jsonResponse(buildTransition("revoked", 2));
          }
          throw new Error("e2e_fixture_refused_mutation:" + method + " " + pathname);
        }
        throw new Error("e2e_fixture_missing:" + method + " " + pathname);
      };

      window.fetch = async (input, init) => {
        const url = typeof input === "string" ? input : String(input.url ?? input);
        const parsed = new URL(url, window.location.href);
        const pathname = parsed.pathname;
        const method = init && init.method ? String(init.method).toUpperCase() : "GET";
        if (pathname.startsWith("/api/flock/")) {
          record(method + " " + pathname + parsed.search);
          let body = {};
          if (method !== "GET") {
            try {
              body = JSON.parse(String((init && init.body) || "{}"));
            } catch {
              body = {};
            }
          }
          return handleFlock(method, pathname, body);
        }
        if (method === "POST" && pathname === "/api/routing/preview") {
          record(method + " " + pathname + parsed.search);
          let body = {};
          try {
            body = JSON.parse(String((init && init.body) || "{}"));
          } catch {
            body = {};
          }
          if (typeof body.task_id !== "string" || body.task_id.trim() === "") {
            return jsonResponse({ detail: "flock_route_preview_invalid" }, 400);
          }
          return jsonResponse(buildRoutePreview(body.task_id));
        }
        if (method === "GET" && pathname === "/api/routing/providers") {
          record(method + " " + pathname + parsed.search);
          return jsonResponse(providers);
        }
        if (method === "GET" && pathname === "/api/routing/targets") {
          record(method + " " + pathname + parsed.search);
          return jsonResponse(targets);
        }
        if (method === "GET" && pathname === "/api/routing/policies") {
          record(method + " " + pathname + parsed.search);
          return jsonResponse(policies);
        }
        return baseFetch(input, init);
      };
    })();
  `;
}

async function openFlock(page: Page, hash: string): Promise<void> {
  await page.goto(hash);
  await page.waitForFunction(() => document.fonts.status === "loaded");
}

async function requestedFlockPaths(page: Page): Promise<string[]> {
  return page.evaluate(
    () =>
      (window as unknown as { __kestrelE2eRequestedPaths: string[] })
        .__kestrelE2eRequestedPaths.filter(
          (path) =>
            path.includes("/api/flock/") || path.includes("/api/routing/preview"),
        ),
  );
}

test.describe("Adaptive Flock mock qualification journey", () => {
  test("no flock qualification traffic exists before the owner acts", async ({
    page,
  }) => {
    await page.addInitScript(demoFixtureInitScript());
    await page.addInitScript(flockQualificationFixtureInitScript());
    await openFlock(page, "/#/flock/qualification");
    await expect(
      page.getByRole("heading", { name: "Adaptive Flock qualification" }),
    ).toBeVisible();
    // Give any erroneous background poller a chance to fire.
    await page.waitForTimeout(500);
    expect(await requestedFlockPaths(page)).toEqual([]);
  });

  test("draft $50 -> preview -> start -> pause/resume -> complete -> activate -> route -> revoke -> static fallback", async ({
    page,
  }) => {
    await page.addInitScript(demoFixtureInitScript());
    await page.addInitScript(flockQualificationFixtureInitScript());
    await openFlock(page, "/#/flock/qualification");
    await expect(
      page.getByRole("heading", { name: "Adaptive Flock qualification" }),
    ).toBeVisible();

    // 1-2. Draft defaults "Maximum provider spend" to 50.00; the owner
    // re-types the cap and edits the per-attempt ceiling to 7.50.
    const capField = page.getByLabel("Maximum provider spend");
    await expect(capField).toHaveValue("50.00");
    await capField.fill("50.00");
    const ceilingField = page.getByLabel("Per-attempt cost ceiling");
    await ceilingField.fill("7.50");
    await expect(ceilingField).toHaveValue("7.50");

    // 3. Preview: the exact target matrix and budget summary render.
    await page.getByRole("button", { name: "Refresh preview" }).click();
    await expect(
      page.getByRole("heading", { name: "Target matrix" }),
    ).toBeVisible();
    await expect(
      page.getByText(/2 scope\/target cells across 1 scope/),
    ).toBeVisible();
    await expect(page.getByText(STATIC_TARGET)).toBeVisible();
    await expect(page.getByText(LEARNED_TARGET)).toBeVisible();
    await expect(
      page.getByText(/Maximum provider spend: \$50\.00/),
    ).toBeVisible();
    await expect(page.getByText(/Per-attempt ceiling: \$7\.50/)).toBeVisible();

    // 4. Owner review confirmation, then create + start with the mock targets.
    await page
      .getByRole("checkbox", { name: /I have reviewed the preview/ })
      .check();
    const startButton = page.getByRole("button", {
      name: "Create and start qualification",
    });
    await expect(startButton).toBeEnabled();
    await startButton.click();
    await expect(page.locator(".banner.success")).toContainText(
      "Qualification started for 1 scope(s)",
    );

    const progress = page.locator(".qual-progress");
    await expect(progress).toBeVisible();
    await expect(progress.locator(".badge").first()).toContainText("running");
    await expect(progress.getByText(`Run ${RUN_ID}`)).toBeVisible();
    await expect(
      progress.getByText("Maximum provider spend (immutable): $50.00"),
    ).toBeVisible();
    await expect(
      progress.getByText("Per-attempt ceiling: $7.50"),
    ).toBeVisible();

    // 5. Pause -> paused; resume -> running (both revision-checked, 409 on
    // mismatch inside the fixture).
    await progress.getByRole("button", { name: "Pause" }).click();
    await expect(progress.locator(".badge").first()).toContainText("paused");
    await progress.getByRole("button", { name: "Resume" }).click();
    await expect(progress.locator(".badge").first()).toContainText("running");

    // 6. The fixture advances the run to completed after the post-resume
    // authoritative reads (the SSE endpoint answers 503, so the hook polls).
    const results = page.locator(".qual-results");
    await expect(
      results.getByText("Evidence collection completed"),
    ).toBeVisible({ timeout: 30_000 });
    await expect(results.getByText("1 scope qualified")).toBeVisible();
    await expect(
      results.getByText("Guardrail violations: 0"),
    ).toBeVisible();
    await expect(
      results.getByText(`Selected target: ${LEARNED_TARGET}`),
    ).toBeVisible();
    await expect(results.getByText(`Receipt ${RECEIPT_ID}`)).toBeVisible();

    // The mutation sequence happened in order, after the preview.
    const qualificationPaths = await requestedFlockPaths(page);
    const previewIndex = qualificationPaths.indexOf(
      "POST /api/flock/qualifications/preview",
    );
    const createIndex = qualificationPaths.indexOf(
      "POST /api/flock/qualifications",
    );
    const startIndex = qualificationPaths.indexOf(
      `POST /api/flock/qualifications/${RUN_ID}/start`,
    );
    expect(previewIndex).toBeGreaterThan(-1);
    expect(createIndex).toBeGreaterThan(previewIndex);
    expect(startIndex).toBeGreaterThan(createIndex);
    expect(
      qualificationPaths.indexOf(
        `POST /api/flock/qualifications/${RUN_ID}/pause`,
      ),
    ).toBeGreaterThan(startIndex);
    expect(
      qualificationPaths.indexOf(
        `POST /api/flock/qualifications/${RUN_ID}/resume`,
      ),
    ).toBeGreaterThan(startIndex);
    expect(qualificationPaths).toContain(
      `GET /api/flock/qualifications/${RUN_ID}/receipt`,
    );

    // 7. Activation preview from the terminal receipt + qualified scope.
    await openFlock(page, "/#/flock/activations");
    await expect(
      page.getByRole("heading", { name: "Flock activations" }),
    ).toBeVisible();
    await expect(page.getByText("No activation grants yet.")).toBeVisible();
    await page.getByLabel("Qualification receipt ID").fill(RECEIPT_ID);
    await page.getByLabel("Scope digests").fill(SCOPE_DIGEST);
    await page.getByRole("button", { name: "Preview activation" }).click();
    await expect(
      page.getByRole("heading", { name: "Activation packet" }),
    ).toBeVisible();

    // 8. Select the qualified scope, confirm, activate; the grant card shows
    // the server-side evaluation as the effectiveness authority.
    await page
      .getByRole("checkbox", { name: "Scope code_repair qualified" })
      .check();
    await page
      .getByRole("checkbox", { name: /I understand this activation grants/ })
      .check();
    await page
      .getByRole("button", { name: /Activate 1 scope/ })
      .click();
    await expect(page.locator(".banner.success")).toContainText(
      "1 grant activated.",
    );
    const grantCard = page.locator(".grant-card");
    await expect(grantCard).toHaveCount(1);
    await expect(
      grantCard.getByText(/Effective — the learned route may be leased/),
    ).toBeVisible();
    await expect(
      grantCard.locator(".badge", { hasText: "effective" }),
    ).toBeVisible();

    // 9. Route preview with the effective grant: the learned mock target is
    // selected.
    await openFlock(page, "/#/flock/routing");
    await expect(
      page.getByRole("heading", { name: "Route Preview" }),
    ).toBeVisible();
    await page.getByLabel("Task ID").fill("task-e2e-flock");
    await page.getByRole("button", { name: "Preview decision" }).click();
    const learnedDecision = page.locator(".run-detail");
    await expect(
      learnedDecision.getByRole("heading", {
        name: "Fixture flock route task",
      }),
    ).toBeVisible();
    await expect(
      learnedDecision.locator(".inline-meta").first(),
    ).toContainText(LEARNED_TARGET);
    await expect(
      learnedDecision.getByText("durable_grant_active"),
    ).toBeVisible();

    // 10. Revoke the grant (owner-confirmed); the card shows terminal
    // revoked state.
    await openFlock(page, "/#/flock/activations");
    const revokedCard = page.locator(".grant-card");
    await expect(
      revokedCard.getByText(/Effective — the learned route may be leased/),
    ).toBeVisible();
    await revokedCard.getByRole("button", { name: "Revoke" }).click();
    await revokedCard
      .getByRole("button", { name: "Confirm revocation" })
      .click();
    await expect(
      revokedCard.getByText(/Revoked — terminal/),
    ).toBeVisible();

    // 11. Route preview after revocation: the static target is selected and
    // the learned target is rejected with verbatim reason codes.
    await openFlock(page, "/#/flock/routing");
    await expect(
      page.getByRole("heading", { name: "Route Preview" }),
    ).toBeVisible();
    await page.getByLabel("Task ID").fill("task-e2e-flock");
    await page.getByRole("button", { name: "Preview decision" }).click();
    const staticDecision = page.locator(".run-detail");
    await expect(
      staticDecision.locator(".inline-meta").first(),
    ).toContainText(STATIC_TARGET);
    await expect(staticDecision.getByText(/grant_revoked/)).toBeVisible();
    await expect(
      staticDecision.getByText(/durable_grant_required/),
    ).toBeVisible();

    // The activation + revocation + route-preview mutations all happened.
    const flockPaths = await requestedFlockPaths(page);
    expect(flockPaths).toContain("POST /api/flock/activations/preview");
    expect(flockPaths).toContain("POST /api/flock/activations");
    expect(flockPaths).toContain(
      `GET /api/flock/activations/${GRANT_ID}/evaluate`,
    );
    expect(flockPaths).toContain(
      `POST /api/flock/activations/${GRANT_ID}/revoke`,
    );
    expect(
      flockPaths.filter((path) => path === "POST /api/routing/preview"),
    ).toHaveLength(2);
  });
});
