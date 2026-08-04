// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createQualification,
  getQualification,
  getQualificationReceipt,
  listQualifications,
  lowerQualificationCap,
  previewQualification,
  qualificationActions,
  qualifiedScopeDigests,
  resumeQualification,
  startQualification,
  pauseQualification,
  cancelQualification,
  streamQualificationEvents,
} from "./api";
import type {
  CreateQualificationInput,
  PreviewQualificationInput,
  QualificationCorpusItemInput,
  QualificationEvent,
  QualificationRun,
  QualificationScopePayload,
} from "./types";

const runId = `qual_${"a".repeat(24)}`;
const digestA = "1".repeat(64);
const digestB = "2".repeat(64);
const digestC = "3".repeat(64);
const digestD = "4".repeat(64);
const digestE = "5".repeat(64);
const digestF = "6".repeat(64);
const digestG = "7".repeat(64);
const digestH = "8".repeat(64);
const digestI = "9".repeat(64);
const digestJ = "a".repeat(64);
const digestK = "b".repeat(64);
const receiptId = `rcpt_${"c".repeat(24)}`;

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

function microsOf(usd: string): number {
  const [whole, fraction = ""] = usd.split(".");
  return Number(whole) * 1_000_000 + Number((fraction + "000000").slice(0, 6));
}

const corpusItemInput: QualificationCorpusItemInput = {
  itemId: "case-1",
  taskFamily: "code_repair",
  risk: "low",
  capabilities: ["generation"],
  taskContractDigest: digestC,
  acceptancePlanDigest: digestD,
  evidenceKind: "synthetic",
};

const previewDraft: PreviewQualificationInput = {
  projectId: "project-1",
  taskFamilies: ["code_repair"],
  corpus: [corpusItemInput],
  policyId: "balanced",
  policyRevision: 1,
  maximumSpendUsd: "50.00",
  defaultPrivacyClass: "approved_cloud",
  projectAuthority: { tools: ["fs.read"] },
  learnedConfig: { router: "bandit" },
};

function scopePayload(): QualificationScopePayload {
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

function previewResponse() {
  return {
    schema: "kestrel.flock.qualification_preview.v1",
    created_at: "2026-08-01T12:00:00+00:00",
    scopes: [scopePayload()],
    excluded_scopes: {},
    target_snapshot_digest: digestB,
    target_ids: ["target-a", "target-b"],
    excluded_targets: {},
    start_blockers: {},
    warnings: {},
    matrix_size: 2,
    estimated_reserved_cost_range: [1_000_000, 2_000_000],
    policy_digest: digestC,
    corpus_digest: digestD,
    project_authority_digest: digestE,
    target_inventory_digest: digestB,
    learned_config_digest: digestF,
    budget: {
      maximum_spend_micros: 37_250_000,
      maximum_spend_usd: "37.25",
      estimated_reserved_cost_range_micros: [1_000_000, 2_000_000],
    },
    preview_digest: digestG,
  };
}

function runResponse(status = "running", revision = 2) {
  const terminal = ["cancelled", "failed", "completed"].includes(status);
  const terminalReason =
    status === "completed"
      ? "qualification_complete"
      : status === "cancelled"
        ? "owner_cancelled"
        : status === "failed"
          ? "worker_error"
          : null;
  return {
    run_id: runId,
    status,
    revision,
    owner_principal: "owner:local-runtime:v1",
    scope_digest: digestA,
    corpus_digest: digestD,
    target_digest: digestB,
    price_digest: digestH,
    policy_digest: digestC,
    learned_digest: digestF,
    project_authority_digest: digestE,
    thresholds_digest: digestI,
    build_digest: digestJ,
    caps: {
      max_spend_micros: 50_000_000,
      max_spend_usd: "50.00",
      effective_stop_cap_micros: 40_000_000,
      effective_stop_cap_usd: "40.00",
      attempt_ceiling_micros: 5_000_000,
      attempt_ceiling_usd: "5.00",
    },
    spend: {
      actual_spend_micros: 1_250_000,
      actual_spend_usd: "1.25",
      unresolved_reserve_micros: 0,
      inflight_reserve_micros: 500_000,
    },
    blockers: [],
    created_at: "2026-08-01T12:00:00+00:00",
    updated_at: "2026-08-01T12:00:01+00:00",
    started_at: "2026-08-01T12:00:01+00:00",
    finished_at: terminal ? "2026-08-01T12:00:02+00:00" : null,
    terminal_reason: terminalReason,
  };
}

function typedRun(
  status: QualificationRun["status"] = "running",
): QualificationRun {
  return { ...runResponse(status), status } as QualificationRun;
}

function runningRun({
  effectiveStop = "40.00",
}: { effectiveStop?: string } = {}): QualificationRun {
  const run = typedRun("running");
  return {
    ...run,
    caps: {
      ...run.caps,
      effective_stop_cap_usd: effectiveStop,
      effective_stop_cap_micros: microsOf(effectiveStop),
    },
  };
}

function scopeResult(
  state: "qualified" | "abstained" | "deterministic_only",
  reasons: string[],
  digest = digestA,
) {
  return {
    scope_digest: digest,
    state,
    qualified: state === "qualified",
    static_target_id: "target-a",
    selected_target_id: state === "qualified" ? "target-b" : null,
    total_support: 12,
    selected_target_support: state === "qualified" ? 9 : 0,
    confidence: 0.82,
    static_utility: 0.4,
    learned_utility: state === "qualified" ? 0.55 : null,
    utility_delta: 0.15,
    cost_coverage: 0.9,
    estimated_savings_usd: state === "qualified" ? 0.42 : null,
    estimated_regret_usd: null,
    guardrail_violations: 0,
    evaluated_target_ids: ["target-a", "target-b"],
    reasons,
    router_state: { exploration_budget: 0 },
    thresholds_digest: digestI,
  };
}

function receiptResponse(
  scopes: unknown[] = [scopeResult("qualified", [])],
  status = "completed",
) {
  return {
    receipt_id: receiptId,
    run_id: runId,
    receipt_type: "run_terminal",
    payload_digest: digestJ,
    payload: {
      schema: "kestrel.flock_qualification_terminal_receipt.v1",
      status,
      terminal_reason: "qualification_complete",
      qualifying:
        (scopes as { state?: string }[]).some(
          (scope) => scope.state === "qualified",
        ) && status === "completed",
      run: { run_id: runId },
      digests: {},
      caps: {},
      spend: {},
      attempts_terminal: 4,
      attempts_succeeded: 4,
      failure_summary: {},
      guardrail_violations: 0,
      attempts: [],
      scopes,
      replay: { passed: true },
      details: {},
    },
    created_at: "2026-08-01T12:00:02+00:00",
  };
}

const createInput: CreateQualificationInput = {
  scope: {
    projectId: "project-1",
    taskFamily: "code_repair",
    risk: "low",
    capabilityKey: "generation",
    policyId: "balanced",
    policyRevision: 1,
    targetIds: ["target-a", "target-b"],
    targetInventoryDigest: digestB,
    priceDigest: digestH,
    learnedConfigDigest: digestF,
    projectAuthorityDigest: digestE,
  },
  corpus: [corpusItemInput],
  targetSnapshot: { targets: ["target-a", "target-b"] },
  priceSnapshot: { currency: "usd" },
  policyPayload: { policy_id: "balanced" },
  learnedPayload: { router: "bandit" },
  projectAuthority: { tools: ["fs.read"] },
  maximumSpendUsd: "37.25",
  attemptCeilingUsd: "5.00",
};

function responseFor(path: string, method: string): unknown {
  if (path === "/api/flock/qualifications/preview") return previewResponse();
  if (path === "/api/flock/qualifications" && method === "POST") {
    return runResponse("draft", 1);
  }
  if (path === "/api/flock/qualifications") return { runs: [runResponse()] };
  if (path === `/api/flock/qualifications/${runId}/receipt`) {
    return receiptResponse();
  }
  if (path.endsWith("/lower-cap")) return runResponse("running", 3);
  if (path.endsWith("/start")) return runResponse("running", 2);
  if (path.endsWith("/pause")) return runResponse("paused", 3);
  if (path.endsWith("/resume")) return runResponse("running", 4);
  if (path.endsWith("/cancel")) return runResponse("cancelled", 5);
  if (path === `/api/flock/qualifications/${runId}`) return runResponse();
  return {};
}

function captureFetch(requests: CapturedRequest[]) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = typeof input === "string" ? input : input.toString();
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    requests.push({ path, method, headers: new Headers(init?.headers), body });
    const status =
      path === "/api/flock/qualifications" && method === "POST" ? 201 : 200;
    return jsonResponse(responseFor(path, method), status);
  });
}

describe("typed Flock qualification API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    sessionStorage.clear();
    localStorage.clear();
  });

  it("sends the owner-entered cap as decimal text", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));
    const lastJsonBody = () =>
      requests[requests.length - 1]?.body as Record<string, unknown>;

    await previewQualification({ ...previewDraft, maximumSpendUsd: "37.25" });
    expect(lastJsonBody().maximum_spend_usd).toBe("37.25");
    expect(typeof lastJsonBody().maximum_spend_usd).toBe("string");

    await createQualification({ ...createInput, maximumSpendUsd: "37.25" });
    expect(lastJsonBody().maximum_spend_usd).toBe("37.25");
    expect(typeof lastJsonBody().maximum_spend_usd).toBe("string");

    await lowerQualificationCap({
      runId,
      maximumSpendUsd: "12.50",
      expectedRevision: 2,
    });
    expect(lastJsonBody().maximum_spend_usd).toBe("12.50");
    expect(typeof lastJsonBody().maximum_spend_usd).toBe("string");
  });

  it("never offers a cap increase for a running run", () => {
    const actions = qualificationActions(
      runningRun({ effectiveStop: "40.00" }),
    );
    expect(actions).toContain("lower_cap");
    expect(actions).not.toContain("raise_cap");

    expect(qualificationActions(typedRun("paused"))).toContain("lower_cap");
    expect(qualificationActions(typedRun("paused"))).not.toContain(
      "raise_cap",
    );
    for (const status of ["cancelled", "failed", "completed"] as const) {
      expect(qualificationActions(typedRun(status))).toEqual([]);
    }
  });

  it("rejects non-text or malformed caps before any request", async () => {
    const fetchMock = captureFetch([]);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      previewQualification({ ...previewDraft, maximumSpendUsd: "37.2.5" }),
    ).rejects.toThrow("flock_qualification_request_invalid");
    await expect(
      previewQualification({
        ...previewDraft,
        maximumSpendUsd: 37.25 as unknown as string,
      }),
    ).rejects.toThrow("flock_qualification_request_invalid");
    await expect(
      lowerQualificationCap({
        runId,
        maximumSpendUsd: "12.5.0",
        expectedRevision: 2,
      }),
    ).rejects.toThrow("flock_qualification_request_invalid");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("maps the preview request to the exact owner contract", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const preview = await previewQualification(previewDraft);

    expect(requests).toHaveLength(1);
    expect(requests[0]?.path).toBe("/api/flock/qualifications/preview");
    expect(requests[0]?.method).toBe("POST");
    expect(requests[0]?.body).toEqual({
      project_id: "project-1",
      task_families: ["code_repair"],
      corpus: [
        {
          item_id: "case-1",
          task_family: "code_repair",
          risk: "low",
          capabilities: ["generation"],
          task_contract_digest: digestC,
          acceptance_plan_digest: digestD,
          evidence_kind: "synthetic",
          actionable: true,
          exclusion_reasons: [],
        },
      ],
      policy_id: "balanced",
      policy_revision: 1,
      maximum_spend_usd: "50.00",
      default_privacy_class: "approved_cloud",
      project_authority: { tools: ["fs.read"] },
      learned_config: { router: "bandit" },
    });
    expect(preview.schema).toBe("kestrel.flock.qualification_preview.v1");
    expect(preview.preview_digest).toBe(digestG);
    expect(preview.budget.maximum_spend_usd).toBe("37.25");
    expect(preview.scopes).toHaveLength(1);
  });

  it("sends expected revisions with lifecycle mutations", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const started = await startQualification({ runId, expectedRevision: 1 });
    const paused = await pauseQualification({ runId, expectedRevision: 2 });
    const resumed = await resumeQualification({ runId, expectedRevision: 3 });
    const cancelled = await cancelQualification({ runId, expectedRevision: 4 });

    expect(
      requests.map(({ path, method, body }) => ({ path, method, body })),
    ).toEqual([
      {
        path: `/api/flock/qualifications/${runId}/start`,
        method: "POST",
        body: { expected_revision: 1 },
      },
      {
        path: `/api/flock/qualifications/${runId}/pause`,
        method: "POST",
        body: { expected_revision: 2 },
      },
      {
        path: `/api/flock/qualifications/${runId}/resume`,
        method: "POST",
        body: { expected_revision: 3 },
      },
      {
        path: `/api/flock/qualifications/${runId}/cancel`,
        method: "POST",
        body: { expected_revision: 4 },
      },
    ]);
    expect(started.status).toBe("running");
    expect(paused.status).toBe("paused");
    expect(resumed.status).toBe("running");
    expect(cancelled.status).toBe("cancelled");
  });

  it("parses run payloads with caps preserved as decimal text", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const run = await getQualification(runId);
    const listed = await listQualifications();

    expect(run.run_id).toBe(runId);
    expect(run.revision).toBe(2);
    expect(run.caps.max_spend_usd).toBe("50.00");
    expect(run.caps.effective_stop_cap_usd).toBe("40.00");
    expect(run.spend.actual_spend_usd).toBe("1.25");
    expect(typeof run.caps.max_spend_usd).toBe("string");
    expect(listed).toHaveLength(1);
    expect(listed[0]?.run_id).toBe(runId);
  });

  it("reads qualification from each scope result, never the run status", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    const receipt = await getQualificationReceipt(runId);
    expect(qualifiedScopeDigests(receipt)).toEqual([digestA]);

    const abstained = receiptResponse(
      [
        scopeResult("abstained", ["sparse_evidence", "low_confidence"]),
        scopeResult(
          "deterministic_only",
          ["high_risk_deterministic_only"],
          digestK,
        ),
      ],
      "completed",
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(abstained)),
    );
    const completedWithAbstentions = await getQualificationReceipt(runId);
    expect(completedWithAbstentions.payload.status).toBe("completed");
    expect(qualifiedScopeDigests(completedWithAbstentions)).toEqual([]);
    expect(completedWithAbstentions.payload.scopes[0]?.reasons).toEqual([
      "sparse_evidence",
      "low_confidence",
    ]);
    expect(completedWithAbstentions.payload.scopes[1]?.reasons).toEqual([
      "high_risk_deterministic_only",
    ]);
    expect(completedWithAbstentions.payload.scopes[1]?.state).toBe(
      "deterministic_only",
    );
  });

  it("streams run events from the persisted cursor", async () => {
    const encoder = new TextEncoder();
    const created = "2026-08-01T12:00:03+00:00";
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(encoder.encode(": heartbeat\n\n"));
            controller.enqueue(
              encoder.encode(
                `id: 5\nevent: budget_projection_overrun\ndata: ${JSON.stringify({
                  sequence: 5,
                  event_type: "budget_projection_overrun",
                  payload: {
                    attempt_id: "att-1",
                    reserve_micros: 100,
                    actual_micros: 200,
                    scope_digest: digestA,
                  },
                  created_at: created,
                })}\n\n`,
              ),
            );
            controller.enqueue(
              encoder.encode(
                `id: 6\nevent: run_completed\ndata: ${JSON.stringify({
                  sequence: 6,
                  event_type: "run_completed",
                  payload: { terminal_reason: "qualification_complete" },
                  created_at: created,
                })}\n\n`,
              ),
            );
            controller.close();
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const events: QualificationEvent[] = [];

    await streamQualificationEvents(runId, {
      afterSequence: "4",
      signal: new AbortController().signal,
      onEvent: (next) => events.push(next),
    });

    expect(events.map((event) => event.event_type)).toEqual([
      "budget_projection_overrun",
      "run_completed",
    ]);
    expect(events[0]?.sequence).toBe("5");
    expect(events[1]?.payload).toEqual({
      terminal_reason: "qualification_complete",
    });
    const [path, init] = fetchMock.mock.calls[0] ?? [];
    const headers = new Headers(init?.headers);
    expect(path).toBe(`/api/flock/qualifications/${runId}/events`);
    expect(headers.get("accept")).toBe("text/event-stream");
    expect(headers.get("last-event-id")).toBe("4");
  });
});
