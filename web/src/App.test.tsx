import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

const run = {
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

describe("App", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(fetchMock));
    vi.stubGlobal(
      "EventSource",
      class {
        onmessage: ((event: MessageEvent) => void) | null = null;
        addEventListener = vi.fn();
        close = vi.fn();

        constructor(readonly url: string) {}
      }
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the local operator workspace and passes basic accessibility checks", async () => {
    const { container } = render(<App />);

    expect(await screen.findByRole("heading", { name: "Agent Workspace" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Run Agent" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Settings & Health" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/schema_version/)).toBeInTheDocument());

    const results = await axe.run(container, {
      rules: {
        "color-contrast": { enabled: false }
      }
    });
    expect(results.violations).toEqual([]);
  });
});

async function fetchMock(input: RequestInfo | URL): Promise<Response> {
  const raw = typeof input === "string" || input instanceof URL ? input.toString() : input.url;
  const url = new URL(raw, "http://kestrel.test");
  const path = `${url.pathname}${url.search}`;
  return jsonResponse(payloadFor(path));
}

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
}

function payloadFor(path: string): unknown {
  if (path === "/api/runs") return [run];
  if (path === "/api/sessions") {
    return [
      {
        session_id: "session_1",
        run_count: 1,
        status_counts: { completed: 1 },
        latest_run_id: "run_1",
        latest_status: "completed",
        latest_message: "Inspect the repo",
        created_at: run.created_at,
        updated_at: run.updated_at
      }
    ];
  }
  if (path === "/api/tools") {
    return [
      {
        name: "memory.search",
        description: "Search memory",
        parameters: { type: "object", properties: { query: { type: "string" } } },
        risk: "low",
        requires_approval: false,
        source: "builtin"
      }
    ];
  }
  if (path === "/api/approvals?status=pending" || path === "/api/approvals") return [];
  if (path === "/api/mcp/servers") return [];
  if (path === "/api/skills") return [];
  if (path === "/api/plugins") return [];
  if (path === "/api/channels") return [];
  if (path === "/api/memory/layers") {
    return [
      { layer: "working", path: "/tmp/working.mv2", exists: true, ok: true, backend: "InMemoryBackend" },
      { layer: "episodic", path: "/tmp/episodic.mv2", exists: true, ok: true, backend: "InMemoryBackend" }
    ];
  }
  if (path === "/api/runtime/config") {
    return {
      name: "Nested MV2 Agent",
      version: "0.1.0",
      schema_version: 9,
      provider: { name: "mock", model: "mock", api_key_env: null, api_key_configured: false },
      feature_flags: {
        allow_shell: false,
        allow_file_write: false,
        allow_policy_writes: false,
        require_approval_for_high_risk_tools: true
      },
      limits: { max_tool_rounds: 6 },
      paths: { workspace: "/tmp/kestrel", memory_dir: "/tmp/memory" },
      validation_commands: ["python -m pytest -q"]
    };
  }
  if (path === "/api/logs?limit=120") return [];
  if (path === "/api/cognition/lessons?k=20") return { items: [] };
  if (path === "/api/cognition/failures?k=20") return { items: [] };
  if (path === "/api/runs/run_1/task-graph") {
    return {
      tasks: [],
      ready_tasks: [],
      approval_blocked_tasks: [],
      subagents: []
    };
  }
  if (path === "/api/runs/run_1/trace?limit=700") {
    return {
      run,
      summary: {
        event_count: 1,
        span_count: 0,
        first_event_at: run.created_at,
        last_event_at: run.updated_at,
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
              validation_evidence: [],
              remaining_risks: []
            }
          },
          created_at: run.updated_at
        }
      ],
      traces: { lifecycle: [] }
    };
  }
  return {};
}
