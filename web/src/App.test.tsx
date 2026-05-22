import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { Approval, Run, SecretRef, Session, Skill, Tool, TraceEvent } from "./types";

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
let toolsPayload: Tool[];
let skillsPayload: Skill[];
let onboardingProfile: Record<string, unknown> | null;
let eventSources: MockEventSource[];
let eventId: number;
let traceTimelines: Record<string, TraceEvent[]>;

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
    vi.stubGlobal("fetch", vi.fn(fetchMock));
    vi.stubGlobal("EventSource", MockEventSource);
  });

  afterEach(() => {
    cleanup();
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
    expect(screen.getByRole("button", { name: /inspector/i })).toBeInTheDocument();

    const results = await axe.run(container, {
      rules: {
        "color-contrast": { enabled: false }
      }
    });
    expect(results.violations).toEqual([]);
  });

  it("renders Stitch command-center cockpit surfaces", async () => {
    const { container } = render(<App />);

    expect(await screen.findByRole("heading", { name: "Ask Kestrel" })).toBeInTheDocument();
    expect(container.querySelector(".stitch-command-deck")).toBeInTheDocument();
    expect(screen.getByText("Command Center")).toBeInTheDocument();
    expect(screen.getByText("Task Capsules")).toBeInTheDocument();
    expect(screen.getByText("Mutation Gate")).toBeInTheDocument();
    expect(screen.getByText("ORACLE Shadow")).toBeInTheDocument();
    expect(screen.getByText("Kernel")).toBeInTheDocument();
    expect(screen.getByText("Registry")).toBeInTheDocument();
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

  it("keeps a new empty thread isolated from the previous active run", async () => {
    render(<App />);

    await screen.findByText("Fix parser");
    fireEvent.click(screen.getByRole("button", { name: /new chat/i }));

    expect(within(screen.getByLabelText("Conversation threads")).getByRole("button", { name: /new chat/i })).toHaveClass("active");
    expect(screen.getByText("Tell Kestrel what to do.")).toBeInTheDocument();
    expect(screen.getByText("No run selected.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    await waitFor(() => {
      expect(within(screen.getByLabelText("Conversation threads")).getByRole("button", { name: /new chat/i })).toHaveClass("active");
      expect(screen.getByText("No run selected.")).toBeInTheDocument();
    });

    const transcript = screen.getByLabelText("Conversation transcript");
    expect(transcript).not.toHaveTextContent("Fix parser");
    expect(transcript).not.toHaveTextContent("Run tests");
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

    await screen.findByText("Inspect the repo");
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

  it("saves runtime settings through the persisted settings route", async () => {
    const fetchSpy = vi.mocked(fetch);
    render(<App />);

    await screen.findByRole("heading", { name: "Ask Kestrel" });
    fireEvent.click(screen.getByRole("button", { name: /settings/i }));
    await screen.findByRole("heading", { name: /settings/i });
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "codex-cli" } });
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "gpt-5.4" } });
    fireEvent.change(screen.getByLabelText("Temperature"), { target: { value: "0.7" } });
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
        provider: "codex-cli",
        model: "gpt-5.4",
        temperature: 0.7,
        backend: "memvid",
        memory_dir: "/tmp/memory",
        workspace: "/tmp/kestrel",
        stream: true,
        require_api_auth: false,
        autonomy_mode: "manual",
        allow_shell: true,
        allow_web: true
      });
      expect(body.allow_file_write).toBe(false);
      expect(body.allow_codex_cli).toBe(false);
    });
    expect(await screen.findByText("Settings saved and applied to new runs.")).toBeInTheDocument();
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
  if (path === "/api/runtime/settings" && init?.method === "PUT") {
    const body = JSON.parse(String(init.body ?? "{}"));
    return jsonResponse({
      settings: {
        ...body,
        updated_at: "2026-05-16T00:10:00Z",
        path: ".nest/config/runtime_settings.json",
        persisted: true
      },
      runtime: body
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
  if (path === "/api/sessions") return sessions;
  const sessionMatch = path.match(/^\/api\/sessions\/([^/]+)\/runs$/);
  if (sessionMatch) return sessionRuns[decodeURIComponent(sessionMatch[1])] ?? [];
  if (path === "/api/approvals?status=pending") return approvals;
  if (path === "/api/approvals") return approvals;
  if (path === "/api/tools") return toolsPayload;
  if (path === "/api/mcp/servers") return [];
  if (path === "/api/skills") return skillsPayload;
  if (path === "/api/plugins") return [];
  if (path === "/api/channels") return [];
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
      "openai-compatible": ["local-model"],
      openrouter: ["openai/gpt-5.5", "anthropic/claude-sonnet-4.5"],
      deepseek: ["deepseek-v4-pro", "deepseek-v4-flash"],
      kimi: ["kimi-k2.6", "kimi-k2.5"],
      ollama: ["llama3.1", "qwen2.5-coder"],
      "ollama-cloud": ["gpt-oss:120b", "gpt-oss:20b"],
      anthropic: ["claude-sonnet-4.5"],
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
  if (path.match(/^\/api\/runs\/run_[a-z0-9]+\/task-graph$/)) {
    return { tasks: [], ready_tasks: [], approval_blocked_tasks: [], subagents: [] };
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
    "ollama-cloud": "OLLAMA_API_KEY",
    deepseek: "DEEPSEEK_API_KEY",
    kimi: "MOONSHOT_API_KEY"
  };
  return apiKeyEnvs[provider] ?? null;
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
