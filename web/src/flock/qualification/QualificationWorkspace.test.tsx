import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QualificationProgress } from "./QualificationProgress";
import { QualificationResults } from "./QualificationResults";
import { ScopeResultCard } from "./ScopeResultCard";
import {
  QualificationWorkspace,
  type QualificationWorkspaceClient,
} from "./QualificationWorkspace";
import type {
  QualificationEvent,
  QualificationPreview,
  QualificationReceipt,
  QualificationRun,
  ScopeQualificationResult,
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

function scopeResult(
  state: ScopeQualificationResult["state"],
  reasons: string[],
  digest = digestA,
): ScopeQualificationResult {
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

const completedWithAbstentions: QualificationReceipt = {
  receipt_id: `rcpt_${"c".repeat(24)}`,
  run_id: runId,
  receipt_type: "run_terminal",
  payload_digest: digestG,
  created_at: "2026-08-01T12:00:02+00:00",
  payload: {
    schema: "kestrel.flock.qualification_receipt.v1",
    status: "completed",
    terminal_reason: "qualification_complete",
    qualifying: true,
    scopes: [
      scopeResult("qualified", []),
      scopeResult("abstained", ["insufficient_support"], digestB),
      scopeResult("abstained", ["confidence_below_threshold"], digestC),
    ],
  },
};

function typedRun(
  status: QualificationRun["status"],
  overrides: Partial<QualificationRun> = {},
): QualificationRun {
  const terminal =
    status === "completed" || status === "cancelled" || status === "failed";
  return {
    run_id: runId,
    status,
    revision: 2,
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
    terminal_reason: status === "completed" ? "qualification_complete" : null,
    ...overrides,
  };
}

function budgetOverrunEvent(): QualificationEvent {
  return {
    sequence: "7",
    event_type: "budget_projection_overrun",
    payload: {
      attempt_id: "att-1",
      reserve_micros: 100,
      actual_micros: 200,
      scope_digest: digestA,
    },
    created_at: "2026-08-01T12:00:01+00:00",
  };
}

function previewFixture(): QualificationPreview {
  return {
    schema: "kestrel.flock.qualification_preview.v1",
    created_at: "2026-08-01T12:00:00+00:00",
    scopes: [
      {
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
      },
    ],
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
      maximum_spend_micros: 50_000_000,
      maximum_spend_usd: "50.00",
      estimated_reserved_cost_range_micros: [1_000_000, 2_000_000],
    },
    preview_digest: digestG,
  };
}

function mockClient(): QualificationWorkspaceClient {
  return {
    preview: vi.fn(async () => previewFixture()),
    create: vi.fn(async () => typedRun("ready", { revision: 1 })),
    start: vi.fn(async () => typedRun("running", { revision: 2 })),
    pause: vi.fn(async () => typedRun("paused", { revision: 3 })),
    resume: vi.fn(async () => typedRun("running", { revision: 4 })),
    cancel: vi.fn(async () => typedRun("cancelled", { revision: 3 })),
    lowerCap: vi.fn(async () => typedRun("running", { revision: 3 })),
    getReceipt: vi.fn(async () => completedWithAbstentions),
  };
}

function pendingEvents() {
  return vi.fn(
    (_id: string, options: { signal: AbortSignal }) =>
      new Promise<void>((_, reject) => {
        options.signal.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      }),
  );
}

describe("QualificationResults", () => {
  afterEach(() => {
    cleanup();
  });

  it("does not call a completed run qualified", () => {
    render(<QualificationResults receipt={completedWithAbstentions} />);

    expect(screen.getByText("Evidence collection completed")).toBeVisible();
    expect(screen.getByText("2 scopes abstained")).toBeVisible();
    expect(screen.queryByText(/run qualified/i)).not.toBeInTheDocument();
  });
});

describe("ScopeResultCard", () => {
  afterEach(() => {
    cleanup();
  });

  it("says deterministic-only for deterministic-only scopes with verbatim reasons", () => {
    render(
      <ScopeResultCard
        result={scopeResult("deterministic_only", [
          "high_risk_deterministic_only",
        ])}
      />,
    );

    expect(screen.getByText("deterministic-only")).toBeVisible();
    expect(screen.getByText("high_risk_deterministic_only")).toBeVisible();
    expect(screen.queryByText(/^qualified$/i)).not.toBeInTheDocument();
  });

  it("redacts secrets in the evidence drill-down", () => {
    const result: ScopeQualificationResult = {
      ...scopeResult("qualified", []),
      router_state: {
        exploration_budget: 3,
        credentials: { api_key: "sk-live-secret-value" },
      },
    };
    render(<ScopeResultCard result={result} />);

    expect(screen.getByText(/Evidence \/ Advanced/)).toBeVisible();
    expect(screen.queryByText(/sk-live-secret-value/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/\[redacted\]/).length).toBeGreaterThan(0);
  });
});

describe("QualificationProgress", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows the immutable max and the lowerable stop cap separately, never raising", () => {
    const onLowerCap = vi.fn();
    render(<QualificationProgress run={typedRun("running")} onLowerCap={onLowerCap} />);

    expect(
      screen.getByText(/Maximum provider spend \(immutable\): \$50\.00/),
    ).toBeVisible();
    expect(screen.getByLabelText("Current stop cap")).toHaveValue("40.00");
    expect(
      screen.queryByRole("button", { name: /raise/i }),
    ).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Current stop cap"), {
      target: { value: "45.00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Lower stop cap" }));
    expect(onLowerCap).not.toHaveBeenCalled();
    expect(screen.getByText(/can only be lowered/i)).toBeVisible();

    fireEvent.change(screen.getByLabelText("Current stop cap"), {
      target: { value: "30.00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Lower stop cap" }));
    expect(onLowerCap).toHaveBeenCalledWith("30.00");
  });

  it("shows cost unresolved for unknown usage, never $0", () => {
    const run = typedRun("running", {
      spend: {
        actual_spend_micros: 1_250_000,
        actual_spend_usd: "1.25",
        unresolved_reserve_micros: 750_000,
        inflight_reserve_micros: 500_000,
      },
    });
    render(<QualificationProgress run={run} />);

    expect(screen.getByText(/cost unresolved/i)).toBeVisible();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("keeps completed evidence on budget exhaustion", () => {
    render(
      <QualificationProgress
        run={typedRun("running")}
        lastEvent={budgetOverrunEvent()}
      />,
    );

    expect(
      screen.getByText(/new attempts stopped; completed evidence retained\./i),
    ).toBeVisible();
  });
});

describe("QualificationWorkspace", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("opens on the draft without contacting qualification endpoints", async () => {
    const requests: string[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      requests.push(url);
      throw new Error(`unexpected_request:${url}`);
    });

    render(
      <QualificationWorkspace
        onError={() => undefined}
        onNotice={() => undefined}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Adaptive Flock qualification" }),
    ).toBeVisible();
    expect(screen.getByLabelText("Maximum provider spend")).toHaveValue(
      "50.00",
    );
    await waitFor(() => {
      expect(
        requests.some((path) => path.includes("/api/flock/")),
      ).toBe(false);
    });
    expect(
      screen.queryByRole("button", { name: /start qualification/i }),
    ).not.toBeInTheDocument();
  });

  it("requires an explicit preview review before start", async () => {
    const client = mockClient();
    const getRun = vi.fn(async () => typedRun("running"));

    render(
      <QualificationWorkspace
        client={client}
        onError={() => undefined}
        onNotice={() => undefined}
        runOptions={{
          getRun,
          readEvents: pendingEvents(),
          reconnectDelayMs: 0,
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Refresh preview" }));
    expect(await screen.findByText("Target matrix")).toBeVisible();

    const startButton = screen.getByRole("button", {
      name: "Create and start qualification",
    });
    expect(startButton).toBeDisabled();

    fireEvent.click(
      screen.getByRole("checkbox", { name: /i have reviewed/i }),
    );
    expect(startButton).toBeEnabled();
    fireEvent.click(startButton);

    await waitFor(() => {
      expect(client.create).toHaveBeenCalledTimes(1);
    });
    expect(vi.mocked(client.create).mock.calls[0]?.[0]).toMatchObject({
      maximumSpendUsd: "50.00",
    });
    await waitFor(() => {
      expect(client.start).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByLabelText("Current stop cap")).toHaveValue(
      "40.00",
    );
  });

  it("shows per-scope results once the run completes", async () => {
    const client = mockClient();
    const getRun = vi.fn(async () => typedRun("completed"));

    render(
      <QualificationWorkspace
        client={client}
        onError={() => undefined}
        onNotice={() => undefined}
        runOptions={{
          getRun,
          readEvents: pendingEvents(),
          reconnectDelayMs: 0,
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Refresh preview" }));
    fireEvent.click(
      await screen.findByRole("checkbox", { name: /i have reviewed/i }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Create and start qualification" }),
    );

    expect(
      await screen.findByText("Evidence collection completed"),
    ).toBeVisible();
    expect(screen.getByText("2 scopes abstained")).toBeVisible();
    expect(client.getReceipt).toHaveBeenCalledWith(runId);
  });
});
