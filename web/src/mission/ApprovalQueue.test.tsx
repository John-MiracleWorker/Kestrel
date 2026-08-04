import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Approval } from "../types";
import { ApprovalQueue } from "./ApprovalQueue";

const FUTURE = "2036-07-29T00:10:00Z";
const PAST = "2020-07-29T00:10:00Z";

function approvalFixture(
  overrides: Partial<Approval> = {},
): Approval {
  return {
    approval_id: "approval_patch",
    run_id: "run_1",
    tool_call_id: "call_patch",
    tool_name: "repair.apply_patch",
    arguments: {
      path: "src/auth.py",
      patch_digest: "sha256:patch",
    },
    risk: "high",
    principal: "owner",
    expires_at: FUTURE,
    capability_revision: 7,
    resource_digest: `sha256:${"a".repeat(64)}`,
    status: "pending",
    created_at: "2026-07-29T00:00:00Z",
    updated_at: "2026-07-29T00:00:00Z",
    ...overrides,
  };
}

describe("ApprovalQueue", () => {
  afterEach(cleanup);

  it("shows exact call, arguments, capability, target digest, expiry, and consequence", () => {
    const approval = approvalFixture();
    const onDecision = vi.fn();
    render(
      <ApprovalQueue
        approvals={[approval]}
        onDecision={onDecision}
      />,
    );

    const card = screen.getByRole("group", {
      name: "Approval for repair.apply_patch",
    });
    expect(card).toHaveTextContent("repair.apply_patch");
    expect(card).toHaveTextContent("tool:repair.apply_patch");
    expect(card).toHaveTextContent("revision 7");
    expect(card).toHaveTextContent("src/auth.py");
    expect(card).toHaveTextContent("a".repeat(64));
    expect(card).toHaveTextContent("Jul");
    expect(card).toHaveTextContent(
      "invoke repair.apply_patch once",
    );
    // P1-4: the complete exact arguments are visible on the card
    // before any disclosure is opened.
    expect(card).toHaveTextContent('"path": "src/auth.py"');
    expect(card).toHaveTextContent('"patch_digest": "sha256:patch"');
    fireEvent.click(
      screen.getByRole("button", {
        name: "Approve repair.apply_patch",
      }),
    );
    expect(onDecision).toHaveBeenCalledWith(approval, true);
  });

  it("fails closed when immutable approval evidence is missing", () => {
    const incomplete = approvalFixture({
      expires_at: null,
      resource_digest: "",
    });
    render(
      <ApprovalQueue
        approvals={[incomplete]}
        onDecision={vi.fn()}
      />,
    );

    expect(
      screen.getByText("Approval evidence is incomplete"),
    ).toBeVisible();
    expect(
      screen.getByRole("button", {
        name: "Approve repair.apply_patch",
      }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", {
        name: "Deny repair.apply_patch",
      }),
    ).toBeEnabled();
  });

  // P1-1: the runtime emits canonical `sha256:<64 hex>` resource
  // digests (run_manager.tool_resource_digest). Approval must accept
  // exactly that API shape while remaining fail-closed for malformed
  // values.
  it("accepts the canonical API-shaped sha256:<64 hex> resource digest", () => {
    const apiShaped = approvalFixture({
      resource_digest: `sha256:${"a".repeat(64)}`,
    });
    render(
      <ApprovalQueue
        approvals={[apiShaped]}
        onDecision={vi.fn()}
      />,
    );

    expect(
      screen.queryByText("Approval evidence is incomplete"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Approve repair.apply_patch",
      }),
    ).toBeEnabled();
    expect(
      screen.getByText(`sha256:${"a".repeat(64)}`),
    ).toBeVisible();
  });

  it.each([
    "sha256:xyz",
    "sha256:",
    "sha256:" + "A".repeat(64),
    "md5:" + "a".repeat(32),
    " " + "a".repeat(64),
  ])(
    "rejects malformed resource digest %s",
    (digest) => {
      const malformed = approvalFixture({
        resource_digest: digest,
      });
      render(
        <ApprovalQueue
          approvals={[malformed]}
          onDecision={vi.fn()}
        />,
      );
      expect(
        screen.getByText("Approval evidence is incomplete"),
      ).toBeVisible();
      expect(
        screen.getByRole("button", {
          name: "Approve repair.apply_patch",
        }),
      ).toBeDisabled();
    },
  );

  // P1-2: an expired pending packet must be visibly expired and must
  // not be approvable; denial stays available as the safe exit.
  it("shows an expired approval as expired and disables approval", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-07-29T01:00:00Z"));
      const expired = approvalFixture({ expires_at: PAST });
      const onDecision = vi.fn();
      render(
        <ApprovalQueue
          approvals={[expired]}
          onDecision={onDecision}
        />,
      );

      expect(
        screen.getByText("Approval has expired"),
      ).toBeVisible();
      expect(screen.getByText(/^Expired /)).toBeVisible();
      const approve = screen.getByRole("button", {
        name: "Approve repair.apply_patch",
      });
      expect(approve).toBeDisabled();
      fireEvent.click(approve);
      expect(onDecision).not.toHaveBeenCalled();
      // Denial remains the safe exit path.
      fireEvent.click(
        screen.getByRole("button", {
          name: "Deny repair.apply_patch",
        }),
      );
      expect(onDecision).toHaveBeenCalledWith(expired, false);
    } finally {
      vi.useRealTimers();
    }
  });

  // P2-2: while a decision for one packet is in flight, both of its
  // controls are disabled so rapid conflicting clicks cannot issue a
  // second contradicting request; other packets stay actionable.
  it("serializes decisions for the pending packet only", () => {
    const first = approvalFixture();
    const second = approvalFixture({
      approval_id: "approval_env",
      tool_call_id: "call_env",
      tool_name: "file.write",
      arguments: { path: "config/app.env" },
    });
    render(
      <ApprovalQueue
        approvals={[first, second]}
        onDecision={vi.fn()}
        pendingApprovalId={first.approval_id}
      />,
    );

    expect(
      screen.getByRole("button", {
        name: "Approve repair.apply_patch",
      }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", {
        name: "Deny repair.apply_patch",
      }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Approve file.write" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Deny file.write" }),
    ).toBeEnabled();
  });

  it("awaits an async decision before re-enabling the packet controls", async () => {
    const approval = approvalFixture();
    let resolveDecision: (() => void) | undefined;
    const onDecision = vi.fn(
      (_approval: Approval, _approved: boolean) =>
        new Promise<void>((resolve) => {
          resolveDecision = resolve;
        }),
    );

    function Harness() {
      const [pendingId, setPendingId] = useState<
        string | null
      >(null);
      return (
        <ApprovalQueue
          approvals={[approval]}
          pendingApprovalId={pendingId}
          onDecision={async (target, approved) => {
            setPendingId(target.approval_id);
            try {
              await onDecision(target, approved);
            } finally {
              setPendingId(null);
            }
          }}
        />
      );
    }

    render(<Harness />);
    const approve = screen.getByRole("button", {
      name: "Approve repair.apply_patch",
    });
    fireEvent.click(approve);
    await waitFor(() => expect(approve).toBeDisabled());
    expect(onDecision).toHaveBeenCalledTimes(1);
    resolveDecision?.();
    await waitFor(() => expect(approve).toBeEnabled());
    expect(onDecision).toHaveBeenCalledTimes(1);
  });
});
