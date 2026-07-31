import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Channel, Routine, RoutineOccurrence, RoutineDelivery, RoutineStatus } from "../types";
import { AutomateWorkspace } from "./AutomateWorkspace";
import type { AutomateChannelsSlice } from "./ChannelsPanel";

function routineFixture(): Routine {
  return {
    routine_id: "morning-review",
    name: "Morning review",
    prompt: "Summarize the overnight work.",
    enabled: true,
    revision: 3,
    schedule_kind: "cron",
    start_at: "2026-05-17T13:00:00Z",
    interval_seconds: null,
    cron_expression: "0 9 * * 1-5",
    timezone: "America/Detroit",
    delivery: {
      channel_id: "telegram",
      conversation_id: "ops-chat",
      template: "{result}"
    },
    workspace: null,
    provider: null,
    model: null,
    autonomy_mode: "background",
    misfire_grace_seconds: 60,
    next_run_at: "2026-05-18T13:00:00Z",
    created_at: "2026-05-17T12:00:00Z",
    updated_at: "2026-05-17T12:00:00Z"
  };
}

function occurrenceFixture(): RoutineOccurrence {
  return {
    occurrence_id: "occ_1",
    routine_id: "morning-review",
    routine_revision: 3,
    run_id: "run_routine_1",
    status: "completed",
    trigger_kind: "scheduled",
    scheduled_for: "2026-05-17T13:00:00Z",
    request: {},
    result: {},
    requested_at: "2026-05-17T13:00:00Z",
    started_at: "2026-05-17T13:00:01Z",
    finished_at: "2026-05-17T13:00:04Z",
    created_at: "2026-05-17T13:00:00Z",
    updated_at: "2026-05-17T13:00:04Z",
    error: null,
    skip_reason: null
  };
}

function deliveryFixture(): RoutineDelivery {
  return {
    delivery_id: "delivery_1",
    routine_id: "morning-review",
    occurrence_id: "occ_1",
    run_id: "run_routine_1",
    status: "uncertain",
    attempt_count: 1,
    idempotency_key: "delivery-key-1",
    destination: {
      channel_id: "telegram",
      conversation_id: "ops-chat",
      template: "{result}"
    },
    destination_digest: "sha256:dest",
    receipt: {},
    error: "connector timeout before receipt",
    delivered_at: null,
    created_at: "2026-05-17T13:00:05Z",
    updated_at: "2026-05-17T13:00:06Z"
  };
}

function statusFixture(): RoutineStatus {
  return {
    enabled: true,
    loop: {
      running: true,
      tick_count: 3,
      last_result: null,
      last_error: null,
      tick_in_progress: false,
      current_tick_age_seconds: null,
      last_started_at: null,
      last_finished_at: null
    }
  };
}

function channelFixture(): Channel {
  return {
    id: "telegram",
    provider: "telegram",
    enabled: true,
    send_enabled: true,
    auto_reply: false,
    token_env: "TELEGRAM_BOT_TOKEN",
    webhook_url_env: null,
    settings: {},
    env_status: {}
  };
}

function stubRoutineFetch(overrides: { deliveries?: RoutineDelivery[] } = {}) {
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = typeof input === "string" ? input : input.toString();
      requests.push(path);
      const value =
        path === "/api/routines/status"
          ? statusFixture()
          : path === "/api/routines"
            ? [routineFixture()]
            : path === "/api/routines/morning-review/history?limit=50"
              ? [occurrenceFixture()]
              : path === "/api/routines/morning-review/deliveries?limit=50"
                ? (overrides.deliveries ?? [])
                : null;
      if (value === null) throw new Error(`unexpected_request:${path}`);
      return new Response(JSON.stringify(value), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    })
  );
  return requests;
}

function channelsSliceFixture(channel: Channel): AutomateChannelsSlice {
  return {
    channels: [channel],
    onEditChannel: () => undefined
  };
}

describe("AutomateWorkspace", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("owns routine status and definitions loading", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = typeof input === "string" ? input : input.toString();
        requests.push(path);
        const value =
          path === "/api/routines/status"
            ? { enabled: true, loop: null }
            : path === "/api/routines"
              ? []
              : null;
        if (value === null) throw new Error(`unexpected_request:${path}`);
        return new Response(JSON.stringify(value), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        });
      })
    );

    render(<AutomateWorkspace onAuthRequired={() => undefined} />);

    expect(
      await screen.findByRole("heading", { name: "Routine Workbench." })
    ).toBeVisible();
    expect(requests).toEqual([
      "/api/routines/status",
      "/api/routines"
    ]);
  });

  it("never claims exactly-once external delivery and shows connector receipts", async () => {
    stubRoutineFetch({ deliveries: [deliveryFixture()] });

    render(
      <AutomateWorkspace
        onAuthRequired={() => undefined}
        channelsSlice={channelsSliceFixture(channelFixture())}
      />
    );

    expect(
      await screen.findByRole("heading", { name: "Routine Workbench." })
    ).toBeVisible();
    await screen.findByText("delivery-key-1");

    expect(screen.queryByText(/exactly once/i)).not.toBeInTheDocument();
    expect(screen.getAllByText(/idempotent admission/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/connector receipt/i).length).toBeGreaterThan(0);
  });

  it("splits the workbench into routine list, detail delivery history, editor, and channels panels", async () => {
    stubRoutineFetch({ deliveries: [deliveryFixture()] });

    render(
      <AutomateWorkspace
        onAuthRequired={() => undefined}
        channelsSlice={channelsSliceFixture(channelFixture())}
      />
    );

    expect(
      await screen.findByRole("heading", { name: "Routine Workbench." })
    ).toBeVisible();

    const listPanel = await screen.findByRole("region", { name: /routines list/i });
    expect(within(listPanel).getByText("Morning review")).toBeInTheDocument();

    const deliverySection = await screen.findByRole("region", { name: /delivery history/i });
    expect(within(deliverySection).getByText("delivery-key-1")).toBeInTheDocument();
    expect(within(deliverySection).getByRole("button", { name: "Retry with same key" })).toBeInTheDocument();

    const channelsPanel = await screen.findByRole("region", { name: /delivery channels/i });
    // Channel id and provider are both "telegram" in the fixture; the id renders as the row title.
    expect(within(channelsPanel).getByText("telegram", { selector: "strong" })).toBeInTheDocument();
    expect(within(channelsPanel).getByRole("button", { name: /edit telegram/i })).toBeInTheDocument();
  });

  it("keeps the create routine editor a named sibling form", async () => {
    stubRoutineFetch();

    render(<AutomateWorkspace onAuthRequired={() => undefined} />);

    expect(
      await screen.findByRole("heading", { name: "Routine Workbench." })
    ).toBeVisible();

    const editorTrigger = await screen.findByRole("button", { name: /new routine/i });
    editorTrigger.click();

    const editor = await screen.findByRole("form", { name: "Create routine" });
    expect(within(editor).getByLabelText("Routine name")).toBeInTheDocument();
    expect(within(editor).getByLabelText("Prompt")).toBeInTheDocument();
  });
});
