export type MissionCheckStatus = "pass" | "warn" | "fail" | "unknown";

export type ProjectRecipe = {
  name: string;
  command: string;
  working_directory?: string | null;
};

export type ProjectProfile = {
  project_id: string;
  display_name: string;
  repository_path: string;
  remote?: string | null;
  default_branch: string;
  allowed_paths: string[];
  provider_policy: Record<string, unknown>;
  cost_budget?: number | null;
  privacy_class: string;
  test_recipes: ProjectRecipe[];
  build_recipes: ProjectRecipe[];
  capability_ceiling: string[];
  baseline_index_digest?: string | null;
  archived_at?: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type ProjectListResponse = {
  items: ProjectProfile[];
  count: number;
};

export type MissionGoalTemplate = {
  template_id: string;
  label: string;
  description: string;
  default_objective: string;
};

export type MissionPlanTask = {
  task_id: string;
  title: string;
  rationale: string;
  dependencies: string[];
  acceptance_criteria: string[];
  required_tools: string[];
  risk: string;
};

export type MissionPreflightCheck = {
  check_id: string;
  title: string;
  status: MissionCheckStatus;
  detail: string;
  recovery?: string | null;
};

export type MissionPreflight = {
  schema: "kestrel.mission_preflight.v1";
  project_id: string;
  project_name: string;
  repository_path: string;
  objective: string;
  template_id: string;
  branch: string;
  working_tree: {
    state: "clean" | "dirty" | "unknown";
    summary: string;
  };
  route_policy: string;
  budget: {
    currency: string;
    limit: number | null;
    estimate: number | null;
  };
  effective_capabilities: string[];
  likely_approvals: string[];
  validation_recipes: string[];
  rollback: string;
  index: {
    freshness: "current" | "stale" | "missing" | "unknown";
    digest?: string | null;
    indexed_at?: string | null;
    detail: string;
  };
  provider: {
    status: MissionCheckStatus;
    detail: string;
  };
  checks: MissionPreflightCheck[];
  tasks: MissionPlanTask[];
  warnings: string[];
  blockers: string[];
  can_start: boolean;
  generated_at: string;
};

export type MissionLaunch = {
  objective: string;
  project: ProjectProfile;
  templateId: string;
  plan: MissionPlanTask[];
  preflight: MissionPreflight;
};
