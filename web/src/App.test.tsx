import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { Approval, Run, SecretRef, Session, TraceEvent } from "./types";

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

let runs: Run[];
let sessions: Session[];
let sessionRuns: Record<string, Run[]>;
let approvals: Approval[];
let secrets: SecretRef[];
let eventSources: MockEventSource[];
let eventId: number;

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
    secrets = [];
    eventSources = [];
    eventId = 1;
    vi.stubGlobal("fetch", vi.fn(fetchMock));
    vi.stubGlobal("EventSource", MockEventSource);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("opens on a conversation-first workspace with Advanced available", async () => {
    const { container } = render(<App />);

    expect(await screen.findByRole("heading", { name: "Ask Kestrel" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /new chat/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
    expect(screen.getByText("Safe Auto")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /advanced/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /inspector/i })).toBeInTheDocument();

    const results = await axe.run(container, {
      rules: {
        "color-contrast": { enabled: false }
      }
    });
    expect(results.violations).toEqual([]);
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
      expect(body.autonomy_mode).toBe("background");
      expect(body).not.toHaveProperty("provider");
      expect(body).not.toHaveProperty("model");
    });
  });

  it("switches threads and renders active session history in chronological order", async () => {
    render(<App />);

    await screen.findByText("Run tests");
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

    await screen.findByText("Inspect the repo");
    eventSources[0].emit("context.compile", { query: "repo" });
    eventSources[0].emit("assistant.token", { content: "Streaming answer" });
    expect(await screen.findByText("Gathering context")).toBeInTheDocument();
    expect(await screen.findByText("Streaming answer")).toBeInTheDocument();

    sessionRuns.session_1 = [{ ...sessionRuns.session_1[0], status: "completed", assistant_message: "Streaming answer", stop_reason: "completed" }];
    runs = [sessionRuns.session_1[0], otherRun];
    eventSources[0].emit("run.completed", {});

    await waitFor(() => {
      const transcript = screen.getByLabelText("Conversation transcript");
      expect(transcript).toHaveTextContent("Streaming answer");
      expect(transcript).not.toHaveTextContent("Streaming answerStreaming answer");
    });
  });

  it("renders inline approvals for the active thread and preserves exact-call decision arguments", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByText("Needs approval");
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
  if (path.match(/^\/api\/approvals\/approval_1\/decision$/) && init?.method === "POST") {
    approvals = [];
    return jsonResponse({ ...pendingApproval, status: "approved", decision: JSON.parse(String(init.body ?? "{}")) });
  }
  if (path === "/api/secrets" && init?.method === "POST") {
    const body = JSON.parse(String(init.body ?? "{}"));
    const secret: SecretRef = {
      id: "telegram_bot_token",
      name: body.name,
      purpose: body.purpose,
      secret_ref: "secret://telegram_bot_token",
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
  return jsonResponse(payloadFor(path));
}

function payloadFor(path: string): unknown {
  if (path === "/api/runs") return runs;
  if (path === "/api/sessions") return sessions;
  const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs$/);
  if (sessionMatch) return sessionRuns[decodeURIComponent(sessionMatch[1])] ?? [];
  if (path === "/api/approvals?status=pending") return approvals;
  if (path === "/api/approvals") return approvals;
  if (path === "/api/tools") return [];
  if (path === "/api/mcp/servers") return [];
  if (path === "/api/skills") return [];
  if (path === "/api/plugins") return [];
  if (path === "/api/channels") return [];
  if (path === "/api/secrets") return secrets;
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
  if (path === "/api/runtime/config") {
    return {
      name: "Nested MV2 Agent",
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
      validation_commands: ["python -m pytest -q"]
    };
  }
  if (path === "/api/logs?limit=120") return [];
  if (path === "/api/cognition/lessons?k=20") return { items: [] };
  if (path === "/api/cognition/failures?k=20") return { items: [] };
  if (path.match(/^\/api\/runs\/run_[a-z0-9]+\/task-graph$/)) {
    return { tasks: [], ready_tasks: [], approval_blocked_tasks: [], subagents: [] };
  }
  if (path.match(/^\/api\/runs\/run_[a-z0-9]+\/trace\?limit=700$/)) {
    return {
      run: runs.find((item) => path.includes(item.run_id)) ?? baseRun,
      summary: {
        event_count: 1,
        span_count: 0,
        first_event_at: baseRun.created_at,
        last_event_at: baseRun.updated_at,
        trace_counts: { lifecycle: 1 }
      },
      timeline: [
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
      ],
      traces: { lifecycle: [] }
    };
  }
  return {};
}

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
}
