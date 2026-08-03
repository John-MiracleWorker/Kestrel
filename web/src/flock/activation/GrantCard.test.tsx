// @vitest-environment jsdom
/**
 * Grant card tests (Adaptive Flock plan, Task 20).
 *
 * Effectiveness comes only from the server evaluation; active-but-ineffective
 * grants show the server reason verbatim.  Revocation warns about new leases
 * vs. in-flight attempts.  Suspended/revoked grants offer Requalify — there is
 * no reactivate control anywhere.
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
import { GrantCard, type GrantCardClient } from "./GrantCard";
import type {
  ActivationGrant,
  ActivationTransition,
  GrantEvaluation,
} from "./types";
import type { FlockGrantStatus } from "../types";

const receiptId = `rcpt_${"c".repeat(24)}`;
const runId = `qual_${"a".repeat(24)}`;
const grantId = `grant_${"b".repeat(24)}`;
const scopeDigest = "a".repeat(64);
const digestOf = (char: string) => char.repeat(64);
const createdAt = "2026-08-01T00:00:00Z";

const grant: ActivationGrant = {
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
  scope_digest: scopeDigest,
  policy_id: "balanced",
  policy_revision: 1,
  qualification_receipt_id: receiptId,
  created_by: "owner:local-runtime:v1",
  created_at: createdAt,
};

function transition(
  transitionType: ActivationTransition["transition_type"],
  sequence: number,
): ActivationTransition {
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

function evaluationFixture(options: {
  status: FlockGrantStatus;
  effective: boolean;
  reasonCodes?: string[];
  bindingChanges?: Record<string, boolean>;
}): GrantEvaluation {
  return {
    grant_id: grantId,
    run_id: runId,
    scope_digest: scopeDigest,
    status: options.status,
    effective: options.effective,
    reason_codes: options.reasonCodes ?? [],
    receipt_authenticates: true,
    binding_changes: options.bindingChanges ?? {
      target_inventory: false,
      price: false,
      policy: false,
      learned: false,
      project_authority: false,
    },
    latest_transition: transition("activated", 1),
    transition_count: 1,
  };
}

function clientFor(
  evaluation: GrantEvaluation,
): GrantCardClient & { revoke: ReturnType<typeof vi.fn> } {
  return {
    evaluate: vi.fn(async () => evaluation),
    revoke: vi.fn(async () => transition("revoked", 2)),
  };
}

describe("GrantCard", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows active-but-ineffective with the server reason", async () => {
    const client = clientFor(
      evaluationFixture({
        status: "active",
        effective: false,
        reasonCodes: ["target_inventory_changed"],
        bindingChanges: {
          target_inventory: true,
          price: false,
          policy: false,
          learned: false,
          project_authority: false,
        },
      }),
    );

    render(<GrantCard grant={grant} client={client} />);

    expect(await screen.findByText(/Suspension pending/)).toBeVisible();
    expect(screen.getByText("Target inventory changed")).toBeVisible();
    // The server reason code is also preserved verbatim.
    expect(screen.getByText("target_inventory_changed")).toBeVisible();
  });

  it("warns that revocation affects new leases", async () => {
    const client = clientFor(
      evaluationFixture({ status: "active", effective: true }),
    );

    render(<GrantCard grant={grant} client={client} />);

    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(screen.getByText(/new route leases immediately/i)).toBeVisible();
    expect(
      screen.getByText(/in-flight attempt keeps its existing route lease/i),
    ).toBeVisible();
  });

  it("revokes with the expected transition revision after confirmation", async () => {
    const client = clientFor(
      evaluationFixture({ status: "active", effective: true }),
    );

    render(<GrantCard grant={grant} client={client} />);

    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm revocation" }));

    await waitFor(() => {
      expect(client.revoke).toHaveBeenCalledWith({
        grantId,
        expectedRevision: 1,
        reason: "owner_revocation",
      });
    });
  });

  it("offers requalify — never reactivate — for suspended and revoked grants", async () => {
    const suspended = clientFor(
      evaluationFixture({
        status: "suspended",
        effective: false,
        reasonCodes: ["grant_suspended", "target_inventory_changed"],
      }),
    );
    const { unmount } = render(<GrantCard grant={grant} client={suspended} />);

    const requalify = await screen.findByRole("link", { name: "Requalify" });
    expect(requalify).toHaveAttribute("href", "#/flock/qualification");
    expect(
      screen.queryByRole("button", { name: /reactivate/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /reactivate/i }),
    ).not.toBeInTheDocument();
    unmount();

    const revoked = clientFor(
      evaluationFixture({
        status: "revoked",
        effective: false,
        reasonCodes: ["grant_revoked"],
      }),
    );
    render(<GrantCard grant={grant} client={revoked} />);

    expect(
      await screen.findByRole("link", { name: "Requalify" }),
    ).toHaveAttribute("href", "#/flock/qualification");
    expect(
      screen.queryByRole("button", { name: "Revoke" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /reactivate/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/fresh qualification/i)).toBeVisible();
  });

  it("links the grant to its receipt and route decision evidence", async () => {
    const client = clientFor(
      evaluationFixture({ status: "active", effective: true }),
    );

    render(<GrantCard grant={grant} client={client} />);

    expect(await screen.findByText(receiptId)).toBeVisible();
    const routeLink = screen.getByRole("link", { name: /route decisions/i });
    expect(routeLink).toHaveAttribute("href", "#/flock/routing");
    expect(screen.getByText(/code_repair/)).toBeVisible();
    expect(screen.getAllByText(/learned-target/).length).toBeGreaterThan(0);
  });
});
