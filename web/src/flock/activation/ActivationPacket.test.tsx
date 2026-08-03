// @vitest-environment jsdom
/**
 * Activation packet tests (Adaptive Flock plan, Task 20).
 *
 * Only explicitly selected, qualified scopes may be activated; abstained and
 * deterministic-only scopes are disabled and never sent.  Activation always
 * requires the explicit owner confirmation and binds the exact receipt digest
 * and run revision.
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
import { ActivationPacket } from "./ActivationPacket";
import type { ActivationPreview, ActivationScopePreview } from "./types";

const receiptId = `rcpt_${"c".repeat(24)}`;
const runId = `qual_${"a".repeat(24)}`;
const grantId = `grant_${"b".repeat(24)}`;
const receiptDigest = "9".repeat(64);
const scopeADigest = "a".repeat(64);
const scopeBDigest = "b".repeat(64);
const scopeCDigest = "c".repeat(64);
const digestOf = (char: string) => char.repeat(64);
const createdAt = "2026-08-01T00:00:00Z";

type CapturedRequest = Readonly<{
  path: string;
  method: string;
  body: Record<string, unknown> | null;
}>;

function captureFetch(requests: CapturedRequest[]): typeof fetch {
  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const path =
      typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    const method = String(init?.method ?? "GET").toUpperCase();
    const body =
      typeof init?.body === "string"
        ? (JSON.parse(init.body) as Record<string, unknown>)
        : null;
    requests.push({ path, method, body });
    if (path === "/api/flock/activations" && method === "POST") {
      return new Response(
        JSON.stringify({
          grants: [grantPayload()],
          transitions: [transitionPayload("activated", 1)],
          superseded: [],
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      );
    }
    throw new Error(`unhandled_request:${method}:${path}`);
  };
}

function lastJsonBody(requests: readonly CapturedRequest[]): Record<string, unknown> {
  const posts = requests.filter((request) => request.method === "POST");
  const last = posts[posts.length - 1];
  if (last === undefined || last.body === null) {
    throw new Error("no POST request captured");
  }
  return last.body;
}

function scopePreview(
  digest: string,
  taskFamily: string,
  qualified: boolean,
  options: { risk?: ActivationScopePreview["risk"]; reasons?: string[] } = {},
): ActivationScopePreview {
  return {
    scope_digest: digest,
    project_id: "project-1",
    task_family: taskFamily,
    risk: options.risk ?? "low",
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
    reasons: options.reasons ?? [],
    qualified,
  };
}

function previewFixture(scopes: ActivationScopePreview[]): ActivationPreview {
  return {
    receipt_id: receiptId,
    run_id: runId,
    run_revision: 3,
    owner_principal: "owner:local-runtime:v1",
    receipt_digest: receiptDigest,
    scopes,
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
    suspension_conditions: ["target_inventory_changed", "receipt_authentication_failed"],
    revocation_behavior:
      "revocation is append-only and terminal; a revoked grant never returns to active",
  };
}

function grantPayload() {
  return {
    grant_id: grantId,
    run_id: runId,
    target_id: "learned-target",
    scope: {
      project_id: "project-1",
      task_family: "scope a",
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
}

function transitionPayload(transitionType: string, sequence: number) {
  return {
    transition_id: `${grantId}:${sequence}`,
    grant_id: grantId,
    sequence,
    transition_type: transitionType,
    reason: "owner_confirmed_activation",
    receipt_id: receiptId,
    created_at: createdAt,
  };
}

describe("ActivationPacket", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("activates only explicitly selected qualified scopes", async () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));
    const preview = previewFixture([
      scopePreview(scopeADigest, "scope a", true),
      scopePreview(scopeBDigest, "scope b", false, { reasons: ["sparse_evidence"] }),
      scopePreview(scopeCDigest, "scope c", false, {
        risk: "high",
        reasons: ["high_risk_deterministic_only"],
      }),
    ]);

    render(<ActivationPacket preview={preview} />);

    fireEvent.click(screen.getByRole("checkbox", { name: /scope a/i }));
    expect(
      screen.getByRole("checkbox", { name: /scope b abstained/i }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /I understand/i }));
    fireEvent.click(screen.getByRole("button", { name: "Activate 1 scope" }));

    await waitFor(() => {
      expect(lastJsonBody(requests).scope_digests).toEqual([scopeADigest]);
    });
    const body = lastJsonBody(requests);
    expect(requests.some((request) => request.path === "/api/flock/activations")).toBe(
      true,
    );
    expect(body.receipt_id).toBe(receiptId);
    expect(body.expected_receipt_digest).toBe(receiptDigest);
    expect(body.expected_run_revision).toBe(3);
    expect(body.bindings).toMatchObject({
      project_authority: {},
      target_snapshot: {},
      price_snapshot: {},
      policy_payload: {},
      learned_payload: {},
    });
    expect(
      await screen.findByText(/1 grant activated/i),
    ).toBeVisible();
  });

  it("disables abstained and deterministic-only scopes with verbatim reasons", () => {
    const preview = previewFixture([
      scopePreview(scopeADigest, "scope a", true),
      scopePreview(scopeBDigest, "scope b", false, { reasons: ["sparse_evidence"] }),
      scopePreview(scopeCDigest, "scope c", false, {
        risk: "high",
        reasons: ["high_risk_deterministic_only"],
      }),
    ]);

    render(<ActivationPacket preview={preview} />);

    expect(
      screen.getByRole("checkbox", { name: /scope b abstained/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole("checkbox", { name: /scope c deterministic-only/i }),
    ).toBeDisabled();
    expect(screen.getByText("sparse_evidence")).toBeVisible();
    expect(screen.getByText("high_risk_deterministic_only")).toBeVisible();
    expect(screen.getByRole("checkbox", { name: /scope a/i })).toBeEnabled();
  });

  it("shows the exact authority change, suspension conditions, and revocation behavior", () => {
    const preview = previewFixture([scopePreview(scopeADigest, "scope a", true)]);

    render(<ActivationPacket preview={preview} />);

    expect(screen.getByText(/exact authority change/i)).toBeVisible();
    expect(screen.getByText(/static-target/)).toBeVisible();
    expect(screen.getByText(/learned-target/)).toBeVisible();
    expect(screen.getByText("target_inventory_changed")).toBeVisible();
    expect(screen.getByText("receipt_authentication_failed")).toBeVisible();
    expect(
      screen.getByText(/revocation is append-only and terminal/i),
    ).toBeVisible();
    expect(screen.getByText(receiptDigest)).toBeVisible();
  });

  it("requires explicit owner confirmation before activating", () => {
    const requests: CapturedRequest[] = [];
    vi.stubGlobal("fetch", captureFetch(requests));
    const preview = previewFixture([scopePreview(scopeADigest, "scope a", true)]);

    render(<ActivationPacket preview={preview} />);

    expect(
      screen.getByRole("button", { name: "Activate 0 scopes" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /scope a/i }));
    expect(
      screen.getByRole("button", { name: "Activate 1 scope" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /I understand/i }));
    expect(
      screen.getByRole("button", { name: "Activate 1 scope" }),
    ).toBeEnabled();
    expect(requests).toHaveLength(0);
  });

  it("warns when the preview reports changed bindings", () => {
    const preview = {
      ...previewFixture([scopePreview(scopeADigest, "scope a", true)]),
      binding_changes: {
        target_inventory: true,
        price: false,
        policy: false,
        learned: false,
        project_authority: false,
      },
      authority_changed: true,
    };

    render(<ActivationPacket preview={preview} />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/bindings changed since qualification/i);
    expect(alert).toHaveTextContent("target_inventory");
  });
});
