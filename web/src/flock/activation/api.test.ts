// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createActivation,
  evaluateActivation,
  isGrantEffective,
  listActivations,
  previewActivation,
  revokeActivation,
  selectableScopeDigests,
} from "./api";

const runId = `qual_${"a".repeat(24)}`;
const grantId = `grant_${"b".repeat(24)}`;
const receiptId = `rcpt_${"c".repeat(24)}`;
const digestA = "1".repeat(64);
const digestB = "2".repeat(64);
const digestC = "3".repeat(64);
const digestE = "5".repeat(64);
const digestF = "6".repeat(64);
const digestH = "8".repeat(64);
const digestK = "b".repeat(64);

type CapturedRequest = {
  path: string;
  method: string;
  headers: Headers;
  body: unknown;
};

function jsonResponse(payload: unknown = {}, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function scopePayload() {
  return {
    project_id: "project-1",
    task_family: "code_repair",
    risk: "low",
    capability_key: "generation",
    policy_id: "balanced",
    policy_revision: 1,
    target_ids: ["target-a", "target-b"],
    target_inventory_digest: digestB,
    price_digest: digestH,
    learned_config_digest: digestF,
    project_authority_digest: digestE,
  };
}

function activationScope(
  digest: string,
  qualified: boolean,
  reasons: string[] = [],
) {
  return {
    scope_digest: digest,
    project_id: "project-1",
    task_family: "code_repair",
    risk: "low",
    capabilities: ["generation"],
    static_target_id: "target-a",
    selected_target_id: qualified ? "target-b" : null,
    alternative_target_ids: ["target-c"],
    total_support: 12,
    selected_target_support: qualified ? 9 : 0,
    confidence: 0.82,
    static_utility: 0.4,
    learned_utility: qualified ? 0.55 : null,
    utility_delta: 0.15,
    cost_coverage: 0.9,
    estimated_savings_usd: qualified ? 0.42 : null,
    guardrail_violations: 0,
    reasons,
    qualified,
  };
}

function previewResponse() {
  return {
    receipt_id: receiptId,
    run_id: runId,
    run_revision: 3,
    owner_principal: "owner:local-runtime:v1",
    receipt_digest: digestA,
    scopes: [
      activationScope(digestA, true),
      activationScope(digestK, false, ["sparse_evidence"]),
    ],
    replay: { passed: true, unique_projection_digests: 1 },
    target_snapshot: { targets: ["target-a", "target-b"] },
    price_snapshot: { currency: "usd" },
    binding_digests: {
      target_inventory: digestB,
      price: digestH,
      policy: digestC,
      learned: digestF,
      project_authority: digestE,
    },
    binding_changes: {
      target_inventory: false,
      price: false,
      policy: false,
      learned: false,
      project_authority: false,
    },
    authority_changed: false,
    suspension_conditions: ["target_inventory_changed"],
    revocation_behavior:
      "new route leases immediately lose the learned route",
  };
}

function transitionPayload(
  transitionType: string,
  sequence = 1,
  id = grantId,
) {
  return {
    transition_id: `${id}:${sequence}`,
    grant_id: id,
    sequence,
    transition_type: transitionType,
    reason: "owner_activation",
    receipt_id: receiptId,
    created_at: "2026-08-01T12:00:02+00:00",
  };
}

function grantPayload() {
  return {
    grant_id: grantId,
    run_id: runId,
    target_id: "target-b",
    scope: scopePayload(),
    scope_digest: digestA,
    policy_id: "balanced",
    policy_revision: 1,
    qualification_receipt_id: receiptId,
    created_by: "owner:local-runtime:v1",
    created_at: "2026-08-01T12:00:02+00:00",
  };
}

function evaluationResponse() {
  return {
    grant_id: grantId,
    run_id: runId,
    scope_digest: digestA,
    status: "active",
    effective: false,
    reason_codes: ["target_inventory_changed"],
    receipt_authenticates: true,
    binding_changes: {
      target_inventory: true,
      price: false,
      policy: false,
      learned: false,
      project_authority: false,
    },
    latest_transition: transitionPayload("activated"),
    transition_count: 1,
  };
}

function responseFor(path: string, method: string): unknown {
  if (path === "/api/flock/activations/preview") return previewResponse();
  if (path === "/api/flock/activations" && method === "POST") {
    return {
      grants: [grantPayload()],
      transitions: [transitionPayload("activated")],
      superseded: [],
    };
  }
  if (path.startsWith("/api/flock/activations?")) {
    return { grants: [grantPayload()] };
  }
  if (path === "/api/flock/activations") return { grants: [grantPayload()] };
  if (path === `/api/flock/activations/${grantId}/evaluate`) {
    return evaluationResponse();
  }
  if (path === `/api/flock/activations/${grantId}/revoke`) {
    return transitionPayload("revoked", 2);
  }
  return {};
}

function captureFetch(requests: CapturedRequest[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input.toString();
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    requests.push({ path, method, headers: new Headers(init?.headers), body });
    const status =
      path === "/api/flock/activations" && method === "POST" ? 201 : 200;
    return jsonResponse(responseFor(path, method), status);
  });
}

describe("typed Flock activation API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    sessionStorage.clear();
    localStorage.clear();
  });

  it("previews only the explicitly selected scope digests", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const preview = await previewActivation({
      receiptId,
      scopeDigests: [digestA, digestK],
    });

    expect(requests[0]?.path).toBe("/api/flock/activations/preview");
    expect(requests[0]?.method).toBe("POST");
    expect(requests[0]?.body).toEqual({
      receipt_id: receiptId,
      scope_digests: [digestA, digestK],
    });
    expect(preview.receipt_digest).toBe(digestA);
    expect(selectableScopeDigests(preview)).toEqual([digestA]);
    expect(preview.scopes[1]?.qualified).toBe(false);
    expect(preview.scopes[1]?.reasons).toEqual(["sparse_evidence"]);
    expect(preview.suspension_conditions).toEqual([
      "target_inventory_changed",
    ]);
  });

  it("binds activation to the exact receipt digest and run revision", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const result = await createActivation({
      receiptId,
      scopeDigests: [digestA],
      expectedReceiptDigest: digestA,
      expectedRunRevision: 3,
      bindings: {
        projectAuthority: { tools: ["fs.read"] },
        targetSnapshot: { targets: ["target-a", "target-b"] },
        priceSnapshot: { currency: "usd" },
        policyPayload: { policy_id: "balanced" },
        learnedPayload: { router: "bandit" },
      },
    });

    expect(requests[0]?.path).toBe("/api/flock/activations");
    expect(requests[0]?.body).toEqual({
      receipt_id: receiptId,
      scope_digests: [digestA],
      expected_receipt_digest: digestA,
      expected_run_revision: 3,
      bindings: {
        project_authority: { tools: ["fs.read"] },
        target_snapshot: { targets: ["target-a", "target-b"] },
        price_snapshot: { currency: "usd" },
        policy_payload: { policy_id: "balanced" },
        learned_payload: { router: "bandit" },
      },
    });
    expect(result.grants).toHaveLength(1);
    expect(result.grants[0]?.grant_id).toBe(grantId);
    expect(result.grants[0]?.scope_digest).toBe(digestA);
    expect(result.transitions[0]?.transition_type).toBe("activated");
    expect(result.superseded).toEqual([]);
  });

  it("never infers effective from an active grant status", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const evaluation = await evaluateActivation(grantId);

    expect(requests[0]?.path).toBe(
      `/api/flock/activations/${grantId}/evaluate`,
    );
    expect(evaluation.status).toBe("active");
    expect(evaluation.effective).toBe(false);
    expect(isGrantEffective(evaluation)).toBe(false);
    expect(evaluation.reason_codes).toEqual(["target_inventory_changed"]);
    expect(evaluation.binding_changes).toEqual({
      target_inventory: true,
      price: false,
      policy: false,
      learned: false,
      project_authority: false,
    });
    expect(evaluation.latest_transition?.transition_type).toBe("activated");
  });

  it("lists grants scoped to a receipt", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const grants = await listActivations({ receiptId });

    expect(requests[0]?.path).toBe(
      `/api/flock/activations?receipt_id=${receiptId}`,
    );
    expect(grants).toHaveLength(1);
    expect(grants[0]?.qualification_receipt_id).toBe(receiptId);
    expect(grants[0]?.scope.target_ids).toEqual(["target-a", "target-b"]);
  });

  it("revokes with the expected revision and reason", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const transition = await revokeActivation({
      grantId,
      expectedRevision: 1,
      reason: "owner_revocation",
    });

    expect(requests[0]?.path).toBe(
      `/api/flock/activations/${grantId}/revoke`,
    );
    expect(requests[0]?.body).toEqual({
      expected_revision: 1,
      reason: "owner_revocation",
    });
    expect(transition.transition_type).toBe("revoked");
    expect(transition.sequence).toBe(2);
  });
});
