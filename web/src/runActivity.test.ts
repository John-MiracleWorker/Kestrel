import { describe, expect, it } from "vitest";
import {
  activityItemsForEvents,
  assistantTextForRun,
  deriveThreadTitle,
  eventBelongsToRun,
  summarizeArguments
} from "./runActivity";
import type { Run, TraceEvent } from "./types";

const baseRun: Run = {
  run_id: "run_1",
  status: "running",
  message: "Build the parser",
  session_id: "session_1",
  workspace: "/tmp/kestrel",
  provider: "mock",
  model: "mock",
  assistant_message: "",
  tool_count: 0,
  context_chars: 0,
  stop_reason: "",
  error: null,
  created_at: "2026-05-17T00:00:00Z",
  updated_at: "2026-05-17T00:00:01Z"
};

describe("run activity helpers", () => {
  it("derives compact thread titles", () => {
    expect(deriveThreadTitle("   ")).toBe("New chat");
    expect(deriveThreadTitle("  Build   a parser  ")).toBe("Build a parser");
    expect(deriveThreadTitle("x".repeat(70))).toBe(`${"x".repeat(51)}...`);
  });

  it("uses streamed assistant text for the active pending run", () => {
    expect(assistantTextForRun(baseRun, "run_1", "streaming")).toBe("streaming");
    expect(assistantTextForRun({ ...baseRun, assistant_message: "final" }, "run_1", "streaming")).toBe("final");
    expect(assistantTextForRun({ ...baseRun, run_id: "run_2" }, "run_1", "streaming")).toBe("Kestrel is working...");
  });

  it("summarizes run activity events without assistant token noise", () => {
    const events: TraceEvent[] = [
      event(1, "assistant.token", { content: "hello" }),
      event(2, "tool.started", { tool_name: "shell.run", arguments: { command: ["pytest", "-q"] } }),
      event(3, "tool.completed", { tool_name: "shell.run", content: "passed" }),
      event(4, "context.compile", { context_chars: 1200, query: "repo" })
    ];

    expect(activityItemsForEvents(events)).toEqual([
      {
        id: "2",
        kind: "tool",
        label: "Using shell.run",
        meta: "pytest -q",
        detail: "",
        status: "running"
      },
      {
        id: "3",
        kind: "tool",
        label: "Finished shell.run",
        meta: "",
        detail: "passed",
        status: "completed"
      },
      {
        id: "4",
        kind: "thinking",
        label: "Gathering context",
        meta: "1200 context chars",
        detail: "repo",
        status: "info"
      }
    ]);
  });

  it("checks run ownership and argument summaries", () => {
    expect(eventBelongsToRun(event(1, "run.started", {}, "run_1"), "run_1")).toBe(true);
    expect(eventBelongsToRun(event(1, "run.started", { run_id: "run_2" }, "run_1"), "run_2")).toBe(true);
    expect(eventBelongsToRun(event(1, "run.started", {}, "run_1"), "run_2")).toBe(false);
    expect(summarizeArguments({ command: ["npm", "test"] })).toBe("npm test");
    expect(summarizeArguments({ path: "/tmp/file.txt" })).toBe("/tmp/file.txt");
  });
});

function event(id: number, type: string, payload: Record<string, unknown>, runId = "run_1"): TraceEvent {
  return {
    id,
    run_id: runId,
    type,
    payload,
    created_at: `2026-05-17T00:00:0${id}Z`
  };
}
