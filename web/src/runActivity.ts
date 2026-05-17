import type { Run, TraceEvent } from "./types";

export type LiveActivityItem = {
  id: string;
  kind: "thinking" | "tool";
  label: string;
  detail: string;
  meta: string;
  status: "running" | "completed" | "failed" | "info";
};

export function deriveThreadTitle(message: string): string {
  const compact = message.replace(/\s+/g, " ").trim();
  if (!compact) return "New chat";
  return compact.length > 54 ? `${compact.slice(0, 51)}...` : compact;
}

export function assistantTextForRun(run: Run, activeRunId: string | null | undefined, streamedAssistant: string): string {
  if (run.run_id === activeRunId && !run.assistant_message && streamedAssistant) return streamedAssistant;
  return run.assistant_message || friendlyRunStatus(run);
}

export function friendlyRunStatus(run: Run): string {
  if (run.status === "queued") return "Queued";
  if (run.status === "running") return "Kestrel is working...";
  if (run.status === "failed") return run.error || "Failed";
  if (run.status === "cancelled") return "Cancelled";
  return run.stop_reason || "Working...";
}

export function friendlyEventLabel(type: string): string {
  const labels: Record<string, string> = {
    "run.queued": "Queued",
    "run.started": "Started",
    "context.compile": "Gathering context",

    "memory.write": "Updating memory",
    "tool.started": "Using tool",
    "tool.completed": "Tool finished",
    "tool.failed": "Tool failed",
    "approval.requested": "Needs approval",
    "run.completed": "Complete",
    "run.failed": "Failed",
    "run.cancelled": "Cancelled",
    "scheduler.step": "Planning",
    "scheduler.run": "Planning",
    "subagent.started": "Delegating",
    "subagent.completed": "Delegation complete",
    "task.approved": "Task approved"
  };
  return labels[type] ?? type;
}

export function activityItemsForEvents(events: TraceEvent[]): LiveActivityItem[] {
  return events
    .map(activityItemForEvent)
    .filter((item): item is LiveActivityItem => Boolean(item))
    .slice(-8);
}

export function eventKey(event: TraceEvent): string {
  return Number.isFinite(event.id) ? String(event.id) : `${event.type}-${eventTimestamp(event)}-${JSON.stringify(event.payload).slice(0, 80)}`;
}

export function eventBelongsToRun(event: TraceEvent, runId: string | null | undefined): boolean {
  if (!runId) return false;
  return event.run_id === runId || event.payload.run_id === runId;
}

export function eventTimestamp(event: TraceEvent): string {
  return typeof event.created_at === "string" ? event.created_at : "";
}

function activityItemForEvent(event: TraceEvent): LiveActivityItem | null {
  if (event.type === "assistant.token") return null;
  if (!isVisibleActivityEvent(event.type)) return null;
  const toolName = toolNameForEvent(event);
  if (event.type === "tool.started") {
    return {
      id: String(event.id),
      kind: "tool",
      label: `Using ${toolName}`,
      meta: argumentsSummaryForEvent(event),
      detail: "",
      status: "running"
    };
  }
  if (event.type === "tool.completed") {
    return {
      id: String(event.id),
      kind: "tool",
      label: `Finished ${toolName}`,
      meta: argumentsSummaryForEvent(event),
      detail: compactActivityDetail(event.payload.content),
      status: "completed"
    };
  }
  if (event.type === "tool.failed") {
    return {
      id: String(event.id),
      kind: "tool",
      label: `Failed ${toolName}`,
      meta: argumentsSummaryForEvent(event),
      detail: compactActivityDetail(event.payload.content ?? event.payload.error),
      status: "failed"
    };
  }
  return {
    id: String(event.id),
    kind: "thinking",
    label: friendlyEventLabel(event.type),
    meta: thinkingMetaForEvent(event),
    detail: thinkingDetailForEvent(event),
    status: event.type === "run.completed" ? "completed" : event.type === "run.failed" ? "failed" : "info"
  };
}

function isVisibleActivityEvent(type: string): boolean {
  return [
    "run.started",
    "context.compile",
    "memory.write",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "approval.requested",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "scheduler.step",
    "scheduler.run",
    "subagent.started",
    "subagent.completed",
    "subagent.failed",
    "task.approved"
  ].includes(type);
}

function toolNameForEvent(event: TraceEvent): string {
  return String(event.payload.tool ?? event.payload.tool_name ?? "tool");
}

function argumentsSummaryForEvent(event: TraceEvent): string {
  const args = event.payload.arguments;
  return args && typeof args === "object" && !Array.isArray(args) ? summarizeArguments(args as Record<string, unknown>) : "";
}

function thinkingMetaForEvent(event: TraceEvent): string {
  if (event.type === "context.compile" && typeof event.payload.context_chars === "number") return `${event.payload.context_chars} context chars`;
  if (event.type === "memory.write" && event.payload.index && event.payload.total) return `${event.payload.index}/${event.payload.total}`;
  if (event.type.startsWith("scheduler.") && event.payload.task_id) return String(event.payload.task_id);
  return "";
}

function thinkingDetailForEvent(event: TraceEvent): string {
  const value = event.payload.query ?? event.payload.record_id ?? event.payload.source ?? event.payload.error;
  return compactActivityDetail(value);
}

function compactActivityDetail(value: unknown): string {
  if (value === null || value === undefined) return "";
  const text = String(value).replace(/\s+/g, " ").trim();
  return text.length > 120 ? `${text.slice(0, 117)}...` : text;
}

export function riskLabel(risk: string): string {
  if (!risk) return "Unknown risk";
  return `${risk.charAt(0).toUpperCase()}${risk.slice(1)} risk`;
}

export function summarizeArguments(argumentsValue: Record<string, unknown>): string {
  const command = argumentsValue.command;
  if (Array.isArray(command)) return command.map((item) => String(item)).join(" ");
  const path = argumentsValue.path ?? argumentsValue.file ?? argumentsValue.cwd;
  if (path) return String(path);
  return Object.keys(argumentsValue).slice(0, 3).join(", ") || "No arguments";
}
