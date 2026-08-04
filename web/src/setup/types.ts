import type {
  ProjectProfile,
  ProjectRecipe,
} from "../mission/types";
import type {
  ProviderModelCatalog,
  SecretRef,
  SetupReadinessReport,
} from "../types";

export type SetupStageId =
  | "core"
  | "intelligence"
  | "project"
  | "safety"
  | "first_mission";

export type SetupStageState =
  | "complete"
  | "current"
  | "attention"
  | "available"
  | "skipped"
  | "upcoming";

export type SetupRuntimeSettings = {
  expectedRevision: string;
  provider: string;
  model: string;
  baseUrl: string | null;
  apiKeyEnv: string | null;
  workspace: string;
  memoryDir: string;
};

export type SetupSnapshot = {
  readiness: SetupReadinessReport;
  catalogs: ProviderModelCatalog[];
  projects: ProjectProfile[];
  secrets: SecretRef[];
  runtime: SetupRuntimeSettings;
};

export type IntelligenceSelection = {
  expectedRevision: string;
  provider: string;
  model: string;
  baseUrl?: string | null;
  apiKeyEnv?: string | null;
};

export type SetupFolderChoice =
  | { status: "cancelled" }
  | {
      status: "selected";
      path: string;
      displayLabel: string;
    };

export type ProjectCreateInput = {
  display_name: string;
  repository_path: string;
  default_branch: string;
  allowed_paths: string[];
  provider_policy: Record<string, unknown>;
  cost_budget: number | null;
  privacy_class:
    | "local_required"
    | "local_preferred"
    | "approved_cloud";
  test_recipes: ProjectRecipe[];
  build_recipes: ProjectRecipe[];
  capability_ceiling: string[];
};

export type ProjectSetupDraft = {
  schema: "kestrel.project_setup_draft.v1";
  inspection: {
    canonical_path: string;
    git: {
      branch: string;
      state: "clean" | "dirty" | "unknown";
      summary: string;
    };
    index: {
      status: "not_created";
      detail: string;
    };
    test_recipes: ProjectRecipe[];
    build_recipes: ProjectRecipe[];
    recipe_warnings: string[];
  };
  create_input: ProjectCreateInput;
  first_mission: {
    template_id: "explain_repository";
    estimated_provider_calls: number;
    can_start: boolean;
    required_tools: string[];
    missing_tools: string[];
    blockers: string[];
  };
};

export type ProjectSetupDraftRequest = {
  repositoryPath: string;
  directEstimatedCostUsd: number | null;
  costBudget: number | null;
};

export type SetupFirstMissionPreflight = {
  projectId: string;
  projectRevision: number;
  canStart: boolean;
  blockers: string[];
  warnings: string[];
  checks: Array<{
    id: string;
    title: string;
    status: "pass" | "warn" | "fail" | "unknown";
    detail: string;
  }>;
};

export type SetupCenterApi = {
  supportsNativeProjectPicker: boolean;
  supportsNativeWorkspacePicker?: boolean;
  supportsNativeCredentialDialog?: boolean;
  load(signal?: AbortSignal): Promise<SetupSnapshot>;
  saveIntelligence(
    selection: IntelligenceSelection,
  ): Promise<SetupSnapshot>;
  chooseProjectFolder(): Promise<SetupFolderChoice>;
  inspectProject(
    request: ProjectSetupDraftRequest,
  ): Promise<ProjectSetupDraft>;
  createProject(input: ProjectCreateInput): Promise<ProjectProfile>;
  preflightFirstMission(
    projectId: string,
  ): Promise<SetupFirstMissionPreflight>;
  repairCore(
    checkId: string,
    expectedRevision: string,
  ): Promise<SetupSnapshot>;
  storeProviderCredential(provider: string): Promise<SetupSnapshot>;
};

export type SetupPresentationState = {
  seen: boolean;
};

export type SetupNavigation = {
  openGeneralSettings(): void;
  openProviderSettings(): void;
  openSafetySettings(): void;
  openCapabilitiesSettings?(): void;
  openMemorySettings?(): void;
  openApiAccessSettings?(): void;
  openMission(): void;
};
