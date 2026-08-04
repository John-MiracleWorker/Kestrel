import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  Approval,
  Run,
  TaskGraph,
  TraceEvent,
} from "../types";
import { ActiveMission } from "./ActiveMission";

const run: Run = {
  run_id: "run_1",
  project_id: "project_kestrel",
  status: "blocked",
  message: "Repair authentication",
  session_id: "session_1",
  workspace: "/tmp/kestrel",
  model: "mock",
  assistant_message: "I need approval for the exact patch.",
  tool_count: 2,
  context_chars: 1000,
  stop_reason: "approval_required",
  created_at: "2026-07-29T00:00:00Z",
  updated_at: "2026-07-29T00:01:00Z",
};

const approval: Approval = {
  approval_id: "approval_patch",
  run_id: run.run_id,
  tool_call_id: "call_patch",
  tool_name: "repair.apply_patch",
  arguments: { path: "src/auth.py" },
  risk: "high",
  principal: "owner",
  expires_at: "2036-07-29T00:10:00Z",
  capability_revision: 7,
  resource_digest: `sha256:${"a".repeat(64)}`,
  status: "pending",
  created_at: "2026-07-29T00:00:30Z",
  updated_at: "2026-07-29T00:00:30Z",
};

const taskGraph: TaskGraph = {
  tasks: [
    {
      task_id: "repair",
      title: "Repair authentication",
      goal: "Apply the bounded patch",
      profile: "worker",
      status: "blocked",
      approved: true,
      required_tools: ["repair.apply_patch"],
      acceptance_criteria: ["Targeted test passes"],
    },
  ],
  ready_tasks: [],
  approval_blocked_tasks: [],
  subagents: [
    {
      subagent_id: "worker_1",
      run_id: run.run_id,
      profile: "reviewer",
      goal: "Review the candidate",
      status: "waiting",
      task_id: "repair",
      result: "",
    },
  ],
};

const events: TraceEvent[] = [
  {
    id: 1,
    run_id: run.run_id,
    type: "approval.requested",
    payload: { tool_name: "repair.apply_patch" },
    created_at: "2026-07-29T00:00:30Z",
  },
];

describe("ActiveMission", () => {
  afterEach(cleanup);

  it("projects owner state, workers, conversation, approvals, and follow-up", () => {
    const onContinue = vi.fn(async () => undefined);
    render(
      <ActiveMission
        missionState="needs-owner"
        run={run}
        taskGraph={taskGraph}
        approvals={[approval]}
        events={events}
        onDecision={vi.fn()}
        onContinue={onContinue}
        onNewMission={vi.fn()}
        onOpenHistory={vi.fn()}
      >
        <p>Candidate comparison evidence</p>
      </ActiveMission>,
    );

    expect(
      screen.getByRole("heading", {
        name: "Mission needs your decision",
      }),
    ).toBeVisible();
    expect(
      screen.getAllByText("Repair authentication").length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("reviewer · waiting")).toBeVisible();
    expect(
      screen.getByRole("group", {
        name: "Approval for repair.apply_patch",
      }),
    ).toBeVisible();
    expect(
      screen.getByText("Candidate comparison evidence"),
    ).toBeVisible();

    fireEvent.change(
      screen.getByLabelText("Continue mission conversation"),
      { target: { value: "Keep the public API unchanged." } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Send follow-up" }),
    );
    expect(onContinue).toHaveBeenCalledWith(
      "Keep the public API unchanged.",
    );
  });

  // P2-2: while a decision for a packet is in flight, both of its
  // controls are disabled so rapid conflicting clicks cannot issue a
  // second contradicting request.
  it("serializes an in-flight approval decision", async () => {
    let resolveDecision: (() => void) | undefined;
    const onDecision = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveDecision = resolve;
        }),
    );
    render(
      <ActiveMission
        missionState="needs-owner"
        run={run}
        taskGraph={taskGraph}
        approvals={[approval]}
        events={events}
        onDecision={onDecision}
        onContinue={async () => undefined}
        onNewMission={vi.fn()}
        onOpenHistory={vi.fn()}
      />,
    );

    const approve = screen.getByRole("button", {
      name: "Approve repair.apply_patch",
    });
    const deny = screen.getByRole("button", {
      name: "Deny repair.apply_patch",
    });
    fireEvent.click(approve);
    await waitFor(() => expect(approve).toBeDisabled());
    expect(deny).toBeDisabled();
    fireEvent.click(deny);
    expect(onDecision).toHaveBeenCalledTimes(1);
    resolveDecision?.();
    await waitFor(() => expect(approve).toBeEnabled());
    expect(deny).toBeEnabled();
    expect(onDecision).toHaveBeenCalledTimes(1);
  });

  // P2-3: a failed follow-up surfaces a stable inline error and keeps
  // the owner draft intact for retry; no unhandled rejection escapes.
  it("shows an inline error and preserves the draft when the follow-up request fails", async () => {
    const onContinue = vi.fn(async () => {
      throw new Error("network offline");
    });
    render(
      <ActiveMission
        missionState="active"
        run={run}
        taskGraph={taskGraph}
        approvals={[]}
        events={events}
        onDecision={vi.fn()}
        onContinue={onContinue}
        onNewMission={vi.fn()}
        onOpenHistory={vi.fn()}
      />,
    );

    const draft = screen.getByLabelText(
      "Continue mission conversation",
    );
    fireEvent.change(draft, {
      target: { value: "Please retry the validation." },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Send follow-up" }),
    );

    expect(
      (await screen.findAllByText(/follow-up could not be sent/i))
        .length,
    ).toBeGreaterThan(0);
    expect(screen.getByText(/network offline/)).toBeVisible();
    expect(draft).toHaveValue("Please retry the validation.");
    // The send control recovers so the owner can retry.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Send follow-up" }),
      ).toBeEnabled(),
    );
  });

  it("routes follow-up auth failures through the auth-recovery path", async () => {
    const authError = new Error("Kestrel API token required.");
    authError.name = "ApiAuthError";
    const onContinue = vi.fn(async () => {
      throw authError;
    });
    const onAuthRequired = vi.fn();
    render(
      <ActiveMission
        missionState="active"
        run={run}
        taskGraph={taskGraph}
        approvals={[]}
        events={events}
        onDecision={vi.fn()}
        onContinue={onContinue}
        onNewMission={vi.fn()}
        onOpenHistory={vi.fn()}
        onAuthRequired={onAuthRequired}
      />,
    );

    fireEvent.change(
      screen.getByLabelText("Continue mission conversation"),
      { target: { value: "Continue after re-auth." } },
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Send follow-up" }),
    );

    await waitFor(() =>
      expect(onAuthRequired).toHaveBeenCalledTimes(1),
    );
    // The draft is preserved for retry after re-authentication.
    expect(
      screen.getByLabelText("Continue mission conversation"),
    ).toHaveValue("Continue after re-auth.");
  });
});
