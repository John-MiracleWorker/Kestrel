import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QualificationDraft } from "./QualificationDraft";
import { TargetMatrix } from "./TargetMatrix";
import type {
  PreviewQualificationInput,
  QualificationPreview,
} from "./types";

const digestA = "1".repeat(64);
const digestB = "2".repeat(64);
const digestC = "3".repeat(64);
const digestD = "4".repeat(64);
const digestE = "5".repeat(64);
const digestF = "6".repeat(64);
const digestG = "7".repeat(64);
const digestH = "8".repeat(64);

const previewFixture: PreviewQualificationInput = {
  projectId: "project-1",
  taskFamilies: ["code_repair"],
  corpus: [
    {
      itemId: "case-1",
      taskFamily: "code_repair",
      risk: "low",
      capabilities: ["generation"],
      taskContractDigest: digestC,
      acceptancePlanDigest: digestD,
      evidenceKind: "synthetic",
    },
  ],
  policyId: "balanced",
  policyRevision: 1,
  defaultPrivacyClass: "approved_cloud",
  projectAuthority: { tools: ["fs.read"] },
  learnedConfig: { router: "bandit" },
};

function previewResponse(): QualificationPreview {
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

type CapturedRequest = Readonly<{
  path: string;
  method: string;
  body: Record<string, unknown> | null;
}>;

function captureFetch(requests: CapturedRequest[]): typeof fetch {
  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    requests.push({
      path: url,
      method: String(init?.method ?? "GET").toUpperCase(),
      body:
        typeof init?.body === "string"
          ? (JSON.parse(init.body) as Record<string, unknown>)
          : null,
    });
    return new Response(JSON.stringify(previewResponse()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
}

function allTargetPreview(): QualificationPreview {
  return {
    ...previewResponse(),
    target_ids: ["target-a", "target-b", "target-c"],
    excluded_targets: {
      "target-d": ["disabled_by_owner"],
      "target-e": ["privacy_class_blocked", "price_missing"],
    },
    excluded_scopes: {
      "code_repair+high": ["high_risk_deterministic_only"],
    },
    matrix_size: 3,
  };
}

describe("QualificationDraft", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("defaults to $50 and lets the owner change it before launch", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));
    const lastJsonBody = () => {
      const withBody = requests.filter((request) => request.body !== null);
      return withBody[withBody.length - 1]?.body ?? {};
    };

    render(<QualificationDraft fixture={previewFixture} />);

    const cap = screen.getByLabelText("Maximum provider spend");
    expect(cap).toHaveValue("50.00");
    fireEvent.change(cap, { target: { value: "35.00" } });
    fireEvent.click(screen.getByRole("button", { name: "Refresh preview" }));

    await waitFor(() => {
      expect(lastJsonBody().maximum_spend_usd).toBe("35.00");
    });
  });

  it("rejects a non-decimal cap before any preview request", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));

    render(<QualificationDraft fixture={previewFixture} />);

    const cap = screen.getByLabelText("Maximum provider spend");
    fireEvent.change(cap, { target: { value: "not-money" } });
    fireEvent.click(screen.getByRole("button", { name: "Refresh preview" }));

    expect(
      await screen.findByText(/enter the cap as decimal text/i),
    ).toBeVisible();
    expect(requests).toHaveLength(0);
  });

  it("displays each qualification threshold individually", () => {
    render(<QualificationDraft fixture={previewFixture} />);

    expect(screen.getByLabelText("Minimum examples per scope")).toHaveValue(
      "5",
    );
    expect(screen.getByLabelText("Minimum examples per target")).toHaveValue(
      "3",
    );
    expect(screen.getByLabelText("Confidence threshold")).toHaveValue("0.7");
    expect(screen.getByLabelText("Utility margin")).toHaveValue("0.08");
    expect(screen.getByLabelText("Cost coverage threshold")).toHaveValue(
      "0.8",
    );
    expect(screen.getByLabelText("Decay half-life (days)")).toHaveValue("30");
    expect(screen.getByLabelText("Maximum guardrail violations")).toHaveValue(
      "0",
    );
    expect(screen.getByLabelText("Replay runs")).toHaveValue("20");
    expect(
      screen.getByLabelText("Replay successes required"),
    ).toHaveValue("20");
  });
});

describe("TargetMatrix", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows every eligible target and every exclusion reason", () => {
    const preview = allTargetPreview();
    render(<TargetMatrix preview={preview} />);

    expect(screen.getAllByRole("row")).toHaveLength(
      preview.target_ids.length +
        Object.keys(preview.excluded_targets).length +
        1,
    );
    for (const targetId of preview.target_ids) {
      expect(screen.getByText(targetId)).toBeVisible();
    }
    expect(screen.getByText(/disabled_by_owner/)).toBeVisible();
    expect(screen.getByText(/privacy_class_blocked/)).toBeVisible();
    expect(screen.getByText(/price_missing/)).toBeVisible();
    expect(screen.getByText(/high_risk_deterministic_only/)).toBeVisible();
  });
});
