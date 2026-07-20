import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type {
  Approval,
  Capability,
  Channel,
  McpServer,
  Routine,
  RoutineOccurrence,
  RoutineStatus,
  Run,
  SecretRef,
  Session,
  Skill,
  TaskGraph,
  Tool,
  TraceEvent
} from "./types";

const baseRun: Run = {
  run_id: "run_1",
  status: "completed",
  message: "Inspect the repo",
  session_id: "session_1",
  workspace: "/tmp/kestrel",
  provider: "mock",
  model: "mock",
  assistant_message: "Mock response",
  tool_count: 1,
  context_chars: 240,
  stop_reason: "completed",
  error: null,
  created_at: "2026-05-16T00:00:00Z",
  updated_at: "2026-05-16T00:00:01Z"
};

const secondRun: Run = {
  ...baseRun,
  run_id: "run_2",
  message: "Run tests",
  assistant_message: "Tests passed",
  created_at: "2026-05-16T00:02:00Z",
  updated_at: "2026-05-16T00:02:01Z"
};

const otherRun: Run = {
  ...baseRun,
  run_id: "run_other",
  message: "Fix parser",
  session_id: "session_2",
  assistant_message: "Parser fixed",
  created_at: "2026-05-16T00:05:00Z",
  updated_at: "2026-05-16T00:05:01Z"
};

const pendingApproval: Approval = {
  approval_id: "approval_1",
  run_id: "run_2",
  tool_call_id: "tool_shell",
  tool_name: "shell.run",
  arguments: { command: ["npm", "test"], cwd: "/tmp/kestrel" },
  risk: "high",
  status: "pending",
  created_at: "2026-05-16T00:02:02Z",
  updated_at: "2026-05-16T00:02:02Z"
};

const baseRoutine: Routine = {
  routine_id: "morning-review",
  name: "Morning review",
  prompt: "Review my pending local work and summarize the next action.",
  schedule_kind: "interval",
  start_at: "2026-05-17T13:00:00Z",
  interval_seconds: 86400,
  enabled: true,
  revision: 3,
  next_run_at: "2026-05-18T13:00:00Z",
  workspace: "/tmp/kestrel",
  provider: "mock",
  model: "mock",
  autonomy_mode: "background",
  misfire_grace_seconds: 300,
  last_scheduled_at: "2026-05-17T13:00:00Z",
  deleted_at: null,
  created_at: "2026-05-16T12:00:00Z",
  updated_at: "2026-05-17T13:00:00Z"
};

const baseRoutineOccurrence: RoutineOccurrence = {
  occurrence_id: "occ_morning_1",
  routine_id: baseRoutine.routine_id,
  routine_revision: baseRoutine.revision,
  scheduled_for: "2026-05-17T13:00:00Z",
  status: "completed",
  run_id: "run_routine_1",
  request: { prompt: baseRoutine.prompt },
  trigger_kind: "scheduled",
  requested_at: null,
  started_at: "2026-05-17T13:00:01Z",
  finished_at: "2026-05-17T13:00:02Z",
  skip_reason: null,
  error: null,
  result: {},
  created_at: "2026-05-17T13:00:00Z",
  updated_at: "2026-05-17T13:00:02Z"
};

let runs: Run[];
let sessions: Session[];
let sessionRuns: Record<string, Run[]>;
let approvals: Approval[];
let channelsPayload: Channel[];
let secrets: SecretRef[];
let toolsPayload: Tool[];
let skillsPayload: Skill[];
let mcpServersPayload: McpServer[];
let capabilitiesPayload: Capability[];
let capabilityMutationFailure: { status: number; detail: string } | null;
let onboardingProfile: Record<string, unknown> | null;
let eventSources: MockEventSource[];
let eventId: number;
let traceTimelines: Record<string, TraceEvent[]>;
let taskGraphs: Record<string, TaskGraph>;
let routinesPayload: Routine[];
let routineStatusPayload: RoutineStatus;
let routineHistories: Record<string, RoutineOccurrence[]>;
let routineLoadFailure: string | null;
let routineHistoryFailure: string | null;
let routineRunNowAmbiguousFailures: number;
let routineRunNowAccepted: Map<string, RoutineOccurrence>;
let routineRunNowInitialStatus: string;

function installRoutineHistoryPollTimers() {
  const nativeSetTimeout = window.setTimeout.bind(window);
  const nativeClearTimeout = window.clearTimeout.bind(window);
  type TimerId = NodeJS.Timeout;
  const pending = new Map<TimerId, () => void>();
  let nextTimerId = 10_000;

  vi.spyOn(window, "setTimeout").mockImplementation((handler, timeout, ...args) => {
    if (timeout === 1_500) {
      const timerId = nextTimerId++ as unknown as TimerId;
      pending.set(timerId, () => handler());
      return timerId;
    }
    return nativeSetTimeout(handler, timeout, ...args) as unknown as NodeJS.Timeout;
  });
  vi.spyOn(window, "clearTimeout").mockImplementation((timerId) => {
    if (timerId !== undefined && pending.delete(timerId as NodeJS.Timeout)) return;
    nativeClearTimeout(timerId as NodeJS.Timeout);
  });

  return {
    count: () => pending.size,
    takeNext: () => {
      const next = pending.entries().next().value as [TimerId, () => void] | undefined;
      if (!next) throw new Error("No routine history poll is scheduled.");
      pending.delete(next[0]);
      return next[1];
    }
  };
}

describe("App", () => {
  beforeEach(() => {
    runs = [otherRun, secondRun, baseRun];
    sessions = [
      {
        session_id: "session_2",
        run_count: 1,
        status_counts: { completed: 1 },
        latest_run_id: "run_other",
        latest_status: "completed",
        latest_message: "Fix parser",
        created_at: otherRun.created_at,
        updated_at: otherRun.updated_at
      },
      {
        session_id: "session_1",
        run_count: 2,
        status_counts: { completed: 2 },
        latest_run_id: "run_2",
        latest_status: "completed",
        latest_message: "Run tests",
        created_at: baseRun.created_at,
        updated_at: secondRun.updated_at
      }
    ];
    sessionRuns = {
      session_1: [baseRun, secondRun],
      session_2: [otherRun]
    };
    approvals = [pendingApproval];
    channelsPayload = [];
    secrets = [];
    toolsPayload = [
      {
        name: "memory.search",
        description: "Search nested memory.",
        risk: "low",
        requires_approval: false,
        source: "builtin",
        capabilities: ["memory"],
        enabled: true,
        enablement_flag: null
      },
      {
        name: "shell.run",
        description: "Run an allowlisted shell command.",
        risk: "high",
        requires_approval: true,
        source: "builtin",
        capabilities: [],
        enabled: false,
        enablement_flag: "allow_shell"
      },
      {
        name: "web.search",
        description: "Search outside context.",
        risk: "medium",
        requires_approval: false,
        source: "builtin",
        capabilities: ["web"],
        enabled: false,
        enablement_flag: "allow_web"
      }
    ];
    skillsPayload = [];
    mcpServersPayload = [];
    capabilitiesPayload = [
      capabilityFixture({
        key: "tool:memory.search",
        id: "memory.search",
        name: "Memory search",
        description: "Search nested memory.",
        default_enabled: true,
        configured_enabled: true,
        effective_enabled: true,
        risk: "low",
        source: "builtin"
      }),
      capabilityFixture({
        key: "tool:shell.run",
        id: "shell.run",
        name: "Shell run",
        description: "Run an allowlisted shell command.",
        default_enabled: false,
        configured_enabled: false,
        effective_enabled: false,
        blocked_by: ["allow_shell"],
        risk: "high",
        requires_approval: true,
        source: "builtin",
        enablement_flag: "allow_shell"
      }),
      capabilityFixture({
        key: "tool:web.search",
        id: "web.search",
        name: "Web search",
        description: "Search outside context.",
        default_enabled: false,
        configured_enabled: false,
        effective_enabled: false,
        blocked_by: ["allow_web"],
        risk: "medium",
        source: "builtin",
        enablement_flag: "allow_web"
      })
    ];
    capabilityMutationFailure = null;
    onboardingProfile = {
      schema_version: "kestrel_onboarding_profile.v1",
      setup_complete: true,
      agent_name: "Kestrel",
      user_name: "Tiuni",
      preferred_name: "Tiuni",
      persona: "steady",
      persona_name: "Steady Companion",
      persona_summary: "Warm, grounded, concise, and quietly capable.",
      persona_guidance: "Be warm and direct.",
      working_style: "Keep it practical.",
      goals: ["ship local-first tools"],
      interests: ["agent workbenches"],
      communication_notes: "",
      continuous_learning: true,
      updated_at: "2026-05-16T00:00:00Z"
    };
    eventSources = [];
    eventId = 1;
    traceTimelines = {
      run_1: [
        {
          id: 1,
          run_id: "run_1",
          type: "run.completed",
          payload: {
            proof_of_work: {
              completed_steps: ["answered"],
              validation_evidence: ["mock"],
              remaining_risks: []
            }
          },
          created_at: baseRun.updated_at
        }
      ]
    };
    taskGraphs = {};
    routinesPayload = [baseRoutine];
    routineStatusPayload = {
      enabled: true,
      loop: {
        running: true,
        tick_count: 8,
        last_result: null,
        last_error: null,
        tick_in_progress: false,
        current_tick_age_seconds: null,
        last_started_at: "2026-05-17T13:00:00Z",
        last_finished_at: "2026-05-17T13:00:02Z"
      }
    };
    routineHistories = { [baseRoutine.routine_id]: [baseRoutineOccurrence] };
    routineLoadFailure = null;
    routineHistoryFailure = null;
    routineRunNowAmbiguousFailures = 0;
    routineRunNowAccepted = new Map();
    routineRunNowInitialStatus = "completed";
    vi.stubGlobal("fetch", vi.fn(fetchMock));
    vi.stubGlobal("EventSource", MockEventSource);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    sessionStorage.clear();
    localStorage.clear();
  });

  it("opens on a conversation-first workspace with Advanced available", async () => {
    const { container } = render(<App />);

    expect(await screen.findByRole("heading", { name: "Ask Kestrel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new chat/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
    expect(screen.getByText("Safe Auto")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /advanced/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /chats/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /details/i })).toBeInTheDocument();
    expect(screen.queryByText("Command Center")).not.toBeInTheDocument();
    expect(screen.queryByText("Kernel")).not.toBeInTheDocument();
    expect(screen.queryByText("Registry")).not.toBeInTheDocument();
    expect(screen.queryByText("ORACLE Shadow")).not.toBeInTheDocument();

    const results = await axe.run(container, {
      rules: {
        "color-contrast": { enabled: false }
      }
    });
    expect(results.violations).toEqual([]);
  });

  it("keeps idle chat polling lightweight", async () => {
    const intervalCallbacks: Array<() => void> = [];
    vi.spyOn(window, "setInterval").mockImplementation((handler: TimerHandler, timeout?: number) => {
      if (typeof handler === "function" && timeout === 3500) {
        intervalCallbacks.push(() => handler());
      }
      return 1 as unknown as ReturnType<typeof window.setInterval>;
    });
    vi.spyOn(window, "clearInterval").mockImplementation(() => undefined);
    const fetchSpy = vi.mocked(fetch);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Ask Kestrel" })).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(([path]) => String(path).includes("/trace?limit=700"))).toBe(true);
    });
    expect(intervalCallbacks).toHaveLength(1);

    fetchSpy.mockClear();
    intervalCallbacks[0]();

    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(([path]) => path === "/api/runs")).toBe(true);
    });
    const idlePaths = fetchSpy.mock.calls.map(([path]) => String(path));
    expect(idlePaths).toEqual(expect.arrayContaining(["/api/runs", "/api/sessions", "/api/approvals?status=pending"]));
    expect(idlePaths).not.toContain("/api/tools");
    expect(idlePaths).not.toContain("/api/mcp/servers");
    expect(idlePaths).not.toContain("/api/skills");
    expect(idlePaths).not.toContain("/api/plugins");
    expect(idlePaths).not.toContain("/api/channels");
    expect(idlePaths).not.toContain("/api/secrets");
    expect(idlePaths).not.toContain("/api/memory/layers");
  });

  it("pulls incoming runs into the active thread without stealing transcript scroll", async () => {
    const intervalCallbacks: Array<() => void> = [];
    vi.spyOn(window, "setInterval").mockImplementation((handler: TimerHandler, timeout?: number) => {
      if (typeof handler === "function" && timeout === 3500) {
        intervalCallbacks.push(() => handler());
      }
      return 1 as unknown as ReturnType<typeof window.setInterval>;
    });
    vi.spyOn(window, "clearInterval").mockImplementation(() => undefined);
    const fetchSpy = vi.mocked(fetch);

    render(<App />);

    await screen.findAllByText("Run tests");
    expect(intervalCallbacks).toHaveLength(1);
    const transcript = screen.getByLabelText("Conversation transcript");
    let transcriptHeight = 1_200;
    Object.defineProperty(transcript, "scrollHeight", {
      configurable: true,
      get: () => transcriptHeight
    });
    Object.defineProperty(transcript, "clientHeight", { configurable: true, get: () => 400 });
    transcript.scrollTop = 100;
    fireEvent.scroll(transcript);

    const incomingRun: Run = {
      ...baseRun,
      run_id: "run_incoming",
      message: "Incoming same-thread message",
      assistant_message: "Incoming response",
      created_at: "2026-05-16T00:03:00Z",
      updated_at: "2026-05-16T00:03:01Z"
    };
    runs = [incomingRun, ...runs];
    sessionRuns.session_1 = [...sessionRuns.session_1, incomingRun];
    sessions = sessions.map((session) =>
      session.session_id === "session_1"
        ? {
            ...session,
            run_count: 3,
            status_counts: { completed: 3 },
            latest_run_id: incomingRun.run_id,
            latest_status: incomingRun.status,
            latest_message: incomingRun.message,
            updated_at: incomingRun.updated_at
          }
        : session
    );
    transcriptHeight = 1_500;
    fetchSpy.mockClear();

    intervalCallbacks[0]();

    expect((await screen.findAllByText("Incoming same-thread message")).length).toBeGreaterThan(0);
    expect(fetchSpy.mock.calls.some(([path]) => path === "/api/sessions/session_1/runs")).toBe(true);
    expect(transcript.scrollTop).toBe(100);
  });

  it("renders assistant markdown as rich chat prose", async () => {
    runs = [
      {
        ...baseRun,
        assistant_message: "Pick one:\n\n1. **Duck Compiler** — turns English into bytecode.\n2. `Bureaucratic Moon` — files forms in orbit."
      }
    ];
    sessions = [
      {
        session_id: "session_1",
        run_count: 1,
        status_counts: { completed: 1 },
        latest_run_id: "run_1",
        latest_status: "completed",
        latest_message: "Inspect the repo",
        created_at: baseRun.created_at,
        updated_at: baseRun.updated_at
      }
    ];
    sessionRuns = { session_1: runs };

    const { container } = render(<App />);

    expect(await screen.findByRole("heading", { name: "Ask Kestrel" })).toBeInTheDocument();
    await screen.findByText("Duck Compiler");
    const assistantMessage = container.querySelector(".msg.kestrel .markdown-message");
    expect(assistantMessage).toBeInTheDocument();
    expect(assistantMessage?.querySelector("ol")).toBeInTheDocument();
    expect(assistantMessage?.querySelector("strong")?.textContent).toBe("Duck Compiler");
    expect(assistantMessage?.querySelector("code")?.textContent).toBe("Bureaucratic Moon");
    expect(assistantMessage?.textContent).not.toContain("**Duck Compiler**");
  });

  it("renders Advanced command-center cockpit surfaces", async () => {
    const { container } = render(<App />);

    expect(await screen.findByRole("heading", { name: "Ask Kestrel" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));

    expect(await screen.findByRole("heading", { name: /advanced/i })).toBeInTheDocument();
    expect(container.querySelector(".advanced-overview")).toBeInTheDocument();
    expect(screen.getByText("Command Center")).toBeInTheDocument();
    expect(screen.getByText("Task Capsules")).toBeInTheDocument();
    expect(screen.getByText("Mutation Gate")).toBeInTheDocument();
    expect(screen.getByText("ORACLE Shadow")).toBeInTheDocument();
    expect(screen.getByText("Memory")).toBeInTheDocument();
    expect(screen.getAllByText("Tools").length).toBeGreaterThan(0);
  });

  it("loads the routine workbench with service status, definitions, history, and accessible owner controls", async () => {
    const { container } = render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("button", { name: "Routines" }));

    const workbench = await screen.findByRole("region", { name: "Routine Workbench" });
    expect(within(workbench).getByRole("heading", { name: "Routine Workbench." })).toBeInTheDocument();
    expect(within(workbench).getByText("running")).toBeInTheDocument();
    expect(within(workbench).getAllByText("Morning review").length).toBeGreaterThan(0);
    expect(within(workbench).getByText("Scheduled run")).toBeInTheDocument();
    expect(within(workbench).getByText("run_routine_1")).toBeInTheDocument();
    expect(within(workbench).getByRole("button", { name: "Pause Morning review" })).toBeEnabled();
    expect(within(workbench).getByRole("button", { name: "Edit Morning review" })).toBeEnabled();
    expect(within(workbench).getByRole("button", { name: "Delete Morning review" })).toBeEnabled();
    expect(within(workbench).getByRole("button", { name: "Run Morning review now" })).toBeEnabled();
    fireEvent.click(within(workbench).getByRole("button", { name: "New routine" }));
    const createForm = within(workbench).getByRole("form", { name: "Create routine" });
    expect(within(createForm).getByLabelText("Routine name")).toBeRequired();
    expect(within(createForm).getByLabelText("Prompt")).toBeRequired();
    expect(within(createForm).getByLabelText(/Start time/)).toBeRequired();
    expect(within(createForm).getByRole("button", { name: "Save routine" })).toBeEnabled();

    const results = await axe.run(container);
    expect(results.violations).toEqual([]);
  });

  it("shows fail-closed disabled and independent endpoint error states", async () => {
    routineStatusPayload = { enabled: false, loop: null };
    routineHistoryFailure = "routine_history_unavailable";
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: "Routines" }));

    expect(await screen.findByText("Proactive dispatch is disabled.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run Morning review now" })).toBeDisabled();
    expect(await screen.findByText("History unavailable: routine_history_unavailable")).toBeInTheDocument();
    expect(screen.getAllByText("Morning review").length).toBeGreaterThan(0);
  });

  it("keeps routine definitions visible when the independent status endpoint fails", async () => {
    routineLoadFailure = "routine_status_unavailable";
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: "Routines" }));

    expect(await screen.findByText("routine_status_unavailable")).toBeInTheDocument();
    expect(screen.getAllByText("Morning review").length).toBeGreaterThan(0);
    expect(screen.getByText("Scheduled run")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run Morning review now" })).toBeDisabled();
  });

  it("dispatches run now with the selected revision and a UUID idempotency key", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: "Routines" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run Morning review now" }));

    await waitFor(() => {
      const request = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/routines/morning-review/actions/run-now" && init?.method === "POST"
      );
      expect(request).toBeDefined();
      const body = JSON.parse(String(request?.[1]?.body ?? "{}"));
      expect(body.expected_revision).toBe(3);
      expect(body.idempotency_key).toMatch(/^[0-9a-f-]{36}$/i);
    });
    expect(await screen.findByText("Dispatch accepted")).toBeInTheDocument();

    const runAgain = screen.getByRole("button", { name: "Run Morning review now" });
    await waitFor(() => expect(runAgain).toBeEnabled());
    fireEvent.click(runAgain);
    await waitFor(() => {
      const requests = fetchSpy.mock.calls.filter(
        ([path, init]) => path === "/api/routines/morning-review/actions/run-now" && init?.method === "POST"
      );
      expect(requests).toHaveLength(2);
      const first = JSON.parse(String(requests[0][1]?.body ?? "{}"));
      const second = JSON.parse(String(requests[1][1]?.body ?? "{}"));
      expect(second.idempotency_key).not.toBe(first.idempotency_key);
    });
  });

  it("polls nonterminal routine history without overlap and refreshes the accepted run result", async () => {
    routineRunNowInitialStatus = "claimed";
    const timers = installRoutineHistoryPollTimers();
    const originalFetch = fetchMock;
    let historyRequestCount = 0;
    let resolvePoll!: (response: Response) => void;
    const pendingPoll = new Promise<Response>((resolve) => {
      resolvePoll = resolve;
    });
    vi.mocked(fetch).mockImplementation((input, init) => {
      const raw = typeof input === "string" || input instanceof URL ? input.toString() : input.url;
      const url = new URL(raw, "http://kestrel.test");
      if (url.pathname === "/api/routines/morning-review/history") {
        historyRequestCount += 1;
        if (historyRequestCount === 3) return pendingPoll;
      }
      return originalFetch(input, init);
    });

    const { container } = render(<App />);
    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: "Routines" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run Morning review now" }));

    const acceptedResult = await waitFor(() => {
      const result = container.querySelector<HTMLElement>(".routine-run-result");
      expect(result).not.toBeNull();
      expect(within(result!).getByText("claimed")).toBeInTheDocument();
      return result!;
    });
    await waitFor(() => expect(timers.count()).toBe(1));

    await act(async () => {
      timers.takeNext()();
      await Promise.resolve();
    });
    await waitFor(() => expect(historyRequestCount).toBe(3));
    expect(timers.count()).toBe(0);

    const claimed = routineHistories[baseRoutine.routine_id][0];
    const completed: RoutineOccurrence = {
      ...claimed,
      status: "completed",
      finished_at: "2026-05-17T14:00:04Z",
      updated_at: "2026-05-17T14:00:04Z"
    };
    routineHistories[baseRoutine.routine_id] = [
      completed,
      ...routineHistories[baseRoutine.routine_id].slice(1)
    ];
    await act(async () => {
      resolvePoll(jsonResponse(routineHistories[baseRoutine.routine_id]));
      await pendingPoll;
    });

    await waitFor(() => expect(within(acceptedResult).getByText("completed")).toBeInTheDocument());
    await waitFor(() => expect(timers.count()).toBe(0));
    expect(historyRequestCount).toBe(3);
  });

  it("cancels routine history polling across selection changes and unmount", async () => {
    const timers = installRoutineHistoryPollTimers();
    const eveningRoutine: Routine = {
      ...baseRoutine,
      routine_id: "evening-review",
      name: "Evening review",
      revision: 1
    };
    routineHistories = {
      [baseRoutine.routine_id]: [{ ...baseRoutineOccurrence, status: "running" }],
      [eveningRoutine.routine_id]: [
        {
          ...baseRoutineOccurrence,
          occurrence_id: "occ_evening_1",
          routine_id: eveningRoutine.routine_id,
          routine_revision: eveningRoutine.revision,
          run_id: "run_evening_1"
        }
      ]
    };
    routinesPayload = [baseRoutine, eveningRoutine];
    const fetchSpy = vi.mocked(fetch);

    render(<App />);
    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: "Routines" }));
    await screen.findByText("run_routine_1");
    await waitFor(() => expect(timers.count()).toBe(1));
    const staleSelectionPoll = timers.takeNext();

    fireEvent.click(screen.getByRole("button", { name: /^Evening review/ }));
    await screen.findByText("run_evening_1");
    const callsAfterSelection = fetchSpy.mock.calls.length;
    await act(async () => {
      staleSelectionPoll();
      await Promise.resolve();
    });
    expect(fetchSpy.mock.calls).toHaveLength(callsAfterSelection);
    expect(timers.count()).toBe(0);

    fireEvent.click(screen.getByRole("button", { name: /^Morning review/ }));
    await screen.findByText("run_routine_1");
    await waitFor(() => expect(timers.count()).toBe(1));
    const staleUnmountPoll = timers.takeNext();
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    await screen.findByRole("heading", { name: "Ask Kestrel" });
    const callsAfterUnmount = fetchSpy.mock.calls.length;
    await act(async () => {
      staleUnmountPoll();
      await Promise.resolve();
    });
    expect(fetchSpy.mock.calls).toHaveLength(callsAfterUnmount);
    expect(timers.count()).toBe(0);
  });

  it("reuses the same run-now idempotency key after an ambiguous network result", async () => {
    const fetchSpy = vi.mocked(fetch);
    routineRunNowAmbiguousFailures = 1;
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: "Routines" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run Morning review now" }));

    expect(await screen.findByText(/No response was received for Morning review/)).toBeInTheDocument();
    expect(screen.getByText(/reuse the original idempotency key and revision/i)).toBeInTheDocument();
    const storageKey = "kestrel.routine.run-now.v1:morning-review";
    const stored = JSON.parse(String(sessionStorage.getItem(storageKey)));
    expect(stored.expectedRevision).toBe(3);

    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: "Routines" }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry Morning review now" }));

    await waitFor(() => {
      const requests = fetchSpy.mock.calls.filter(
        ([path, init]) => path === "/api/routines/morning-review/actions/run-now" && init?.method === "POST"
      );
      expect(requests).toHaveLength(2);
      const first = JSON.parse(String(requests[0][1]?.body ?? "{}"));
      const retry = JSON.parse(String(requests[1][1]?.body ?? "{}"));
      expect(retry).toEqual(first);
      expect(first.idempotency_key).toBe(stored.idempotencyKey);
    });
    expect(await screen.findByText("Recovered dispatch")).toBeInTheDocument();
    expect(sessionStorage.getItem(storageKey)).toBeNull();
  });


  it("renders the Learning Dashboard panel under behavior deltas", async () => {
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));

    expect(await screen.findByRole("heading", { name: "Learning Dashboard" })).toBeInTheDocument();
    expect(screen.getByText("Auto-activations")).toBeInTheDocument();
    expect(screen.getByText("Activations then rolled back"));
    expect(screen.getByText("procedural"));
  });

  it("surfaces repair patch review validation and rollback state from the task graph", async () => {
    const validationId = "repair_validation_aaaaaaaaaaaaaaaaaaaaaaaa";
    const reviewId = "repair_review_bbbbbbbbbbbbbbbbbbbbbbbb";
    const repairSnapshot = {
      branch: "kestrel/worker/run-repair/repair",
      head_sha: "1111111111111111111111111111111111111111",
      diff_digest: "deadbeefcafebabedeadbeefcafebabedeadbeefcafebabedeadbeefcafebabe"
    };
    const repairRun: Run = {
      ...baseRun,
      run_id: "run_repair",
      message: "Repair calculator add",
      session_id: "session_repair",
      assistant_message: "Repair ready for review"
    };
    runs = [repairRun];
    sessions = [
      {
        session_id: "session_repair",
        run_count: 1,
        status_counts: { completed: 1 },
        latest_run_id: "run_repair",
        latest_status: "completed",
        latest_message: "Repair calculator add",
        created_at: repairRun.created_at,
        updated_at: repairRun.updated_at
      }
    ];
    sessionRuns = { session_repair: [repairRun] };
    approvals = [];
    taskGraphs.run_repair = {
      tasks: [
        {
          task_id: "validate",
          title: "Validate repair",
          goal: "Run targeted validation",
          profile: "worker",
          status: "completed",
          approved: true,
          required_tools: ["repair.validate"],
          risk: "high",
          attempt_count: 1,
          result: {
            repair_artifact: {
              schema_version: 1,
              tool: "repair.validate",
              validation_id: validationId,
              repair_snapshot: repairSnapshot
            }
          }
        },
        {
          task_id: "review",
          title: "Review repair before commit",
          goal: "Create durable review artifact",
          profile: "reviewer",
          status: "completed",
          approved: true,
          required_tools: ["repair.review"],
          risk: "medium",
          attempt_count: 1,
          result: {
            repair_artifact: {
              schema_version: 1,
              tool: "repair.review",
              validation_id: validationId,
              review_id: reviewId,
              repair_snapshot: repairSnapshot,
              changed_files: ["src/calculator.py"],
              commit_gate: { commit_allowed: true, approval_required_before_commit: true }
            }
          }
        },
        {
          task_id: "rollback",
          title: "Rollback stale repair",
          goal: "Restore tracked repair changes",
          profile: "worker",
          status: "ready",
          approved: false,
          required_tools: ["repair.rollback"],
          risk: "high",
          attempt_count: 0,
          result: {
            rollback_id: "rollback_abc123",
            restored_files: ["src/calculator.py"],
            artifact_path: ".nest/repair_rollbacks/rollback_abc123.json"
          }
        }
      ],
      ready_tasks: [],
      approval_blocked_tasks: [],
      subagents: []
    };

    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));

    const panel = await screen.findByLabelText("Repair Patch Review");
    expect(within(panel).getByText(`Validation passed: ${validationId}`)).toBeInTheDocument();
    expect(within(panel).getByText(`Candidate digest ${repairSnapshot.diff_digest}`)).toBeInTheDocument();
    expect(within(panel).getByText(`Review gate: ${reviewId} · commit approval required`)).toBeInTheDocument();
    expect(within(panel).getByText(`Diff ${repairSnapshot.diff_digest} · src/calculator.py`)).toBeInTheDocument();
    expect(within(panel).getByText(`Candidate ${repairSnapshot.branch} @ ${repairSnapshot.head_sha}`)).toBeInTheDocument();
    expect(within(panel).getByText("Rollback state: ready · rollback_abc123")).toBeInTheDocument();
    expect(within(panel).getByText("Restores src/calculator.py and preserves .nest/repair_rollbacks/rollback_abc123.json")).toBeInTheDocument();
  });

  it("prepares exact-call repair commit and rollback tool requests without invoking them", async () => {
    const fetchSpy = vi.mocked(fetch);
    const repairRun: Run = {
      ...baseRun,
      run_id: "run_repairactions",
      message: "Repair calculator add",
      session_id: "session_repair_actions",
      assistant_message: "Repair ready for exact-call action"
    };
    runs = [repairRun];
    sessions = [
      {
        session_id: "session_repair_actions",
        run_count: 1,
        status_counts: { completed: 1 },
        latest_run_id: "run_repairactions",
        latest_status: "completed",
        latest_message: "Repair calculator add",
        created_at: repairRun.created_at,
        updated_at: repairRun.updated_at
      }
    ];
    sessionRuns = { session_repair_actions: [repairRun] };
    approvals = [];
    toolsPayload = [
      ...toolsPayload,
      { name: "git.commit", description: "Commit reviewed repair.", risk: "high", requires_approval: true, source: "builtin", capabilities: ["git"], enabled: true, enablement_flag: null },
      { name: "repair.rollback", description: "Rollback repair.", risk: "high", requires_approval: true, source: "builtin", capabilities: ["safe-repair"], enabled: true, enablement_flag: null }
    ];
    taskGraphs.run_repairactions = {
      tasks: [
        {
          task_id: "review",
          title: "Review repair before commit",
          goal: "Create durable review artifact",
          profile: "reviewer",
          status: "completed",
          approved: true,
          required_tools: ["repair.review"],
          risk: "medium",
          attempt_count: 1,
          result: {
            review_id: "review_action123",
            diff_hash: "feedfacecafebeef",
            changed_files: ["src/calculator.py"],
            commit_gate: { approval_required_before_commit: true }
          }
        },
        {
          task_id: "rollback",
          title: "Rollback stale repair",
          goal: "Restore tracked repair changes",
          profile: "worker",
          status: "ready",
          approved: false,
          required_tools: ["repair.rollback"],
          risk: "high",
          attempt_count: 0,
          result: {
            rollback_id: "rollback_action123",
            restored_files: ["src/calculator.py"],
            artifact_path: ".nest/repair_rollbacks/rollback_action123.json"
          }
        }
      ],
      ready_tasks: [],
      approval_blocked_tasks: [],
      subagents: []
    };

    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    const panel = await screen.findByLabelText("Repair Patch Review");

    fireEvent.click(within(panel).getByRole("button", { name: /prepare exact-call git.commit/i }));
    expect(screen.getByLabelText("Tool")).toHaveValue("git.commit");
    const toolArgsInput = screen.getAllByLabelText("Arguments JSON")[0] as HTMLTextAreaElement;
    const commitArgs = JSON.parse(toolArgsInput.value);
    expect(commitArgs).toMatchObject({ repair_review_id: "review_action123" });
    expect(String(commitArgs.message)).toContain("review_action123");
    let preview = screen.getByLabelText("Exact-call approval preview");
    expect(within(preview).getByText("Prepared exact-call request: git.commit")).toBeInTheDocument();
    expect(within(preview).getByText("Invoking this request will create or require approval before execution; it has not run yet.")).toBeInTheDocument();
    expect(within(preview).getByRole("link", { name: /review prepared request in tool form/i })).toHaveAttribute("href", "#tools");
    expect(within(preview).getByText(/review_action123/)).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: /prepare exact-call repair.rollback/i }));
    expect(screen.getByLabelText("Tool")).toHaveValue("repair.rollback");
    const rollbackArgs = JSON.parse(toolArgsInput.value);
    expect(rollbackArgs).toMatchObject({ review_id: "review_action123", reason: "Rollback reviewed repair review_action123" });
    preview = screen.getByLabelText("Exact-call approval preview");
    expect(within(preview).getByText("Prepared exact-call request: repair.rollback")).toBeInTheDocument();
    expect(within(preview).getByText(/Rollback reviewed repair review_action123/)).toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([path, init]) => String(path).includes("/api/tools/") && init?.method === "POST")).toBe(false);
  });

  it("submits a prepared repair commit request into a pending approval card with exact arguments", async () => {
    const fetchSpy = vi.mocked(fetch);
    const repairRun: Run = {
      ...baseRun,
      run_id: "run_repairapproval",
      session_id: "session_repair_approval",
      message: "Repair calculator add",
      assistant_message: "Reviewed repair ready for commit.",
      status: "blocked",
      stop_reason: "tool_approval_required",
      created_at: "2026-05-16T00:20:00Z",
      updated_at: "2026-05-16T00:20:00Z"
    };
    sessions = [
      {
        session_id: "session_repair_approval",
        run_count: 1,
        status_counts: { blocked: 1 },
        latest_run_id: "run_repairapproval",
        latest_status: "blocked",
        latest_message: "Repair calculator add",
        created_at: repairRun.created_at,
        updated_at: repairRun.updated_at
      }
    ];
    sessionRuns = { session_repair_approval: [repairRun] };
    runs = [repairRun];
    approvals = [];
    toolsPayload = [
      ...toolsPayload,
      { name: "git.commit", description: "Commit reviewed repair.", risk: "high", requires_approval: true, source: "builtin", capabilities: ["git"], enabled: true, enablement_flag: null }
    ];
    taskGraphs.run_repairapproval = {
      tasks: [
        {
          task_id: "review",
          title: "Review repair before commit",
          goal: "Create durable review artifact",
          profile: "reviewer",
          status: "completed",
          approved: true,
          required_tools: ["repair.review"],
          risk: "medium",
          attempt_count: 1,
          result: {
            review_id: "review_approval123",
            diff_hash: "feedfacecafebeef",
            changed_files: ["src/calculator.py"],
            commit_gate: { approval_required_before_commit: true }
          }
        }
      ],
      ready_tasks: [],
      approval_blocked_tasks: [],
      subagents: []
    };

    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    const panel = await screen.findByLabelText("Repair Patch Review");
    fireEvent.click(within(panel).getByRole("button", { name: /prepare exact-call git.commit/i }));
    const preparedArgs = JSON.parse((screen.getAllByLabelText("Arguments JSON")[0] as HTMLTextAreaElement).value);

    fireEvent.click(screen.getByRole("button", { name: /invoke tool/i }));

    await waitFor(() => {
      const invokeCall = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/tools/git.commit/invoke" && init?.method === "POST"
      );
      expect(invokeCall).toBeDefined();
      expect(JSON.parse(String(invokeCall?.[1]?.body ?? "{}"))).toMatchObject({
        run_id: "run_repairapproval",
        session_id: "session_repair_approval",
        arguments: preparedArgs
      });
    });
    const approvalCard = await screen.findByRole("group", { name: /approval for git.commit/i });
    expect(within(approvalCard).getByText("git.commit")).toBeInTheDocument();
    expect(within(approvalCard).getByText(/High risk/i)).toBeInTheDocument();
    expect(within(approvalCard).getByText(/review_approval123/)).toBeInTheDocument();
    expect(within(approvalCard).getByText(new RegExp(String(preparedArgs.message)))).toBeInTheDocument();
  });

  it("submits a prepared repair rollback request into a pending approval card with exact arguments", async () => {
    const fetchSpy = vi.mocked(fetch);
    const repairRun: Run = {
      ...baseRun,
      run_id: "run_rollbackapproval",
      session_id: "session_rollback_approval",
      message: "Rollback stale repair",
      assistant_message: "Reviewed repair can be rolled back.",
      status: "blocked",
      stop_reason: "tool_approval_required",
      created_at: "2026-05-16T00:25:00Z",
      updated_at: "2026-05-16T00:25:00Z"
    };
    sessions = [
      {
        session_id: "session_rollback_approval",
        run_count: 1,
        status_counts: { blocked: 1 },
        latest_run_id: "run_rollbackapproval",
        latest_status: "blocked",
        latest_message: "Rollback stale repair",
        created_at: repairRun.created_at,
        updated_at: repairRun.updated_at
      }
    ];
    sessionRuns = { session_rollback_approval: [repairRun] };
    runs = [repairRun];
    approvals = [];
    toolsPayload = [
      ...toolsPayload,
      { name: "repair.rollback", description: "Rollback repair.", risk: "high", requires_approval: true, source: "builtin", capabilities: ["safe-repair"], enabled: true, enablement_flag: null }
    ];
    taskGraphs.run_rollbackapproval = {
      tasks: [
        {
          task_id: "review",
          title: "Review repair before rollback",
          goal: "Confirm durable repair review artifact",
          profile: "reviewer",
          status: "completed",
          approved: true,
          required_tools: ["repair.review"],
          risk: "medium",
          attempt_count: 1,
          result: {
            review_id: "review_rollback123",
            diff_hash: "feedfacecafebeef",
            changed_files: ["src/calculator.py"],
            commit_gate: { approval_required_before_commit: true }
          }
        },
        {
          task_id: "rollback",
          title: "Rollback stale repair",
          goal: "Restore tracked repair changes",
          profile: "worker",
          status: "ready",
          approved: false,
          required_tools: ["repair.rollback"],
          risk: "high",
          attempt_count: 0,
          result: {
            rollback_id: "rollback_approval123",
            restored_files: ["src/calculator.py"],
            artifact_path: ".nest/repair_rollbacks/rollback_approval123.json"
          }
        }
      ],
      ready_tasks: [],
      approval_blocked_tasks: [],
      subagents: []
    };

    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    const panel = await screen.findByLabelText("Repair Patch Review");
    fireEvent.click(within(panel).getByRole("button", { name: /prepare exact-call repair.rollback/i }));
    const preparedArgs = JSON.parse((screen.getAllByLabelText("Arguments JSON")[0] as HTMLTextAreaElement).value);

    fireEvent.click(screen.getByRole("button", { name: /invoke tool/i }));

    await waitFor(() => {
      const invokeCall = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/tools/repair.rollback/invoke" && init?.method === "POST"
      );
      expect(invokeCall).toBeDefined();
      expect(JSON.parse(String(invokeCall?.[1]?.body ?? "{}"))).toMatchObject({
        run_id: "run_rollbackapproval",
        session_id: "session_rollback_approval",
        arguments: preparedArgs
      });
    });
    const approvalCard = await screen.findByRole("group", { name: /approval for repair.rollback/i });
    expect(within(approvalCard).getByText("repair.rollback")).toBeInTheDocument();
    expect(within(approvalCard).getByText(/High risk/i)).toBeInTheDocument();
    expect(within(approvalCard).getByText(/review_rollback123/)).toBeInTheDocument();
    expect(within(approvalCard).getByText(new RegExp(String(preparedArgs.reason)))).toBeInTheDocument();
  });

  it("creates a new local thread and sends without manual session, provider, or model fields", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /new chat/i }));
    fireEvent.change(screen.getByLabelText("Ask Kestrel"), { target: { value: "Build a parser" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    await waitFor(() => {
      const runCall = fetchSpy.mock.calls.find(([path, init]) => path === "/api/runs" && init?.method === "POST");
      expect(runCall).toBeDefined();
      const body = JSON.parse(String(runCall?.[1]?.body ?? "{}"));
      expect(body.message).toBe("Build a parser");
      expect(body.session_id).toMatch(/^thread_/);
      expect(body.autonomy_mode).toBe("autonomous");
      expect(body).not.toHaveProperty("provider");
      expect(body).not.toHaveProperty("model");
    });
  });

  it("clears a queued success notice after the run reaches a terminal state", async () => {
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /new chat/i }));
    fireEvent.change(screen.getByLabelText("Ask Kestrel"), { target: { value: "Finish this task" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));

    expect(await screen.findByText("Run queued.")).toBeInTheDocument();
    const created = runs.find((run) => run.run_id === "run_created");
    expect(created).toBeDefined();
    const completed = {
      ...created!,
      status: "completed",
      assistant_message: "Task complete",
      stop_reason: "completed",
      updated_at: "2026-05-16T00:10:01Z"
    } satisfies Run;
    runs = runs.map((run) => run.run_id === completed.run_id ? completed : run);
    sessionRuns[completed.session_id] = (sessionRuns[completed.session_id] ?? []).map((run) =>
      run.run_id === completed.run_id ? completed : run
    );

    await waitFor(() => expect(eventSources.length).toBeGreaterThan(1));
    eventSources[eventSources.length - 1].emit("run.completed", {});

    await waitFor(() => expect(screen.queryByText("Run queued.")).not.toBeInTheDocument());
    expect(await screen.findByText("Task complete")).toBeInTheDocument();
  });

  it("resets the workspace scroll position when navigating between sections", async () => {
    const { container } = render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    const workspace = container.querySelector<HTMLElement>("main#workspace");
    expect(workspace).not.toBeNull();
    workspace!.scrollTop = 420;

    fireEvent.click(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("button", { name: "Routines" }));

    await screen.findByRole("region", { name: "Routine Workbench" });
    expect(workspace!.scrollTop).toBe(0);
  });

  it("follows live transcript output unless the user has scrolled away from the bottom", async () => {
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    const transcript = await screen.findByLabelText("Conversation transcript");
    let transcriptHeight = 1_200;
    Object.defineProperty(transcript, "scrollHeight", { configurable: true, get: () => transcriptHeight });
    Object.defineProperty(transcript, "clientHeight", { configurable: true, get: () => 400 });
    transcript.scrollTop = 800;
    fireEvent.scroll(transcript);

    const stream = await waitForEventSource();
    transcriptHeight = 1_500;
    stream.emit("assistant.token", { content: "More output" });
    await waitFor(() => expect(transcript.scrollTop).toBe(1_500));

    transcript.scrollTop = 100;
    fireEvent.scroll(transcript);
    transcriptHeight = 1_800;
    stream.emit("assistant.token", { content: "Even more output" });
    await act(async () => Promise.resolve());
    expect(transcript.scrollTop).toBe(100);
  });

  it("keeps a new empty thread isolated from the previous active run", async () => {
    render(<App />);

    await screen.findByText("Fix parser");
    fireEvent.click(screen.getByRole("button", { name: /new chat/i }));

    expect(within(screen.getByLabelText("Conversation threads")).getByRole("button", { name: /new chat/i })).toHaveClass("active");
    expect(screen.getByText("Tell Kestrel what to do.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /details/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    await waitFor(() => {
      expect(within(screen.getByLabelText("Conversation threads")).getByRole("button", { name: /new chat/i })).toHaveClass("active");
      expect(screen.queryByRole("button", { name: /details/i })).not.toBeInTheDocument();
    });

    const transcript = screen.getByLabelText("Conversation transcript");
    expect(transcript).not.toHaveTextContent("Fix parser");
    expect(transcript).not.toHaveTextContent("Run tests");
  });

  it("switches threads and renders active session history in chronological order", async () => {
    render(<App />);

    await screen.findAllByText("Run tests");
    fireEvent.click(screen.getByRole("button", { name: /run tests/i }));

    const transcript = await screen.findByLabelText("Conversation transcript");
    const text = transcript.textContent ?? "";
    expect(text.indexOf("Inspect the repo")).toBeLessThan(text.indexOf("Run tests"));
    expect(text).toContain("Mock response");
    expect(text).toContain("Tests passed");

    fireEvent.click(screen.getByRole("button", { name: /fix parser/i }));
    await waitFor(() => {
      expect(screen.getByLabelText("Conversation transcript")).toHaveTextContent("Parser fixed");
    });
  });

  it("streams progress and assistant tokens without duplicating the final assistant message", async () => {
    sessionRuns.session_1 = [{ ...baseRun, status: "running", assistant_message: "", stop_reason: "" }];
    runs = [sessionRuns.session_1[0], otherRun];
    sessions = [
      { ...sessions[1], latest_run_id: "run_1", latest_status: "running", latest_message: "Inspect the repo" },
      sessions[0]
    ];
    approvals = [];
    render(<App />);

    await screen.findAllByText("Inspect the repo");
    const stream = await waitForEventSource();
    stream.emit("context.compile", { query: "repo" });
    stream.emit("assistant.token", { content: "Streaming answer" });
    expect(await screen.findByText("Gathering context")).toBeInTheDocument();
    expect(await screen.findByText("Streaming answer")).toBeInTheDocument();

    sessionRuns.session_1 = [{ ...sessionRuns.session_1[0], status: "completed", assistant_message: "Streaming answer", stop_reason: "completed" }];
    runs = [sessionRuns.session_1[0], otherRun];
    stream.emit("run.completed", {});

    await waitFor(() => {
      const transcript = screen.getByLabelText("Conversation transcript");
      expect(transcript).toHaveTextContent("Streaming answer");
      expect(transcript).not.toHaveTextContent("Streaming answerStreaming answer");
    });
  });

  it("shows live thinking and tool use inside the active assistant turn", async () => {
    sessionRuns.session_1 = [{ ...baseRun, status: "running", assistant_message: "", stop_reason: "" }];
    runs = [sessionRuns.session_1[0], otherRun];
    sessions = [
      { ...sessions[1], latest_run_id: "run_1", latest_status: "running", latest_message: "Inspect the repo" },
      sessions[0]
    ];
    approvals = [];
    render(<App />);

    await screen.findAllByText("Inspect the repo");
    const stream = await waitForEventSource();
    stream.emit("context.compile", { query: "repo", context_chars: 1200 });
    stream.emit("tool.started", { tool: "shell.run", tool_call_id: "tool_1" });
    stream.emit("tool.completed", {
      tool: "shell.run",
      tool_call_id: "tool_1",
      arguments: { command: ["npm", "test"] },
      content: "7 passed",
      success: true
    });

    const activity = await screen.findByLabelText("Live run activity");
    expect(within(activity).getByText("Thinking")).toBeInTheDocument();
    expect(within(activity).getByText("Gathering context")).toBeInTheDocument();
    expect(within(activity).getByText("Using shell.run")).toBeInTheDocument();
    expect(within(activity).getByText("Finished shell.run")).toBeInTheDocument();
    expect(within(activity).getByText("npm test")).toBeInTheDocument();
    expect(within(activity).getByText("7 passed")).toBeInTheDocument();
  });

  it("shows a typing indicator while an active run has no visible events yet", async () => {
    sessionRuns.session_1 = [{ ...baseRun, status: "running", assistant_message: "", stop_reason: "" }];
    runs = [sessionRuns.session_1[0], otherRun];
    sessions = [
      { ...sessions[1], latest_run_id: "run_1", latest_status: "running", latest_message: "Inspect the repo" },
      sessions[0]
    ];
    approvals = [];
    traceTimelines.run_1 = [];

    render(<App />);

    await screen.findAllByText("Inspect the repo");
    expect(await screen.findByLabelText("Kestrel is responding")).toHaveTextContent("Working");
  });

  it("hydrates thinking and tool use from the selected run trace", async () => {
    sessionRuns.session_1 = [{ ...baseRun, status: "completed", assistant_message: "Done", stop_reason: "complete" }];
    runs = [sessionRuns.session_1[0], otherRun];
    sessions = [
      { ...sessions[1], run_count: 1, latest_run_id: "run_1", latest_status: "completed", latest_message: "Inspect the repo" },
      sessions[0]
    ];
    approvals = [];
    traceTimelines.run_1 = [
      {
        id: 10,
        run_id: "",
        type: "context.compile",
        payload: { context_chars: 1200 },
        created_at: "2026-05-16T00:00:02Z"
      },
      {
        id: 11,
        run_id: "",
        type: "tool.completed",
        payload: { tool: "shell.run", arguments: { command: ["npm", "test"] }, content: "7 passed" },
        created_at: "2026-05-16T00:00:03Z"
      }
    ];
    render(<App />);

    const activity = await screen.findByLabelText("Live run activity");
    expect(within(activity).getByText("Thinking")).toBeInTheDocument();
    expect(within(activity).getByText("Gathering context")).toBeInTheDocument();
    expect(within(activity).getByText("Finished shell.run")).toBeInTheDocument();
    expect(within(activity).getByText("npm test")).toBeInTheDocument();
    expect(within(activity).getByText("7 passed")).toBeInTheDocument();
  });

  it("renders inline approvals for the active thread and preserves exact-call decision arguments", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    expect((await screen.findAllByText("Needs approval")).length).toBeGreaterThan(0);
    const approvalCard = screen.getByRole("group", { name: /approval for shell.run/i });
    expect(within(approvalCard).getByText("shell.run")).toBeInTheDocument();
    expect(within(approvalCard).getByText("High risk")).toBeInTheDocument();

    fireEvent.click(within(approvalCard).getByRole("button", { name: /approve/i }));
    await waitFor(() => {
      const decisionCall = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/approvals/approval_1/decision" && init?.method === "POST"
      );
      expect(decisionCall).toBeDefined();
      expect(JSON.parse(String(decisionCall?.[1]?.body ?? "{}"))).toEqual({
        approved: true,
        arguments: pendingApproval.arguments
      });
    });
  });

  it("approves repair commit cards with immutable approval arguments after tool form edits", async () => {
    const fetchSpy = vi.mocked(fetch);
    const exactCommitArgs = {
      repair_review_id: "review_decision_commit",
      message: "repair: commit exactly reviewed diff"
    };
    approvals = [
      {
        approval_id: "approval_repair_commit_decision",
        run_id: "run_2",
        tool_call_id: "tool_repair_commit_decision",
        tool_name: "git.commit",
        arguments: exactCommitArgs,
        risk: "high",
        status: "pending",
        created_at: "2026-05-16T00:30:00Z",
        updated_at: "2026-05-16T00:30:00Z"
      }
    ];

    render(<App />);

    expect((await screen.findAllByText("Needs approval")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    fireEvent.change(screen.getAllByLabelText("Arguments JSON")[0], {
      target: {
        value: JSON.stringify(
          { repair_review_id: "edited_form_review", message: "edited form state must not be approved" },
          null,
          2
        )
      }
    });

    const approvalCard = screen.getAllByRole("group", { name: /approval for git.commit/i })[0];
    fireEvent.click(within(approvalCard).getByRole("button", { name: /approve/i }));

    await waitFor(() => {
      const decisionCall = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/approvals/approval_repair_commit_decision/decision" && init?.method === "POST"
      );
      expect(decisionCall).toBeDefined();
      expect(JSON.parse(String(decisionCall?.[1]?.body ?? "{}"))).toEqual({
        approved: true,
        arguments: exactCommitArgs
      });
    });
  });

  it("approves repair rollback cards with immutable approval arguments after tool form edits", async () => {
    const fetchSpy = vi.mocked(fetch);
    const exactRollbackArgs = {
      review_id: "review_decision_rollback",
      reason: "Rollback the reviewed repair only"
    };
    approvals = [
      {
        approval_id: "approval_repair_rollback_decision",
        run_id: "run_2",
        tool_call_id: "tool_repair_rollback_decision",
        tool_name: "repair.rollback",
        arguments: exactRollbackArgs,
        risk: "high",
        status: "pending",
        created_at: "2026-05-16T00:31:00Z",
        updated_at: "2026-05-16T00:31:00Z"
      }
    ];

    render(<App />);

    expect((await screen.findAllByText("Needs approval")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    fireEvent.change(screen.getAllByLabelText("Arguments JSON")[0], {
      target: {
        value: JSON.stringify(
          { review_id: "edited_form_review", reason: "edited form state must not be approved" },
          null,
          2
        )
      }
    });

    const approvalCard = screen.getAllByRole("group", { name: /approval for repair.rollback/i })[0];
    fireEvent.click(within(approvalCard).getByRole("button", { name: /approve/i }));

    await waitFor(() => {
      const decisionCall = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/approvals/approval_repair_rollback_decision/decision" && init?.method === "POST"
      );
      expect(decisionCall).toBeDefined();
      expect(JSON.parse(String(decisionCall?.[1]?.body ?? "{}"))).toEqual({
        approved: true,
        arguments: exactRollbackArgs
      });
    });
  });

  it("stores secrets through the broker without rendering raw values", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    fireEvent.change(await screen.findByLabelText("Secret name"), { target: { value: "TELEGRAM_BOT_TOKEN" } });
    fireEvent.change(screen.getByLabelText(/Secret value/), { target: { value: "123456:ABC-super-secret" } });
    fireEvent.click(screen.getByRole("button", { name: /store secret/i }));

    await waitFor(() => {
      const secretCall = fetchSpy.mock.calls.find(([path, init]) => path === "/api/secrets" && init?.method === "POST");
      expect(secretCall).toBeDefined();
      expect(JSON.parse(String(secretCall?.[1]?.body ?? "{}"))).toEqual({
        name: "TELEGRAM_BOT_TOKEN",
        purpose: "Enable Telegram channel delivery.",
        value: "123456:ABC-super-secret",
        validate: true
      });
    });
    await waitFor(() => {
      expect(screen.getByText("secret://telegram_bot_token")).toBeInTheDocument();
      expect(screen.queryByText("123456:ABC-super-secret")).not.toBeInTheDocument();
    });
  });

  it("saves runtime settings through the persisted settings route", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    await screen.findByRole("heading", { name: /settings/i });
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "codex-cli" } });
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "gpt-5.4" } });
    fireEvent.change(screen.getByLabelText("Temperature"), { target: { value: "0.7" } });
    fireEvent.change(screen.getByLabelText("Max tool calls"), { target: { value: "12" } });
    fireEvent.click(screen.getByRole("button", { name: "Manual" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Stream responses" }));
    fireEvent.click(screen.getByRole("button", { name: "Memvid" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Command tools" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Web context" }));
    fireEvent.click(screen.getByRole("button", { name: /save settings/i }));

    await waitFor(() => {
      const saveCall = fetchSpy.mock.calls.find(([path, init]) => path === "/api/runtime/settings" && init?.method === "PUT");
      expect(saveCall).toBeDefined();
      const body = JSON.parse(String(saveCall?.[1]?.body ?? "{}"));
      expect(body).toMatchObject({
        expected_revision: "runtime-revision-1",
        provider: "codex-cli",
        model: "gpt-5.4",
        temperature: 0.7,
        max_tool_rounds: 12,
        backend: "memvid",
        memory_dir: "/tmp/memory",
        workspace: "/tmp/kestrel",
        stream: true,
        autonomy_mode: "manual",
        allow_shell: true,
        allow_web: true
      });
      expect(body.allow_file_write).toBe(false);
      expect(body.allow_codex_cli).toBe(false);
      expect(body).not.toHaveProperty("require_api_auth");
    });
    expect(await screen.findByText("Settings saved and applied to new runs.")).toBeInTheDocument();
  });

  it("renders guided Telegram channel setup and webhook controls", async () => {
    const fetchSpy = vi.mocked(fetch);
    channelsPayload = [
      {
        id: "telegram",
        provider: "telegram",
        enabled: true,
        send_enabled: true,
        auto_reply: true,
        token_env: "TELEGRAM_BOT_TOKEN",
        webhook_url_env: null,
        settings: {
          admin_enabled: true,
          owner_user_ids: ["777"],
          signature_secret_env: "TELEGRAM_WEBHOOK_SECRET"
        },
        env_status: {
          token_env_configured: true,
          signature_secret_env_configured: true
        }
      }
    ];

    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    await screen.findByRole("heading", { name: /settings/i });

    const card = await screen.findByRole("group", { name: /telegram setup/i });
    expect(within(card).getByText("owner 777")).toBeInTheDocument();
    expect(within(card).getByText("token configured")).toBeInTheDocument();
    expect(within(card).getByText("signature configured")).toBeInTheDocument();

    fireEvent.change(within(card).getByLabelText("Telegram public webhook URL"), {
      target: { value: "https://kestrel.example/api/channels/telegram/webhook?channel_id=telegram" }
    });
    fireEvent.click(within(card).getByRole("button", { name: /set webhook/i }));

    await waitFor(() => {
      const setupCall = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/channels/telegram/telegram/set-webhook" && init?.method === "POST"
      );
      expect(setupCall).toBeDefined();
      expect(JSON.parse(String(setupCall?.[1]?.body ?? "{}"))).toEqual({
        url: "https://kestrel.example/api/channels/telegram/webhook?channel_id=telegram",
        drop_pending_updates: false
      });
    });
  });

  it("reports skills discovery results instead of making discover look idle", async () => {
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    fireEvent.click(screen.getByRole("button", { name: "Discover" }));

    expect(await screen.findByText("No skill capsules found in .nest/skills.")).toBeInTheDocument();
    expect(screen.getByText(/"discovered_count": 0/)).toBeInTheDocument();
  });

  it("reviews plugins before install and surfaces enable blockers", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));

    fireEvent.change(screen.getByLabelText("GitHub source"), { target: { value: "owner/repo" } });
    fireEvent.click(screen.getByRole("button", { name: "Review" }));

    expect(await screen.findByText("Review: reviewed")).toBeInTheDocument();
    expect(screen.getByText(/python:requests>=2/)).toBeInTheDocument();
    expect(screen.getByText(/container required unavailable/)).toBeInTheDocument();
    expect(screen.getByText("plugin_dependencies_unmanaged")).toBeInTheDocument();
    expect(screen.getByText("plugin_isolation_unavailable")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /enable after install/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Install" })).toBeEnabled();

    await waitFor(() => {
      const reviewCall = fetchSpy.mock.calls.find(([path, init]) => path === "/api/plugins/review" && init?.method === "POST");
      expect(reviewCall).toBeDefined();
      expect(JSON.parse(String(reviewCall?.[1]?.body ?? "{}"))).toEqual({
        source: "owner/repo",
        ref: null
      });
    });
  });

  it("filters tool cards by name and enabled state", async () => {
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));

    const grid = await screen.findByLabelText("Tool cards");
    expect(within(grid).getByText("memory.search")).toBeInTheDocument();
    expect(within(grid).getByText("shell.run")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Filter tools"), { target: { value: "memory" } });
    expect(within(grid).getByText("memory.search")).toBeInTheDocument();
    expect(within(grid).queryByText("shell.run")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Filter tools"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Tool enabled state"), { target: { value: "disabled" } });
    expect(within(grid).getByText("shell.run")).toBeInTheDocument();
    expect(within(grid).getByText("web.search")).toBeInTheDocument();
    expect(within(grid).queryByText("memory.search")).not.toBeInTheDocument();
  });

  it("persists individual tool capability switches and removes disabled tools from invocation", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    const center = await screen.findByRole("region", { name: "Capabilities" });

    expect(within(center).getAllByText(/Blocked by:/).some((item) => item.parentElement?.textContent?.includes("allow shell"))).toBe(true);
    fireEvent.click(within(center).getByRole("switch", { name: "Disable Memory search" }));

    await waitFor(() => {
      const mutation = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/capabilities/tool/memory.search" && init?.method === "PUT"
      );
      expect(mutation).toBeDefined();
      expect(JSON.parse(String(mutation?.[1]?.body ?? "{}"))).toEqual({ enabled: false, expected_revision: 1 });
    });
    expect(await screen.findByText(/Memory search disabled for future invocations/i)).toBeInTheDocument();

    fireEvent.click(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("button", { name: "Advanced" }));
    const toolSelect = await screen.findByLabelText("Tool");
    expect(within(toolSelect).queryByRole("option", { name: "memory.search" })).not.toBeInTheDocument();
  });

  it("toggles MCP servers and skills from capabilities and exposes only effective invoke options", async () => {
    skillsPayload = [
      { id: "writer", name: "Writing skill", description: "Draft structured prose.", enabled: false }
    ];
    mcpServersPayload = [
      {
        id: "filesystem",
        name: "Filesystem MCP",
        transport: "stdio",
        command: "filesystem-mcp",
        enabled: false,
        tools: [
          {
            name: "mcp.filesystem.read_file",
            remote_name: "read_file",
            description: "Read an allowed file.",
            risk: "low",
            requires_approval: false,
            source: "mcp",
            enabled: false
          }
        ],
        status: "configured",
        session_state: "disconnected"
      }
    ];
    capabilitiesPayload = [
      ...capabilitiesPayload,
      capabilityFixture({
        key: "mcp_server:filesystem",
        kind: "mcp_server",
        id: "filesystem",
        name: "Filesystem MCP",
        description: "Filesystem MCP server.",
        configured_enabled: false,
        effective_enabled: false,
        risk: "medium",
        source: "mcp"
      }),
      capabilityFixture({
        key: "tool:mcp.filesystem.read_file",
        id: "mcp.filesystem.read_file",
        name: "Read file",
        description: "Read an allowed file.",
        configured_enabled: true,
        effective_enabled: false,
        risk: "low",
        source: "mcp",
        parent_key: "mcp_server:filesystem"
      }),
      capabilityFixture({
        key: "skill:writer",
        kind: "skill",
        id: "writer",
        name: "Writing skill",
        description: "Draft structured prose.",
        configured_enabled: false,
        effective_enabled: false,
        risk: "low",
        source: "skill"
      })
    ];

    render(<App />);
    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    expect(within(await screen.findByLabelText("MCP tool")).queryByRole("option", { name: /read_file/ })).not.toBeInTheDocument();
    expect(within(screen.getByLabelText("Skill")).queryByRole("option", { name: "writer" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    const center = await screen.findByRole("region", { name: "Capabilities" });
    fireEvent.click(within(center).getByRole("switch", { name: "Enable Filesystem MCP" }));
    await within(center).findByRole("switch", { name: "Disable Filesystem MCP" });
    fireEvent.click(within(center).getByRole("switch", { name: "Enable Writing skill" }));
    await within(center).findByRole("switch", { name: "Disable Writing skill" });

    fireEvent.click(within(screen.getByRole("navigation", { name: "Primary" })).getByRole("button", { name: "Advanced" }));
    expect(within(await screen.findByLabelText("MCP tool")).getByRole("option", { name: /read_file/ })).toBeInTheDocument();
    expect(within(screen.getByLabelText("Skill")).getByRole("option", { name: "writer" })).toBeInTheDocument();
  });

  it("does not overwrite hidden MCP arguments, environment, secrets, or discovered tools on edit", async () => {
    const fetchSpy = vi.mocked(fetch);
    mcpServersPayload = [
      {
        id: "filesystem",
        name: "Filesystem MCP",
        transport: "stdio",
        command: "filesystem-mcp",
        enabled: true,
        tools: [
          {
            name: "mcp.filesystem.read_file",
            remote_name: "read_file",
            description: "Read an allowed file.",
            risk: "low",
            requires_approval: false,
            source: "mcp"
          }
        ],
        status: "online",
        session_state: "connected",
        argument_count: 2,
        env_keys: ["FILESYSTEM_ROOT"],
        secret_env_status: { FILESYSTEM_TOKEN: { configured: true } }
      }
    ];
    capabilitiesPayload = [
      ...capabilitiesPayload,
      capabilityFixture({
        key: "mcp_server:filesystem",
        kind: "mcp_server",
        id: "filesystem",
        name: "Filesystem MCP",
        description: "Filesystem MCP server.",
        source: "mcp"
      })
    ];

    render(<App />);
    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Filesystem MCP" }));
    expect(screen.getByText("2 stored arguments are hidden. Edit to replace them.")).toBeInTheDocument();
    expect(screen.getByText("1 stored environment names are hidden. Edit to replace them.")).toBeInTheDocument();
    expect(screen.getByText("1 secret bindings are hidden. Edit to replace them.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save Server" }));

    await waitFor(() => {
      const update = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/mcp/servers/filesystem" && init?.method === "PUT"
      );
      expect(update).toBeDefined();
      const body = JSON.parse(String(update?.[1]?.body ?? "{}"));
      expect(body).not.toHaveProperty("tools");
      expect(body).not.toHaveProperty("args");
      expect(body).not.toHaveProperty("env");
      expect(body).not.toHaveProperty("secret_env");
    });
  });

  it("requires confirmation before enabling a high-risk capability", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    const center = await screen.findByRole("region", { name: "Capabilities" });
    fireEvent.click(within(center).getByRole("switch", { name: "Enable Shell run" }));

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("high-risk capability"));
    expect(fetchSpy.mock.calls.some(([path, init]) => path === "/api/capabilities/tool/shell.run" && init?.method === "PUT")).toBe(false);
  });

  it("reauthorizes a capability after its protected resource changes", async () => {
    const fetchSpy = vi.mocked(fetch);
    capabilitiesPayload = capabilitiesPayload.map((capability) =>
      capability.id === "memory.search"
        ? { ...capability, effective_enabled: false, blocked_by: ["resource_changed"] }
        : capability
    );
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    const center = await screen.findByRole("region", { name: "Capabilities" });
    fireEvent.click(within(center).getByRole("button", { name: "Reauthorize" }));

    await waitFor(() => {
      const mutation = fetchSpy.mock.calls.find(
        ([path, init]) => path === "/api/capabilities/tool/memory.search" && init?.method === "PUT"
      );
      expect(JSON.parse(String(mutation?.[1]?.body ?? "{}"))).toEqual({ enabled: true, expected_revision: 1 });
    });
    expect(await screen.findByText(/Memory search enabled for future invocations/i)).toBeInTheDocument();
  });

  it("refreshes authoritative capability state after a mutation conflict", async () => {
    capabilityMutationFailure = { status: 409, detail: "capability_revision_conflict" };
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    const center = await screen.findByRole("region", { name: "Capabilities" });
    fireEvent.click(within(center).getByRole("switch", { name: "Disable Memory search" }));

    expect(await screen.findByText("capability_revision_conflict")).toBeInTheDocument();
    expect(within(center).getByRole("switch", { name: "Disable Memory search" })).toBeChecked();
  });

  it("fetches provider model names when the provider changes", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    await screen.findByRole("heading", { name: /settings/i });
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "ollama-cloud" } });

    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(([path]) => path === "/api/runtime/models?provider=ollama-cloud")).toBe(true);
    });
    expect(screen.getByDisplayValue("gpt-oss:120b")).toBeInTheDocument();
    expect(screen.getByText("2 provider models")).toBeInTheDocument();
  });

  it("offers friendly local and cloud providers and stores provider keys through the broker", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    await screen.findByRole("heading", { name: /settings/i });

    expect(screen.getByRole("option", { name: /LM Studio/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Ollama \(local\)/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Claude \/ Anthropic/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Grok \/ xAI/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Gemini/i })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "lm-studio" } });
    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(([path]) => path === "/api/runtime/models?provider=lm-studio")).toBe(true);
    });
    expect(screen.getByDisplayValue("http://localhost:1234/v1")).toBeInTheDocument();
    expect(screen.getByDisplayValue("local-model")).toBeInTheDocument();
    expect(screen.getByText("No key needed")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "grok" } });
    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(([path]) => path === "/api/runtime/models?provider=grok")).toBe(true);
    });
    expect(screen.getByDisplayValue("https://api.x.ai/v1")).toBeInTheDocument();
    expect(screen.getByDisplayValue("XAI_API_KEY")).toBeInTheDocument();
    expect(screen.getByDisplayValue("grok-4.3")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Provider API key"), { target: { value: "xai-super-secret" } });
    fireEvent.click(screen.getByRole("button", { name: /store provider key/i }));

    await waitFor(() => {
      const secretCall = fetchSpy.mock.calls.find(([path, init]) => path === "/api/secrets" && init?.method === "POST");
      expect(secretCall).toBeDefined();
      expect(JSON.parse(String(secretCall?.[1]?.body ?? "{}"))).toEqual({
        name: "XAI_API_KEY",
        purpose: "Enable Grok / xAI as an LLM provider.",
        value: "xai-super-secret",
        validate: true
      });
    });
    expect(screen.queryByText("xai-super-secret")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /save settings/i }));
    await waitFor(() => {
      const saveCall = fetchSpy.mock.calls.find(([path, init]) => path === "/api/runtime/settings" && init?.method === "PUT");
      expect(saveCall).toBeDefined();
      expect(JSON.parse(String(saveCall?.[1]?.body ?? "{}"))).toMatchObject({
        provider: "grok",
        model: "grok-4.3",
        base_url: "https://api.x.ai/v1",
        api_key_env: "XAI_API_KEY"
      });
    });
  });

  it("replaces an incompatible model when switching friendly providers", async () => {
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    await screen.findByRole("heading", { name: /settings/i });

    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "deepseek-v4-pro" } });
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "grok" } });

    expect(await screen.findByDisplayValue("grok-4.3")).toBeInTheDocument();
  });

  it("renders behavior delta review panel from the ledger API", async () => {
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /advanced/i }));

    const panel = await screen.findByLabelText("Behavior Deltas Review");
    expect(within(panel).getByText("Behavior Deltas Review")).toBeInTheDocument();
    expect(within(panel).getByText("Policy-safe workflow")).toBeInTheDocument();
    expect(within(panel).getByText("delta_policy_gate_check")).toBeInTheDocument();
    expect(within(panel).getByText("active · policy · high")).toBeInTheDocument();
    expect(within(panel).getAllByText("1 activations").length).toBeGreaterThan(0);
    expect(within(panel).getByText("Useful 100% · Failure 0% · Rollback 0%")).toBeInTheDocument();
    expect(within(panel).getByText("Mutation actions require exact-call approval and MutationGate review."));
  });

  it("runs the setup wizard and saves onboarding to Soul memory", async () => {
    const fetchSpy = vi.mocked(fetch);
    onboardingProfile = null;
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Meet your Kestrel" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "First-run readiness" })).toBeInTheDocument();
    expect(screen.getByText("5 pass · 2 warn · 1 fail")).toBeInTheDocument();
    expect(screen.getByText("Provider configuration")).toBeInTheDocument();
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.getByText("Fix failing setup checks before starting the golden local workflow.")).toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([path]) => path === "/api/product/setup")).toBe(true);
    fireEvent.change(screen.getByLabelText("Agent name"), { target: { value: "Northstar" } });
    fireEvent.change(screen.getByLabelText("Your name"), { target: { value: "Taylor" } });
    fireEvent.change(screen.getByLabelText("What should it call you?"), { target: { value: "Tay" } });
    fireEvent.click(screen.getByRole("radio", { name: /creative spark/i }));
    fireEvent.change(screen.getByLabelText("What are you usually trying to get done?"), {
      target: { value: "Build Kestrel\nDesign local tools" }
    });
    fireEvent.change(screen.getByLabelText("How do you like collaboration to feel?"), {
      target: { value: "Short plans, direct tradeoffs, live verification." }
    });
    fireEvent.change(screen.getByLabelText("Interests or recurring themes"), {
      target: { value: "Local-first software, thoughtful UI" }
    });
    fireEvent.change(screen.getByLabelText("Anything else it should remember?"), {
      target: { value: "Warm but concrete." }
    });
    fireEvent.click(screen.getByRole("button", { name: /save to soul/i }));

    await waitFor(() => {
      const setupCall = fetchSpy.mock.calls.find(([path, init]) => path === "/api/self/onboarding" && init?.method === "POST");
      expect(setupCall).toBeDefined();
      const body = JSON.parse(String(setupCall?.[1]?.body ?? "{}"));
      expect(body).toMatchObject({
        agent_name: "Northstar",
        user_name: "Taylor",
        preferred_name: "Tay",
        persona: "spark",
        working_style: "Short plans, direct tradeoffs, live verification.",
        communication_notes: "Warm but concrete.",
        continuous_learning: true
      });
      expect(body.goals).toEqual(["Build Kestrel", "Design local tools"]);
      expect(body.interests).toEqual(["Local-first software", "thoughtful UI"]);
    });
    expect(await screen.findByText("Setup saved to Soul memory.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Meet your Kestrel" })).not.toBeInTheDocument();
  });

  it("prompts for an API token after a 401 and sends it on later API calls", async () => {
    const fetchSpy = vi.mocked(fetch);
    fetchSpy.mockImplementationOnce(async () => jsonResponse({ detail: "Invalid or missing Kestrel API token." }, 401));
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Kestrel API token" })).toBeInTheDocument();
    expect(screen.queryByText("Action failed")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("API token"), { target: { value: "browser-token" } });
    fireEvent.click(screen.getByRole("button", { name: /save token/i }));

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    await waitFor(() => {
      expect(
        fetchSpy.mock.calls.some(([, init]) => {
          const headers = init?.headers as Record<string, string> | undefined;
          return headers?.Authorization === "Bearer browser-token";
        })
      ).toBe(true);
    });
  });
});

class MockEventSource {
  onmessage: ((event: MessageEvent) => void) | null = null;
  private listeners = new Map<string, Array<(event: MessageEvent) => void>>();

  constructor(readonly url: string) {
    eventSources.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close = vi.fn();

  emit(type: string, payload: Record<string, unknown>) {
    const event: TraceEvent = {
      id: eventId++,
      run_id: "run_1",
      type,
      payload,
      created_at: "2026-05-16T00:00:02Z"
    };
    const message = new MessageEvent(type, { data: JSON.stringify(event) });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(message);
    }
  }
}

async function waitForEventSource(): Promise<MockEventSource> {
  await waitFor(() => expect(eventSources[0]).toBeDefined());
  return eventSources[0];
}

async function fetchMock(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const raw = typeof input === "string" || input instanceof URL ? input.toString() : input.url;
  const url = new URL(raw, "http://kestrel.test");
  const path = `${url.pathname}${url.search}`;
  if (path === "/api/runs" && init?.method === "POST") {
    const body = JSON.parse(String(init.body ?? "{}"));
    const run: Run = {
      ...baseRun,
      ...body,
      run_id: "run_created",
      status: "queued",
      assistant_message: "",
      stop_reason: "",
      provider: body.provider,
      model: body.model ?? "mock",
      created_at: "2026-05-16T00:10:00Z",
      updated_at: "2026-05-16T00:10:00Z"
    };
    runs = [run, ...runs];
    sessionRuns[run.session_id] = [...(sessionRuns[run.session_id] ?? []), run];
    sessions = [
      {
        session_id: run.session_id,
        run_count: sessionRuns[run.session_id].length,
        status_counts: { queued: 1 },
        latest_run_id: run.run_id,
        latest_status: run.status,
        latest_message: run.message,
        created_at: run.created_at,
        updated_at: run.updated_at
      },
      ...sessions.filter((session) => session.session_id !== run.session_id)
    ];
    return jsonResponse(run);
  }
  if (path === "/api/routines/status" && routineLoadFailure) {
    return jsonResponse({ detail: routineLoadFailure }, 503);
  }
  const routineHistoryMatch = path.match(/^\/api\/routines\/([^/]+)\/history\?limit=50$/);
  if (routineHistoryMatch) {
    if (routineHistoryFailure) return jsonResponse({ detail: routineHistoryFailure }, 503);
    return jsonResponse(routineHistories[decodeURIComponent(routineHistoryMatch[1])] ?? []);
  }
  const routineRunNowMatch = path.match(/^\/api\/routines\/([^/]+)\/actions\/run-now$/);
  if (routineRunNowMatch && init?.method === "POST") {
    const routineId = decodeURIComponent(routineRunNowMatch[1]);
    const body = JSON.parse(String(init.body ?? "{}"));
    const requestKey = `${routineId}:${String(body.idempotency_key)}`;
    const existing = routineRunNowAccepted.get(requestKey);
    const occurrence: RoutineOccurrence = existing ?? {
      ...baseRoutineOccurrence,
      occurrence_id: `occ_manual_${routineId}_${routineRunNowAccepted.size + 1}`,
      routine_id: routineId,
      routine_revision: body.expected_revision,
      scheduled_for: "2026-05-17T14:00:00Z",
      run_id: `run_manual_${routineId}_${routineRunNowAccepted.size + 1}`,
      status: routineRunNowInitialStatus,
      trigger_kind: "manual",
      requested_at: "2026-05-17T14:00:00Z",
      created_at: "2026-05-17T14:00:00Z",
      updated_at: "2026-05-17T14:00:02Z"
    };
    if (!existing) {
      routineRunNowAccepted.set(requestKey, occurrence);
      routineHistories[routineId] = [occurrence, ...(routineHistories[routineId] ?? [])];
    }
    if (routineRunNowAmbiguousFailures > 0) {
      routineRunNowAmbiguousFailures -= 1;
      throw new TypeError("Failed to fetch");
    }
    return jsonResponse({
      requested_at: occurrence.requested_at,
      claim_owner: "routine-test-owner",
      idempotent_replay: Boolean(existing),
      occurrence,
      dispatch: existing ? null : {
        occurrence_id: occurrence.occurrence_id,
        routine_id: routineId,
        run_id: occurrence.run_id,
        status: occurrence.status,
        error: null
      }
    });
  }
  if (path === "/api/routines" && init?.method === "POST") {
    const body = JSON.parse(String(init.body ?? "{}"));
    const routine: Routine = {
      ...baseRoutine,
      ...body,
      routine_id: "routine_created",
      enabled: false,
      revision: 1,
      next_run_at: body.start_at,
      created_at: "2026-05-17T14:00:00Z",
      updated_at: "2026-05-17T14:00:00Z"
    };
    routinesPayload = [routine, ...routinesPayload];
    routineHistories[routine.routine_id] = [];
    return jsonResponse(routine);
  }
  const routineEnabledMatch = path.match(/^\/api\/routines\/([^/]+)\/enabled$/);
  if (routineEnabledMatch && init?.method === "PUT") {
    const routineId = decodeURIComponent(routineEnabledMatch[1]);
    const body = JSON.parse(String(init.body ?? "{}"));
    const current = routinesPayload.find((routine) => routine.routine_id === routineId);
    if (!current) return jsonResponse({ detail: "routine_not_found" }, 404);
    if (body.expected_revision !== current.revision) return jsonResponse({ detail: "routine_revision_conflict" }, 409);
    const saved = { ...current, enabled: body.enabled, revision: current.revision + 1 };
    routinesPayload = routinesPayload.map((routine) => routine.routine_id === routineId ? saved : routine);
    return jsonResponse(saved);
  }
  const routineMutationMatch = path.match(/^\/api\/routines\/([^/?]+)(?:\?expected_revision=(\d+))?$/);
  if (routineMutationMatch && init?.method === "PUT") {
    const routineId = decodeURIComponent(routineMutationMatch[1]);
    const body = JSON.parse(String(init.body ?? "{}"));
    const current = routinesPayload.find((routine) => routine.routine_id === routineId);
    if (!current) return jsonResponse({ detail: "routine_not_found" }, 404);
    if (body.expected_revision !== current.revision) return jsonResponse({ detail: "routine_revision_conflict" }, 409);
    const { expected_revision: _expectedRevision, ...changes } = body;
    const saved = { ...current, ...changes, revision: current.revision + 1 };
    routinesPayload = routinesPayload.map((routine) => routine.routine_id === routineId ? saved : routine);
    return jsonResponse(saved);
  }
  if (routineMutationMatch && init?.method === "DELETE") {
    const routineId = decodeURIComponent(routineMutationMatch[1]);
    const expectedRevision = Number(routineMutationMatch[2]);
    const current = routinesPayload.find((routine) => routine.routine_id === routineId);
    if (!current) return jsonResponse({ detail: "routine_not_found" }, 404);
    if (expectedRevision !== current.revision) return jsonResponse({ detail: "routine_revision_conflict" }, 409);
    routinesPayload = routinesPayload.filter((routine) => routine.routine_id !== routineId);
    return jsonResponse({ ...current, deleted_at: "2026-05-17T14:00:00Z", revision: current.revision + 1 });
  }
  if (path.match(/^\/api\/approvals\/approval_1\/decision$/) && init?.method === "POST") {
    approvals = [];
    return jsonResponse({ ...pendingApproval, status: "approved", decision: JSON.parse(String(init.body ?? "{}")) });
  }
  if (path === "/api/tools/git.commit/invoke" && init?.method === "POST") {
    const body = JSON.parse(String(init.body ?? "{}"));
    const approval: Approval = {
      approval_id: "approval_repair_commit",
      run_id: body.run_id,
      tool_call_id: "tool_repair_commit",
      tool_name: "git.commit",
      arguments: body.arguments,
      risk: "high",
      status: "pending",
      decision: null,
      result: null,
      created_at: "2026-05-16T00:21:00Z",
      updated_at: "2026-05-16T00:21:00Z"
    };
    approvals = [approval];
    return jsonResponse({
      tool: "git.commit",
      tool_call_id: approval.tool_call_id,
      success: false,
      content: "Approval required for git.commit.",
      data: { approval_id: approval.approval_id, status: "pending" },
      error: "approval_required"
    });
  }
  if (path === "/api/tools/repair.rollback/invoke" && init?.method === "POST") {
    const body = JSON.parse(String(init.body ?? "{}"));
    const approval: Approval = {
      approval_id: "approval_repair_rollback",
      run_id: body.run_id,
      tool_call_id: "tool_repair_rollback",
      tool_name: "repair.rollback",
      arguments: body.arguments,
      risk: "high",
      status: "pending",
      decision: null,
      result: null,
      created_at: "2026-05-16T00:26:00Z",
      updated_at: "2026-05-16T00:26:00Z"
    };
    approvals = [approval];
    return jsonResponse({
      tool: "repair.rollback",
      tool_call_id: approval.tool_call_id,
      success: false,
      content: "Approval required for repair.rollback.",
      data: { approval_id: approval.approval_id, status: "pending" },
      error: "approval_required"
    });
  }
  if (path === "/api/secrets" && init?.method === "POST") {
    const body = JSON.parse(String(init.body ?? "{}"));
    const secretId = String(body.name ?? "secret").toLowerCase().replace(/[^a-z0-9_.-]+/g, "_");
    const secret: SecretRef = {
      id: secretId,
      name: body.name,
      purpose: body.purpose,
      secret_ref: `secret://${secretId}`,
      configured: true,
      validated: Boolean(body.validate),
      last_validated_at: "2026-05-16T00:10:00Z",
      fingerprint: "sha256:abc123",
      created_at: "2026-05-16T00:10:00Z",
      updated_at: "2026-05-16T00:10:00Z",
      source: "broker"
    };
    secrets = [secret];
    return jsonResponse(secret);
  }
  const mcpUpdateMatch = path.match(/^\/api\/mcp\/servers\/([^/]+)$/);
  if (mcpUpdateMatch && init?.method === "PUT") {
    const serverId = decodeURIComponent(mcpUpdateMatch[1]);
    const body = JSON.parse(String(init.body ?? "{}"));
    const current = mcpServersPayload.find((server) => server.id === serverId);
    if (!current) return jsonResponse({ detail: "mcp_server_not_found" }, 404);
    const saved = { ...current, ...body, id: serverId };
    mcpServersPayload = mcpServersPayload.map((server) => server.id === serverId ? saved : server);
    return jsonResponse(saved);
  }
  const capabilityMatch = path.match(/^\/api\/capabilities\/(tool|mcp_server|skill)\/(.+)$/);
  if (capabilityMatch && init?.method === "PUT") {
    if (capabilityMutationFailure) {
      const failure = capabilityMutationFailure;
      capabilityMutationFailure = null;
      return jsonResponse({ detail: failure.detail }, failure.status);
    }
    const kind = capabilityMatch[1];
    const id = decodeURIComponent(capabilityMatch[2]);
    const body = JSON.parse(String(init.body ?? "{}"));
    const index = capabilitiesPayload.findIndex((capability) => capability.kind === kind && capability.id === id);
    if (index < 0) return jsonResponse({ detail: "capability_not_found" }, 404);
    const current = capabilitiesPayload[index];
    if (body.expected_revision !== current.revision) {
      return jsonResponse({ detail: "capability_revision_conflict" }, 409);
    }
    const enabled = Boolean(body.enabled);
    const blockedBy = enabled
      ? current.blocked_by.filter((blocker) => blocker !== "resource_changed")
      : current.blocked_by;
    const capability: Capability = {
      ...current,
      configured_enabled: enabled,
      effective_enabled: enabled && blockedBy.length === 0,
      blocked_by: blockedBy,
      revision: current.revision + 1,
      updated_at: "2026-05-16T00:10:00Z"
    };
    capabilitiesPayload = capabilitiesPayload.map((item) => item.key === capability.key ? capability : item);
    if (capability.kind === "mcp_server") {
      capabilitiesPayload = capabilitiesPayload.map((item) =>
        item.parent_key === capability.key
          ? { ...item, effective_enabled: enabled && item.configured_enabled && item.blocked_by.length === 0, revision: item.revision + 1 }
          : item
      );
    }
    return jsonResponse({ capability, revoked_approvals: 0, applies_to: "future_invocations" });
  }
  if (path === "/api/runtime/settings" && init?.method === "PUT") {
    const body = JSON.parse(String(init.body ?? "{}"));
    const { expected_revision: _expectedRevision, ...changes } = body;
    return jsonResponse({
      settings: {
        ...changes,
        revision: "runtime-revision-2",
        updated_at: "2026-05-16T00:10:00Z",
        path: ".nest/config/runtime_settings.json",
        persisted: true
      },
      runtime: changes
    });
  }
  const telegramWebhookMatch = path.match(/^\/api\/channels\/([^/]+)\/telegram\/(webhook-info|set-webhook|delete-webhook|test-message)$/);
  if (telegramWebhookMatch) {
    return jsonResponse({
      ok: true,
      channel_id: decodeURIComponent(telegramWebhookMatch[1]),
      method: telegramWebhookMatch[2],
      delivery: { sent: true, request_json: {} }
    });
  }
  if (path === "/api/self/onboarding" && init?.method === "POST") {
    const body = JSON.parse(String(init.body ?? "{}"));
    onboardingProfile = {
      schema_version: "kestrel_onboarding_profile.v1",
      setup_complete: true,
      agent_name: body.agent_name,
      user_name: body.user_name,
      preferred_name: body.preferred_name,
      persona: body.persona,
      persona_name: body.persona === "spark" ? "Creative Spark" : "Steady Companion",
      persona_summary: "Saved persona",
      persona_guidance: "Saved guidance",
      working_style: body.working_style,
      goals: body.goals,
      interests: body.interests,
      communication_notes: body.communication_notes,
      continuous_learning: body.continuous_learning,
      updated_at: "2026-05-16T00:10:00Z"
    };
    return jsonResponse({
      success: true,
      profile: onboardingProfile,
      personas: personaPayload(),
      memory: { success: true, data: { record_id: "self_profile_1" } }
    });
  }
  if (path === "/api/skills/discover" && init?.method === "POST") {
    return jsonResponse({
      skills: skillsPayload,
      discovered_count: 0,
      enabled_count: 0,
      skills_dir: ".nest/skills",
      validation_errors: [],
      message: "No skill capsules found in .nest/skills."
    });
  }
  if (path === "/api/plugins/review" && init?.method === "POST") {
    return jsonResponse({
      source_url: "https://github.com/owner/repo",
      source_ref: null,
      commit_sha: "a".repeat(40),
      manifest: { id: "reviewed", name: "Reviewed Plugin" },
      capabilities: ["plugin", "skill"],
      risk_report: {
        enable_blockers: ["plugin_dependencies_unmanaged", "plugin_isolation_unavailable"]
      },
      dependency_review: {
        declared: { python: ["requests>=2"], node: [], system: [] },
        requires_install: true,
        managed: false,
        status: "unmanaged"
      },
      isolation_review: { mode: "container", required: true, available: false },
      enable_blockers: ["plugin_dependencies_unmanaged", "plugin_isolation_unavailable"],
      warnings: [],
      unsupported_features: []
    });
  }
  return jsonResponse(payloadFor(path));
}

function payloadFor(path: string): unknown {
  if (path === "/api/runs") return runs;
  if (path === "/api/routines/status") return routineStatusPayload;
  if (path === "/api/routines") return routinesPayload;
  if (path === "/api/sessions") return sessions;
  const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs$/);
  if (sessionMatch) return sessionRuns[decodeURIComponent(sessionMatch[1])] ?? [];
  if (path === "/api/approvals?status=pending") return approvals;
  if (path === "/api/approvals") return approvals;
  if (path === "/api/tools") return toolsPayload;
  if (path === "/api/capabilities") return capabilitySnapshotFixture(capabilitiesPayload);
  if (path === "/api/mcp/servers") return mcpServersPayload;
  if (path === "/api/skills") return skillsPayload;
  if (path === "/api/plugins") return [];
  if (path === "/api/channels") return channelsPayload;
  if (path === "/api/secrets") return secrets;


  if (path === "/api/learning/dashboard?since=all") {
    return {
      since: null,
      headline: {
        auto_activations: 1,
        rollbacks: 0,
        false_positive_rate: 0,
        activations_then_rolled_back: 0,
        average_time_to_rollback_hours: null
      },
      layers: [
        {
          layer: "procedural",
          activations: 1,
          auto_activations: 1,
          rollbacks: 0,
          false_positive_rate: 0,
          activations_then_rolled_back: 0,
          average_time_to_rollback_hours: null
        }
      ]
    };
  }

  if (path === "/api/memory/deltas?since=all") {
    return {
      summary: {
        total_deltas: 1,
        active_deltas: 1,
        activated_deltas: 1,
        never_activated: 0,
        useful_rate: 1,
        failure_rate: 0,
        rollback_rate: 0,
        never_activated_rate: 0,
        outcomes: { useful: 1, caused_failure: 0, contradicted: 0, rolled_back: 0 }
      },
      deltas: [
        {
          delta_id: "delta_policy_gate_check",
          title: "Policy-safe workflow",
          kind: "policy",
          target_layer: "policy",
          risk: "high",
          status: "active",
          activation_count: 1,
          outcome_counts: { useful: 1, caused_failure: 0, contradicted: 0, rolled_back: 0 },
          useful_rate: 1,
          failure_rate: 0,
          rollback_rate: 0,
          never_activated: false,
          last_activated_at: "2026-05-20T00:00:00Z",
          last_outcome_at: "2026-05-20T00:01:00Z"
        }
      ],
      recommendations: []
    };
  }
  if (path === "/api/memory/layers") return [
    { layer: "working", path: "/tmp/working.mv2", exists: true, ok: true, backend: "InMemoryBackend" },
    { layer: "self", path: "/tmp/self.mv2", exists: true, ok: true, backend: "InMemoryBackend" }
  ];
  if (path === "/api/self") {
    return {
      identity: {
        name: "Kestrel",
        display_name: "Soul",
        description: "A local-first, memory-native engineering agent runtime."
      },
      provider: { provider: "mock", model: "mock", api_key_env: null, api_key_configured: false },
      config: { allow_self_modification: false, allow_web: false },
      memory_layers: [{ layer: "self", mv2_file: "self.mv2" }],
      tools: [],
      skills: [],
      plugins: [],
      mcp_servers: []
    };
  }
  if (path === "/api/self/onboarding") {
    return {
      completed: Boolean(onboardingProfile),
      profile: onboardingProfile,
      personas: personaPayload(),
      reflection: onboardingProfile ? "Relevant Soul/self memory: Kestrel onboarding profile" : "No validated Soul/self memory matched the query yet."
    };
  }
  if (path === "/api/product/setup") {
    return {
      schema: "kestrel.setup_readiness.v1",
      ready: false,
      pass_count: 5,
      warn_count: 2,
      fail_count: 1,
      next_action: "Fix failing setup checks before starting the golden local workflow.",
      checks: [
        {
          check_id: "provider_configuration",
          title: "Provider configuration",
          status: "pass",
          detail: "Mock provider is selected, so deterministic first-run smoke tests can run without credentials.",
          recovery: "Choose a live provider later and rerun setup readiness before claiming provider support."
        },
        {
          check_id: "workspace",
          title: "Workspace",
          status: "fail",
          detail: "Workspace `/tmp/missing` does not exist.",
          recovery: "Create the workspace or pass `--workspace` pointing at the repo/project Kestrel should operate on."
        },
        {
          check_id: "memory_storage",
          title: "Memory storage",
          status: "warn",
          detail: "Memory directory is not present yet. Path: `/tmp/memory`.",
          recovery: "Run `nest-agent init` or start a local run so Kestrel can initialize memory layers."
        }
      ]
    };
  }
  if (path === "/api/runtime/config") {
    return {
      name: "Kestrel",
      version: "0.1.0",
      schema_version: 9,
      provider: { name: "mock", model: "mock", api_key_env: null, api_key_configured: false },
      feature_flags: {
        enable_autonomous_scheduler: false,
        require_approval_for_high_risk_tools: true,
        allow_shell: false,
        allow_file_write: false,
        allow_policy_writes: false
      },
      limits: { max_tool_rounds: 6 },
      paths: { workspace: "/tmp/kestrel", memory_dir: "/tmp/memory" },
      settings: {
        runtime: {
          provider: "mock",
          model: "mock",
          backend: "memory",
          memory_dir: "/tmp/memory",
          workspace: "/tmp/kestrel",
          stream: false,
          require_api_auth: false,
          autonomy_mode: "background",
          revision: "runtime-revision-1",
          persisted: false
        }
      },
      validation_commands: ["python -m pytest -q"]
    };
  }
  const modelCatalogMatch = path.match(/^\/api\/runtime\/models\?provider=([^&]+)$/);
  if (modelCatalogMatch) {
    const provider = decodeURIComponent(modelCatalogMatch[1]);
    const modelsByProvider: Record<string, string[]> = {
      mock: ["mock"],
      openai: ["gpt-5.5", "gpt-5.4"],
      "lm-studio": ["local-model"],
      "openai-compatible": ["local-model"],
      openrouter: ["openai/gpt-5.5", "anthropic/claude-sonnet-4.5"],
      deepseek: ["deepseek-v4-pro", "deepseek-v4-flash"],
      kimi: ["kimi-k2.6", "kimi-k2.5"],
      ollama: ["llama3.1", "qwen2.5-coder"],
      "ollama-cloud": ["gpt-oss:120b", "gpt-oss:20b"],
      anthropic: ["claude-sonnet-4.5"],
      grok: ["grok-4.3", "grok-build-0.1"],
      gemini: ["gemini-2.5-pro"],
      "codex-cli": ["gpt-5.5", "gpt-5.4"]
    };
    return {
      provider,
      models: modelsByProvider[provider] ?? [],
      fallback_models: modelsByProvider[provider] ?? [],
      source: provider === "mock" || provider === "codex-cli" ? "static" : "provider",
      ok: true,
      fetchable: provider !== "mock" && provider !== "codex-cli",
      error: null,
      base_url_configured: false,
      api_key_env: apiKeyEnvForProvider(provider),
      api_key_configured: !["ollama-cloud", "deepseek", "kimi"].includes(provider),
      fetched_at: "2026-05-17T00:00:00Z"
    };
  }
  if (path === "/api/runtime/models") return { providers: [] };
  if (path === "/api/logs?limit=120") return [];
  if (path === "/api/cognition/lessons?k=20") return { items: [] };
  if (path === "/api/cognition/failures?k=20") return { items: [] };
  const graphMatch = path.match(/^\/api\/runs\/(run_[a-z0-9]+)\/task-graph$/);
  if (graphMatch) {
    return taskGraphs[graphMatch[1]] ?? { tasks: [], ready_tasks: [], approval_blocked_tasks: [], subagents: [] };
  }
  const traceMatch = path.match(/^\/api\/runs\/([^/]+)\/trace\?limit=700$/);
  if (traceMatch) {
    const runId = traceMatch[1];
    const run = runs.find((item) => item.run_id === runId) ?? baseRun;
    const timeline = traceTimelines[runId] ?? [];
    return {
      run,
      summary: {
        event_count: timeline.length,
        span_count: 0,
        first_event_at: timeline[0]?.created_at ?? run.created_at,
        last_event_at: timeline[timeline.length - 1]?.created_at ?? run.updated_at,
        trace_counts: { lifecycle: 1 }
      },
      timeline,
      traces: { lifecycle: [] }
    };
  }
  return {};
}

function apiKeyEnvForProvider(provider: string): string | null {
  const apiKeyEnvs: Record<string, string> = {
    openai: "OPENAI_API_KEY",
    openrouter: "OPENROUTER_API_KEY",
    "ollama-cloud": "OLLAMA_API_KEY",
    anthropic: "ANTHROPIC_API_KEY",
    grok: "XAI_API_KEY",
    gemini: "GEMINI_API_KEY",
    deepseek: "DEEPSEEK_API_KEY",
    kimi: "MOONSHOT_API_KEY"
  };
  return apiKeyEnvs[provider] ?? null;
}

function capabilityFixture(
  overrides: Partial<Capability> & Pick<Capability, "key" | "id" | "name">
): Capability {
  return {
    kind: "tool",
    description: "Test capability.",
    default_enabled: true,
    configured_enabled: true,
    effective_enabled: true,
    blocked_by: [],
    revision: 1,
    risk: "low",
    requires_approval: false,
    source: "builtin",
    ...overrides
  };
}

function capabilitySnapshotFixture(items: Capability[]) {
  return {
    items,
    counts: {
      total: items.length,
      configured_enabled: items.filter((item) => item.configured_enabled).length,
      effective_enabled: items.filter((item) => item.effective_enabled).length,
      blocked: items.filter((item) => item.blocked_by.length > 0).length
    }
  };
}

function personaPayload() {
  return [
    {
      id: "steady",
      name: "Steady Companion",
      summary: "Warm, grounded, concise, and quietly capable.",
      guidance: "Be warm and direct."
    },
    {
      id: "mentor",
      name: "Patient Mentor",
      summary: "Explains reasoning, teaches patterns, and checks understanding without dragging.",
      guidance: "Be patient and instructional."
    },
    {
      id: "spark",
      name: "Creative Spark",
      summary: "More playful, imaginative, and idea-forward while staying useful.",
      guidance: "Bring more creative options."
    },
    {
      id: "operator",
      name: "Calm Operator",
      summary: "Precise, terse, and technical for focused execution.",
      guidance: "Be crisp and operational."
    }
  ];
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
