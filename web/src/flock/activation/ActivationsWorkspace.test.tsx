// @vitest-environment jsdom
/**
 * Activations workspace tests (Adaptive Flock plan, Task 20).
 *
 * The workspace previews the exact authority change, activates only selected
 * qualified scopes with explicit owner confirmation, and lists grants with
 * their server-side evaluation.  Activation stays separate from provider and
 * target enablement.
 */
import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ActivationsWorkspace,
  type ActivationsWorkspaceClient,
} from "./ActivationsWorkspace";
import type {
  ActivationGrant,
  ActivationPreview,
  ActivationResult,
  ActivationScopePreview,
  ActivationTransition,
  GrantEvaluation,
} from "./types";

const receiptId = `rcpt_${"c".repeat(24)}`;
const runId = `qual_${"a".repeat(24)}`;
const grantId = `grant_${"b".repeat(24)}`;
const receiptDigest = "9".repeat(64);
const scopeADigest = "a".repeat(64);
const scopeBDigest = "b".repeat(64);
const digestOf = (char: string) => char.repeat(64);
const createdAt = "2026-08-01T00:00:00Z";

function scopePreview(
  digest: string,
  taskFamily: string,
  qualified: boolean,
  reasons: string[] = [],
): ActivationScopePreview {
  return {
    scope_digest: digest,
    project_id: "project-1",
    task_family: taskFamily,
    risk: "low",
    capabilities: ["generation"],
    static_target_id: "static-target",
    selected_target_id: qualified ? "learned-target" : null,
    alternative_target_ids: ["alt-target"],
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

const previewFixture: ActivationPreview = {
  receipt_id: receiptId,
  run_id: runId,
  run_revision: 3,
  owner_principal: "owner:local-runtime:v1",
  receipt_digest: receiptDigest,
  scopes: [
    scopePreview(scopeADigest, "scope a", true),
    scopePreview(scopeBDigest, "scope b", false, ["sparse_evidence"]),
  ],
  replay: { passed: true, unique_projection_digests: 1 },
  target_snapshot: { targets: ["static-target", "learned-target"] },
  price_snapshot: { currency: "usd" },
  binding_digests: {
    target_inventory: digestOf("2"),
    price: digestOf("8"),
    policy: digestOf("3"),
    learned: digestOf("6"),
    project_authority: digestOf("5"),
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
    "revocation is append-only and terminal; a revoked grant never returns to active",
};

const grantFixture: ActivationGrant = {
  grant_id: grantId,
  run_id: runId,
  target_id: "learned-target",
  scope: {
    project_id: "project-1",
    task_family: "code_repair",
    risk: "low",
    capability_key: "generation",
    policy_id: "balanced",
    policy_revision: 1,
    target_ids: ["static-target", "learned-target"],
    target_inventory_digest: digestOf("2"),
    price_digest: digestOf("8"),
    learned_config_digest: digestOf("6"),
    project_authority_digest: digestOf("5"),
  },
  scope_digest: scopeADigest,
  policy_id: "balanced",
  policy_revision: 1,
  qualification_receipt_id: receiptId,
  created_by: "owner:local-runtime:v1",
  created_at: createdAt,
};

const transitionFixture: ActivationTransition = {
  transition_id: `${grantId}:1`,
  grant_id: grantId,
  sequence: 1,
  transition_type: "activated",
  reason: "owner_confirmed_activation",
  receipt_id: receiptId,
  created_at: createdAt,
};

const activationResult: ActivationResult = {
  grants: [grantFixture],
  transitions: [transitionFixture],
  superseded: [],
};

const evaluationFixture: GrantEvaluation = {
  grant_id: grantId,
  run_id: runId,
  scope_digest: scopeADigest,
  status: "active",
  effective: true,
  reason_codes: [],
  receipt_authenticates: true,
  binding_changes: {
    target_inventory: false,
    price: false,
    policy: false,
    learned: false,
    project_authority: false,
  },
  latest_transition: transitionFixture,
  transition_count: 1,
};

function clientFixture(): ActivationsWorkspaceClient & {
  preview: ReturnType<typeof vi.fn>;
  create: ReturnType<typeof vi.fn>;
  list: ReturnType<typeof vi.fn>;
  evaluate: ReturnType<typeof vi.fn>;
  revoke: ReturnType<typeof vi.fn>;
} {
  return {
    preview: vi.fn(async () => previewFixture),
    create: vi.fn(async () => activationResult),
    list: vi.fn(async () => [grantFixture]),
    evaluate: vi.fn(async () => evaluationFixture),
    revoke: vi.fn(async () => ({
      ...transitionFixture,
      transition_id: `${grantId}:2`,
      sequence: 2,
      transition_type: "revoked" as const,
      reason: "owner_revocation",
    })),
  };
}

describe("ActivationsWorkspace", () => {
  afterEach(() => {
    cleanup();
  });

  it("previews a receipt and activates only the selected qualified scope", async () => {
    const client = clientFixture();
    const notices: string[] = [];

    render(
      <ActivationsWorkspace
        client={client}
        onError={() => undefined}
        onNotice={(message) => notices.push(message)}
      />,
    );

    fireEvent.change(screen.getByLabelText("Qualification receipt ID"), {
      target: { value: receiptId },
    });
    fireEvent.change(screen.getByLabelText("Scope digests"), {
      target: { value: `${scopeADigest}, ${scopeBDigest}` },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview activation" }));

    await waitFor(() => {
      expect(client.preview).toHaveBeenCalledWith({
        receiptId,
        scopeDigests: [scopeADigest, scopeBDigest],
      });
    });
    const scopeCheckbox = await screen.findByRole("checkbox", {
      name: /scope a/i,
    });
    expect(
      screen.getByRole("checkbox", { name: /scope b abstained/i }),
    ).toBeDisabled();

    fireEvent.click(scopeCheckbox);
    fireEvent.click(screen.getByRole("checkbox", { name: /I understand/i }));
    fireEvent.click(screen.getByRole("button", { name: "Activate 1 scope" }));

    await waitFor(() => {
      expect(client.create).toHaveBeenCalledWith(
        expect.objectContaining({
          receiptId,
          scopeDigests: [scopeADigest],
          expectedReceiptDigest: receiptDigest,
          expectedRunRevision: 3,
        }),
      );
    });
    expect(notices.some((message) => /1 grant activated/i.test(message))).toBe(
      true,
    );
  });

  it("keeps activation separate from provider and target enablement", () => {
    const client = clientFixture();

    render(
      <ActivationsWorkspace
        client={client}
        onError={() => undefined}
        onNotice={() => undefined}
      />,
    );

    expect(
      screen.getByText(/never enables a provider or model target/i),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: /routing inventory/i }),
    ).toHaveAttribute("href", "#/flock/routing");
  });

  it("lists grants with their server-side evaluation", async () => {
    const client = clientFixture();

    render(
      <ActivationsWorkspace
        client={client}
        onError={() => undefined}
        onNotice={() => undefined}
      />,
    );

    await waitFor(() => {
      expect(client.list).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(client.evaluate).toHaveBeenCalledWith(grantId);
    });
    expect(await screen.findByText("effective")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /reactivate/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /reactivate/i }),
    ).not.toBeInTheDocument();
  });
});
