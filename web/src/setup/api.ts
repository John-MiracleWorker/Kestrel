import { getJson, postJson, putJson } from "../api";
import type {
  ProjectListResponse,
  ProjectProfile,
} from "../mission/types";
import { readDesktopBridge } from "../platform/desktopBridge";
import type {
  ProviderModelCatalog,
  SecretRef,
  SetupReadinessCheck,
  SetupReadinessReport,
} from "../types";
import type {
  IntelligenceSelection,
  ProjectCreateInput,
  ProjectSetupDraft,
  ProjectSetupDraftRequest,
  SetupCenterApi,
  SetupFirstMissionPreflight,
  SetupFolderChoice,
  SetupRuntimeSettings,
  SetupSnapshot,
} from "./types";

const desktopCredentialProviders = new Set([
  "openai",
  "openrouter",
  "deepseek",
  "kimi",
  "ollama-cloud",
  "anthropic",
  "grok",
  "gemini",
]);

const demoCatalog: ProviderModelCatalog = {
  provider: "mock",
  models: ["mock"],
  fallback_models: ["mock"],
  source: "bundled",
  ok: true,
  fetchable: false,
  error: null,
  base_url_configured: false,
  api_key_env: null,
  api_key_configured: false,
};

export const defaultSetupCenterApi: SetupCenterApi =
  createSetupCenterApi();

export function createSetupCenterApi(): SetupCenterApi {
  return {
    supportsNativeProjectPicker: desktopBridgeAvailable(),
    supportsNativeWorkspacePicker: desktopBridgeAvailable(),
    supportsNativeCredentialDialog: desktopBridgeAvailable(),
    load: loadSetupSnapshot,
    async saveIntelligence(selection) {
      await putJson("/api/runtime/settings", {
        expected_revision: selection.expectedRevision,
        provider: selection.provider,
        model: selection.model,
        base_url: selection.baseUrl ?? null,
        api_key_env: selection.apiKeyEnv ?? null,
      });
      return loadSetupSnapshot();
    },
    async chooseProjectFolder() {
      const bridge = safeDesktopBridge();
      if (bridge === null) return { status: "cancelled" };
      return parseFolderChoice(await bridge.chooseProjectFolder());
    },
    async inspectProject(request) {
      const payload = await postJson<unknown>(
        "/api/projects/setup-draft",
        {
          repository_path: request.repositoryPath,
          direct_estimated_cost_usd:
            request.directEstimatedCostUsd,
          cost_budget: request.costBudget,
        },
      );
      return parseProjectSetupDraft(payload);
    },
    async createProject(input) {
      const payload = await postJson<unknown>("/api/projects", input);
      return parseProject(payload);
    },
    async preflightFirstMission(projectId) {
      const payload = await postJson<unknown>(
        `/api/projects/${encodeURIComponent(
          projectId,
        )}/mission/preflight`,
        {
          objective:
            "Explain this repository's architecture, entry points, and validation surfaces with exact file evidence.",
          template_id: "explain_repository",
        },
      );
      return parseFirstMissionPreflight(payload);
    },
    async repairCore(checkId, expectedRevision) {
      const bridge = safeDesktopBridge();
      if (bridge === null) {
        throw new Error("desktop_core_repair_unavailable");
      }
      const rawChoice =
        checkId === "workspace"
          ? await bridge.chooseProjectFolder()
          : null;
      if (rawChoice === null) {
        throw new Error("core_check_has_no_bounded_gui_repair");
      }
      const choice = parseFolderChoice(rawChoice);
      if (choice.status === "cancelled") {
        throw new Error("core_repair_cancelled");
      }
      await putJson("/api/runtime/settings", {
        expected_revision: expectedRevision,
        ...(checkId === "workspace"
          ? { workspace: choice.path }
          : { memory_dir: choice.path }),
      });
      const next = await loadSetupSnapshot();
      const appliedPath =
        checkId === "workspace"
          ? next.runtime.workspace
          : next.runtime.memoryDir;
      if (appliedPath !== choice.path) {
        throw new Error(
          "The selected path is launch-controlled in this runtime. Review restart recovery; Setup did not claim or apply the location.",
        );
      }
      return next;
    },
    async storeProviderCredential(provider) {
      if (!desktopCredentialProviders.has(provider)) {
        throw new Error("provider_credential_dialog_unavailable");
      }
      const bridge = safeDesktopBridge();
      if (bridge === null) {
        throw new Error("desktop_bridge_unavailable");
      }
      parseCredentialResult(
        await bridge.openCredentialDialog({
          providerId: provider,
          purpose: "provider_api_key",
        }),
      );
      return loadSetupSnapshot();
    },
  };
}

export async function loadSetupSnapshot(
  signal?: AbortSignal,
): Promise<SetupSnapshot> {
  const [
    readinessPayload,
    catalogPayload,
    projectsPayload,
    secretsPayload,
    runtimePayload,
  ] = await Promise.all([
    getJson<unknown>("/api/product/setup", { signal }),
    getJson<unknown>("/api/runtime/models", { signal }),
    getJson<unknown>("/api/projects", { signal }),
    getJson<unknown>("/api/secrets", { signal }),
    getJson<unknown>("/api/runtime/settings", { signal }),
  ]);

  return {
    readiness: parseReadiness(readinessPayload),
    catalogs: parseCatalogs(catalogPayload),
    projects: parseProjects(projectsPayload),
    secrets: parseSecrets(secretsPayload),
    runtime: parseRuntimeSettings(runtimePayload),
  };
}

function parseReadiness(value: unknown): SetupReadinessReport {
  const record = plainRecord(value, "setup_readiness_invalid");
  const experienceMode = stringValue(
    record.experience_mode,
    "setup_experience_mode_invalid",
  );
  if (
    experienceMode !== "demo" &&
    experienceMode !== "model_not_connected" &&
    experienceMode !== "connected"
  ) {
    throw new Error("setup_experience_mode_invalid");
  }
  const checksValue = record.checks;
  if (!Array.isArray(checksValue)) {
    throw new Error("setup_checks_invalid");
  }
  const checks = checksValue.map((entry) => {
    const check = plainRecord(entry, "setup_check_invalid");
    const status = stringValue(
      check.status,
      "setup_check_status_invalid",
    );
    if (status !== "pass" && status !== "warn" && status !== "fail") {
      throw new Error("setup_check_status_invalid");
    }
    return {
      check_id: stringValue(check.check_id, "setup_check_id_invalid"),
      title: stringValue(check.title, "setup_check_title_invalid"),
      status,
      detail: stringValue(check.detail, "setup_check_detail_invalid"),
      recovery: stringValue(
        check.recovery,
        "setup_check_recovery_invalid",
      ),
    } satisfies SetupReadinessCheck;
  });

  return {
    schema: stringValue(record.schema, "setup_schema_invalid"),
    ready: booleanValue(record.ready, "setup_ready_invalid"),
    experience_mode: experienceMode,
    pass_count: countValue(record.pass_count, "setup_pass_count_invalid"),
    warn_count: countValue(record.warn_count, "setup_warn_count_invalid"),
    fail_count: countValue(record.fail_count, "setup_fail_count_invalid"),
    checks,
    next_action: stringValue(
      record.next_action,
      "setup_next_action_invalid",
    ),
    credential_storage:
      typeof record.credential_storage === "object" &&
      record.credential_storage !== null
        ? (record.credential_storage as SetupReadinessReport["credential_storage"])
        : undefined,
  };
}

function parseCatalogs(value: unknown): ProviderModelCatalog[] {
  const record = plainRecord(value, "model_catalog_invalid");
  const entries = Array.isArray(record.providers)
    ? record.providers
    : [record];
  const parsed = entries.map(parseCatalog);
  if (!parsed.some((catalog) => catalog.provider === "mock")) {
    parsed.unshift(demoCatalog);
  }
  return parsed;
}

function parseCatalog(value: unknown): ProviderModelCatalog {
  const record = plainRecord(value, "model_catalog_invalid");
  return {
    provider: stringValue(
      record.provider,
      "model_catalog_provider_invalid",
    ),
    models: stringArray(record.models, "model_catalog_models_invalid"),
    fallback_models: stringArray(
      record.fallback_models,
      "model_catalog_fallback_invalid",
    ),
    source: stringValue(record.source, "model_catalog_source_invalid"),
    ok: booleanValue(record.ok, "model_catalog_status_invalid"),
    fetchable: booleanValue(
      record.fetchable,
      "model_catalog_fetchable_invalid",
    ),
    error:
      record.error === null || record.error === undefined
        ? null
        : stringValue(record.error, "model_catalog_error_invalid"),
    base_url_configured: booleanValue(
      record.base_url_configured,
      "model_catalog_base_url_invalid",
    ),
    api_key_env:
      record.api_key_env === null || record.api_key_env === undefined
        ? null
        : stringValue(
            record.api_key_env,
            "model_catalog_api_key_env_invalid",
          ),
    api_key_configured: booleanValue(
      record.api_key_configured,
      "model_catalog_api_key_invalid",
    ),
    fetched_at:
      record.fetched_at === null || record.fetched_at === undefined
        ? null
        : stringValue(
            record.fetched_at,
            "model_catalog_fetched_at_invalid",
          ),
  };
}

function parseProjects(value: unknown): ProjectProfile[] {
  const record = plainRecord(value, "project_list_invalid");
  if (!Array.isArray(record.items)) {
    throw new Error("project_list_invalid");
  }
  const response = {
    items: record.items.map(parseProject),
    count: countValue(record.count, "project_count_invalid"),
  } satisfies ProjectListResponse;
  if (response.count !== response.items.length) {
    throw new Error("project_count_mismatch");
  }
  return response.items;
}

function parseProject(value: unknown): ProjectProfile {
  const record = plainRecord(value, "project_invalid");
  return {
    ...(record as ProjectProfile),
    project_id: stringValue(record.project_id, "project_id_invalid"),
    display_name: stringValue(
      record.display_name,
      "project_name_invalid",
    ),
    repository_path: stringValue(
      record.repository_path,
      "project_path_invalid",
    ),
    default_branch: stringValue(
      record.default_branch,
      "project_branch_invalid",
    ),
    allowed_paths: stringArray(
      record.allowed_paths,
      "project_allowed_paths_invalid",
    ),
    provider_policy: plainRecord(
      record.provider_policy,
      "project_provider_policy_invalid",
    ),
    privacy_class: stringValue(
      record.privacy_class,
      "project_privacy_invalid",
    ),
    test_recipes: Array.isArray(record.test_recipes)
      ? (record.test_recipes as ProjectProfile["test_recipes"])
      : [],
    build_recipes: Array.isArray(record.build_recipes)
      ? (record.build_recipes as ProjectProfile["build_recipes"])
      : [],
    capability_ceiling: stringArray(
      record.capability_ceiling,
      "project_capability_ceiling_invalid",
    ),
    revision: countValue(record.revision, "project_revision_invalid"),
    created_at: stringValue(
      record.created_at,
      "project_created_at_invalid",
    ),
    updated_at: stringValue(
      record.updated_at,
      "project_updated_at_invalid",
    ),
  };
}

function parseSecrets(value: unknown): SecretRef[] {
  if (!Array.isArray(value)) throw new Error("secret_metadata_invalid");
  return value.map((entry) => {
    const record = plainRecord(entry, "secret_metadata_invalid");
    return {
      ...(record as SecretRef),
      id: stringValue(record.id, "secret_id_invalid"),
      name: stringValue(record.name, "secret_name_invalid"),
      purpose: stringValue(record.purpose, "secret_purpose_invalid"),
      secret_ref: stringValue(
        record.secret_ref,
        "secret_reference_invalid",
      ),
      configured: booleanValue(
        record.configured,
        "secret_configured_invalid",
      ),
      validated: booleanValue(
        record.validated,
        "secret_validated_invalid",
      ),
    };
  });
}

function parseRuntimeSettings(value: unknown): SetupRuntimeSettings {
  const outer = plainRecord(value, "runtime_settings_invalid");
  const source =
    typeof outer.settings === "object" && outer.settings !== null
      ? plainRecord(outer.settings, "runtime_settings_invalid")
      : outer;
  const revision = stringValue(
    source.revision,
    "runtime_settings_revision_missing",
  );
  return {
    expectedRevision: revision,
    provider: stringValue(
      source.provider,
      "runtime_provider_invalid",
    ),
    model: stringValue(source.model, "runtime_model_invalid"),
    baseUrl:
      source.base_url === null || source.base_url === undefined
        ? null
        : stringValue(source.base_url, "runtime_base_url_invalid"),
    apiKeyEnv:
      source.api_key_env === null || source.api_key_env === undefined
        ? null
        : stringValue(
            source.api_key_env,
            "runtime_api_key_env_invalid",
          ),
    workspace: stringValue(
      source.workspace,
      "runtime_settings_workspace_invalid",
    ),
    memoryDir: stringValue(
      source.memory_dir,
      "runtime_settings_memory_dir_invalid",
    ),
  };
}

function parseProjectSetupDraft(
  value: unknown,
): ProjectSetupDraft {
  const record = plainRecord(value, "project_setup_draft_invalid");
  if (record.schema !== "kestrel.project_setup_draft.v1") {
    throw new Error("project_setup_draft_schema_invalid");
  }
  const inspection = plainRecord(
    record.inspection,
    "project_setup_inspection_invalid",
  );
  const git = plainRecord(
    inspection.git,
    "project_setup_git_invalid",
  );
  const gitState = stringValue(
    git.state,
    "project_setup_git_state_invalid",
  );
  if (
    gitState !== "clean" &&
    gitState !== "dirty" &&
    gitState !== "unknown"
  ) {
    throw new Error("project_setup_git_state_invalid");
  }
  const index = plainRecord(
    inspection.index,
    "project_setup_index_invalid",
  );
  if (index.status !== "not_created") {
    throw new Error("project_setup_index_status_invalid");
  }
  const firstMission = plainRecord(
    record.first_mission,
    "project_setup_first_mission_invalid",
  );
  if (firstMission.template_id !== "explain_repository") {
    throw new Error("project_setup_template_invalid");
  }
  const createInputRecord = plainRecord(
    record.create_input,
    "project_setup_create_input_invalid",
  );
  const privacyClass = stringValue(
    createInputRecord.privacy_class,
    "project_setup_privacy_invalid",
  );
  if (
    privacyClass !== "local_required" &&
    privacyClass !== "local_preferred" &&
    privacyClass !== "approved_cloud"
  ) {
    throw new Error("project_setup_privacy_invalid");
  }
  const costBudget =
    createInputRecord.cost_budget === null
      ? null
      : finiteNonNegativeNumber(
          createInputRecord.cost_budget,
          "project_setup_budget_invalid",
        );

  return {
    schema: "kestrel.project_setup_draft.v1",
    inspection: {
      canonical_path: stringValue(
        inspection.canonical_path,
        "project_setup_path_invalid",
      ),
      git: {
        branch: stringValue(
          git.branch,
          "project_setup_branch_invalid",
        ),
        state: gitState,
        summary: stringValue(
          git.summary,
          "project_setup_git_summary_invalid",
        ),
      },
      index: {
        status: "not_created",
        detail: stringValue(
          index.detail,
          "project_setup_index_detail_invalid",
        ),
      },
      test_recipes: parseRecipes(
        inspection.test_recipes,
        "project_setup_test_recipes_invalid",
      ),
      build_recipes: parseRecipes(
        inspection.build_recipes,
        "project_setup_build_recipes_invalid",
      ),
      recipe_warnings: stringArray(
        inspection.recipe_warnings,
        "project_setup_recipe_warnings_invalid",
      ),
    },
    create_input: {
      display_name: stringValue(
        createInputRecord.display_name,
        "project_setup_name_invalid",
      ),
      repository_path: stringValue(
        createInputRecord.repository_path,
        "project_setup_repository_invalid",
      ),
      default_branch: stringValue(
        createInputRecord.default_branch,
        "project_setup_default_branch_invalid",
      ),
      allowed_paths: stringArray(
        createInputRecord.allowed_paths,
        "project_setup_allowed_paths_invalid",
      ),
      provider_policy: plainRecord(
        createInputRecord.provider_policy,
        "project_setup_policy_invalid",
      ),
      cost_budget: costBudget,
      privacy_class: privacyClass,
      test_recipes: parseRecipes(
        createInputRecord.test_recipes,
        "project_setup_test_recipes_invalid",
      ),
      build_recipes: parseRecipes(
        createInputRecord.build_recipes,
        "project_setup_build_recipes_invalid",
      ),
      capability_ceiling: stringArray(
        createInputRecord.capability_ceiling,
        "project_setup_capabilities_invalid",
      ),
    },
    first_mission: {
      template_id: "explain_repository",
      estimated_provider_calls: countValue(
        firstMission.estimated_provider_calls,
        "project_setup_estimated_calls_invalid",
      ),
      can_start: booleanValue(
        firstMission.can_start,
        "project_setup_can_start_invalid",
      ),
      required_tools: stringArray(
        firstMission.required_tools,
        "project_setup_required_tools_invalid",
      ),
      missing_tools: stringArray(
        firstMission.missing_tools,
        "project_setup_missing_tools_invalid",
      ),
      blockers: stringArray(
        firstMission.blockers,
        "project_setup_blockers_invalid",
      ),
    },
  };
}

function parseFirstMissionPreflight(
  value: unknown,
): SetupFirstMissionPreflight {
  const record = plainRecord(
    value,
    "setup_first_mission_preflight_invalid",
  );
  const blockers = stringArray(
    record.blockers,
    "setup_first_mission_blockers_invalid",
  );
  const warnings = stringArray(
    record.warnings,
    "setup_first_mission_warnings_invalid",
  );
  if (!Array.isArray(record.checks)) {
    throw new Error("setup_first_mission_checks_invalid");
  }
  const checks = record.checks.map((entry) => {
    const check = plainRecord(
      entry,
      "setup_first_mission_check_invalid",
    );
    const status = stringValue(
      check.status,
      "setup_first_mission_check_status_invalid",
    );
    if (
      status !== "pass" &&
      status !== "warn" &&
      status !== "fail" &&
      status !== "unknown"
    ) {
      throw new Error("setup_first_mission_check_status_invalid");
    }
    const checkedStatus = status as
      | "pass"
      | "warn"
      | "fail"
      | "unknown";
    return {
      id: stringValue(
        check.check_id,
        "setup_first_mission_check_id_invalid",
      ),
      title: stringValue(
        check.title,
        "setup_first_mission_check_title_invalid",
      ),
      status: checkedStatus,
      detail: stringValue(
        check.detail,
        "setup_first_mission_check_detail_invalid",
      ),
    };
  });
  return {
    projectId: stringValue(
      record.project_id,
      "setup_first_mission_project_invalid",
    ),
    projectRevision: countValue(
      record.project_revision,
      "setup_first_mission_revision_invalid",
    ),
    canStart: booleanValue(
      record.can_start,
      "setup_first_mission_can_start_invalid",
    ),
    blockers,
    warnings,
    checks,
  };
}

function parseRecipes(
  value: unknown,
  error: string,
): ProjectCreateInput["test_recipes"] {
  if (!Array.isArray(value)) throw new Error(error);
  return value.map((entry) => {
    const recipe = plainRecord(entry, error);
    const workingDirectory =
      recipe.working_directory === null ||
      recipe.working_directory === undefined
        ? undefined
        : stringValue(recipe.working_directory, error);
    return {
      name: stringValue(recipe.name, error),
      command: stringValue(recipe.command, error),
      ...(workingDirectory
        ? { working_directory: workingDirectory }
        : {}),
    };
  });
}

function finiteNonNegativeNumber(
  value: unknown,
  error: string,
): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0
  ) {
    throw new Error(error);
  }
  return value;
}

function parseFolderChoice(value: unknown): SetupFolderChoice {
  const record = plainRecord(value, "desktop_folder_choice_invalid");
  const status = stringValue(
    record.status,
    "desktop_folder_choice_invalid",
  );
  if (status === "cancelled") return { status };
  if (status !== "selected") {
    throw new Error("desktop_folder_choice_invalid");
  }
  const path = stringValue(record.path, "desktop_folder_path_invalid");
  const normalizedSegments = path
    .replace(/\\/g, "/")
    .replace(/^[A-Za-z]:/, "")
    .split("/")
    .filter(Boolean);
  if (
    path.length > 4_096 ||
    path.includes("\0") ||
    normalizedSegments.some(
      (segment) => segment === "." || segment === "..",
    ) ||
    (!path.startsWith("/") && !/^[A-Za-z]:[\\/]/.test(path))
  ) {
    throw new Error("desktop_folder_path_invalid");
  }
  return {
    status,
    path,
    displayLabel: stringValue(
      record.displayLabel,
      "desktop_folder_label_invalid",
    ),
  };
}

function parseCredentialResult(value: unknown): void {
  const record = plainRecord(value, "credential_result_invalid");
  if (record.status === "cancelled") return;
  if (
    record.status !== "stored" ||
    typeof record.secretRef !== "string" ||
    !record.secretRef.startsWith("secret://")
  ) {
    throw new Error("credential_result_invalid");
  }
}

function desktopBridgeAvailable(): boolean {
  return safeDesktopBridge() !== null;
}

function safeDesktopBridge() {
  try {
    return readDesktopBridge();
  } catch {
    return null;
  }
}

function plainRecord(
  value: unknown,
  error: string,
): Record<string, unknown> {
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value)
  ) {
    throw new Error(error);
  }
  return value as Record<string, unknown>;
}

function stringValue(value: unknown, error: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(error);
  return value;
}

function booleanValue(value: unknown, error: string): boolean {
  if (typeof value !== "boolean") throw new Error(error);
  return value;
}

function countValue(value: unknown, error: string): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0
  ) {
    throw new Error(error);
  }
  return value;
}

function stringArray(value: unknown, error: string): string[] {
  if (
    !Array.isArray(value) ||
    value.some((entry) => typeof entry !== "string")
  ) {
    throw new Error(error);
  }
  return value as string[];
}
