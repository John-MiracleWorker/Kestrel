import type { Approval, Run, TaskGraph } from "../types";
import type { ProjectProfile } from "../mission/types";

export const gateProject: ProjectProfile = {
  project_id: "project_kestrel",
  display_name: "Kestrel",
  repository_path: "/tmp/kestrel",
  remote: "git@example.invalid:kestrel.git",
  default_branch: "main",
  allowed_paths: ["."],
  provider_policy: { preset: "balanced" },
  cost_budget: 1.5,
  privacy_class: "local_required",
  test_recipes: [{ name: "pytest", command: "pytest -q" }],
  build_recipes: [],
  capability_ceiling: ["file.read", "repair.prepare", "repair.validate"],
  baseline_index_digest: "sha256:index",
  archived_at: null,
  revision: 1,
  created_at: "2026-07-27T12:00:00Z",
  updated_at: "2026-07-27T12:00:00Z",
};

export const gateRun: Run = {
  run_id: "run_a11y_gate",
  project_id: gateProject.project_id,
  status: "running",
  message: "Fix the failing authentication test",
  session_id: "session_a11y_gate",
  workspace: gateProject.repository_path,
  provider: "local",
  model: "local",
  assistant_message: "Working through the acceptance plan.",
  tool_count: 2,
  context_chars: 800,
  stop_reason: "",
  created_at: "2026-07-27T12:00:00Z",
  updated_at: "2026-07-27T12:05:00Z",
};

export const gateTaskGraph: TaskGraph = {
  tasks: [
    {
      task_id: "map",
      title: "Map the failure",
      goal: "Reproduce the failure",
      profile: "worker",
      status: "running",
      approved: true,
      required_tools: ["repo.context_pack"],
      acceptance_criteria: ["Failure reproduced"],
    },
  ],
  ready_tasks: [],
  approval_blocked_tasks: [],
  subagents: [],
};

export const gateApproval: Approval = {
  approval_id: "approval_a11y_gate",
  run_id: gateRun.run_id,
  tool_call_id: "tool_call_a11y_gate",
  tool_name: "repair.apply_patch",
  arguments: { path: "src/auth.py" },
  status: "pending",
  risk: "medium",
  created_at: "2026-07-27T12:01:00Z",
  updated_at: "2026-07-27T12:01:00Z",
};

export function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Projects-list fetch stub used by MissionControl and ProjectsWorkspace. */
export function stubProjectsFetch() {
  return async (input: RequestInfo | URL) => {
    const path = new URL(String(input), "http://kestrel.test").pathname;
    if (path === "/api/projects") {
      return jsonResponse({ items: [gateProject], count: 1 });
    }
    if (
      path === `/api/projects/${gateProject.project_id}/mission/preflight`
    ) {
      return jsonResponse({ detail: "not inspected" }, 404);
    }
    return jsonResponse({ detail: path }, 404);
  };
}

export const missionControlProps = {
  runs: [] as Run[],
  activeRun: null as Run | null,
  taskGraph: null as TaskGraph | null,
  approvals: [] as Approval[],
  events: [],
  onLaunch: async () => undefined,
  onOpenRun: () => undefined,
  onOpenHistory: () => undefined,
  onOpenAdvanced: () => undefined,
  onOpenDiagnostics: () => undefined,
  onPrepareTool: () => undefined,
  onDecideApproval: () => undefined,
  onContinueConversation: async () => undefined,
  onAuthRequired: () => undefined,
};
