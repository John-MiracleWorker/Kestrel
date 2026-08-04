import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import axe from "axe-core";
import {
  afterEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import type { ProjectProfile } from "../mission/types";
import type {
  ProviderModelCatalog,
  SetupReadinessReport,
} from "../types";
import { SetupCenter } from "./SetupCenter";
import type {
  ProjectCreateInput,
  ProjectSetupDraft,
  SetupCenterApi,
  SetupFirstMissionPreflight,
  SetupSnapshot,
} from "./types";

const firstMissionTools = [
  "file.read",
  "repo.context_pack",
  "repo.dependencies",
  "repo.map",
  "repo.references",
  "repo.symbols",
  "repo.tests_for",
];

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

const project: ProjectProfile = {
  project_id: "project_kestrel",
  display_name: "Kestrel",
  repository_path: "/workspace/kestrel",
  remote: null,
  default_branch: "main",
  allowed_paths: ["."],
  provider_policy: { preset: "local_only" },
  cost_budget: 0,
  privacy_class: "local_required",
  test_recipes: [],
  build_recipes: [],
  capability_ceiling: firstMissionTools.map(
    (tool) => `tool:${tool}`,
  ),
  baseline_index_digest: null,
  archived_at: null,
  revision: 1,
  created_at: "2026-07-31T00:00:00Z",
  updated_at: "2026-07-31T00:00:00Z",
};

function readiness(
  overrides: Partial<SetupReadinessReport> = {},
): SetupReadinessReport {
  return {
    schema: "kestrel.setup_readiness.v1",
    ready: true,
    experience_mode: "demo",
    pass_count: 2,
    warn_count: 0,
    fail_count: 0,
    checks: [
      {
        check_id: "provider_configuration",
        title: "Provider configuration",
        status: "pass",
        detail: "Deterministic Demo is selected.",
        recovery: "Choose a live provider later.",
      },
      {
        check_id: "workspace",
        title: "Workspace",
        status: "pass",
        detail: "The bundled workspace is available.",
        recovery: "Choose another folder in Settings.",
      },
    ],
    next_action: "Demo is ready.",
    ...overrides,
  };
}

function snapshot(
  overrides: Partial<SetupSnapshot> = {},
): SetupSnapshot {
  return {
    readiness: readiness(),
    catalogs: [demoCatalog],
    projects: [],
    secrets: [],
    runtime: {
      expectedRevision: "runtime-revision-1",
      provider: "mock",
      model: "mock",
      baseUrl: null,
      apiKeyEnv: null,
      workspace: "/workspace",
      memoryDir: "/workspace/.nest/memory",
    },
    ...overrides,
  };
}

function fakeApi(
  initial: SetupSnapshot,
  overrides: Partial<SetupCenterApi> = {},
): SetupCenterApi {
  let current = initial;
  return {
    supportsNativeProjectPicker: false,
    load: vi.fn(async () => current),
    saveIntelligence: vi.fn(async (selection) => {
      current = snapshot({
        ...current,
        readiness: readiness({
          experience_mode:
            selection.provider === "mock" ? "demo" : "connected",
        }),
        runtime: {
          ...current.runtime,
          provider: selection.provider,
          model: selection.model,
        },
      });
      return current;
    }),
    chooseProjectFolder: vi.fn(async () => ({
      status: "cancelled" as const,
    })),
    inspectProject: vi.fn(async () => projectDraft()),
    createProject: vi.fn(async (_input: ProjectCreateInput) => {
      current = snapshot({
        ...current,
        projects: [project],
      });
      return project;
    }),
    preflightFirstMission: vi.fn(async () =>
      firstMissionPreflight(),
    ),
    repairCore: vi.fn(async () => current),
    storeProviderCredential: vi.fn(async () => current),
    ...overrides,
  };
}

function projectDraft(
  overrides: Partial<ProjectSetupDraft> = {},
): ProjectSetupDraft {
  return {
    schema: "kestrel.project_setup_draft.v1",
    inspection: {
      canonical_path: "/workspace/kestrel",
      git: {
        branch: "trunk",
        state: "clean",
        summary: "Working tree is clean.",
      },
      index: {
        status: "not_created",
        detail:
          "No index is created during preview; build it explicitly after registration.",
      },
      test_recipes: [
        { name: "tests", command: "npm run test" },
      ],
      build_recipes: [
        { name: "build", command: "npm run build" },
      ],
      recipe_warnings: [],
    },
    create_input: {
      display_name: "kestrel",
      repository_path: "/workspace/kestrel",
      default_branch: "trunk",
      allowed_paths: ["."],
      provider_policy: {
        preset: "local_only",
        allowed_providers: ["mock"],
        allowed_models: ["mock"],
        direct_estimated_cost_usd: 0,
      },
      cost_budget: 0,
      privacy_class: "local_required",
      test_recipes: [
        { name: "tests", command: "npm run test" },
      ],
      build_recipes: [
        { name: "build", command: "npm run build" },
      ],
      capability_ceiling: firstMissionTools.map(
        (tool) => `tool:${tool}`,
      ),
    },
    first_mission: {
      template_id: "explain_repository",
      estimated_provider_calls: 3,
      can_start: true,
      required_tools: firstMissionTools,
      missing_tools: [],
      blockers: [],
    },
    ...overrides,
  };
}

function firstMissionPreflight(
  overrides: Partial<SetupFirstMissionPreflight> = {},
): SetupFirstMissionPreflight {
  return {
    projectId: project.project_id,
    projectRevision: project.revision,
    canStart: true,
    blockers: [],
    warnings: ["Repository index has not been built yet."],
    checks: [
      {
        id: "route",
        title: "Route",
        status: "warn",
        detail: "Demo route is deterministic.",
      },
    ],
    ...overrides,
  };
}

describe("SetupCenter", () => {
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("can continue offline with Demo and no project", async () => {
    const initial = snapshot({
      readiness: readiness({
        ready: false,
        experience_mode: "model_not_connected",
        pass_count: 1,
        warn_count: 1,
        fail_count: 1,
        checks: [
          {
            check_id: "provider_configuration",
            title: "Provider configuration",
            status: "fail",
            detail: "The live provider is not configured.",
            recovery: "Choose Demo or configure a provider in Settings.",
          },
          {
            check_id: "provider_operational",
            title: "Provider operational health",
            status: "warn",
            detail: "The network is offline.",
            recovery: "Continue with Demo while offline.",
          },
        ],
      }),
      runtime: {
        expectedRevision: "runtime-revision-1",
        provider: "openai",
        model: "gpt-5.5",
        baseUrl: null,
        apiKeyEnv: "OPENAI_API_KEY",
        workspace: "/workspace",
        memoryDir: "/workspace/.nest/memory",
      },
    });
    const api = fakeApi(initial);

    render(<SetupCenter api={api} />);

    expect(
      await screen.findByRole("heading", {
        name: "Choose intelligence",
      }),
    ).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Continue with Demo" }),
    );

    const projectHeading = await screen.findByRole("heading", {
      name: "Add a project",
    });
    await waitFor(() => expect(projectHeading).toHaveFocus(), { timeout: 5000 });
    expect(api.saveIntelligence).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "mock",
        model: "mock",
        expectedRevision: "runtime-revision-1",
      }),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Do this later" }),
    );
    await waitFor(
      () =>
        expect(
          screen.getByRole("heading", {
            name: "Review safety defaults",
          }),
        ).toHaveFocus(),
      { timeout: 5000 },
    );
    expect(
      screen.getByRole("button", {
        name: /Step 3.*skipped.*Project/i,
      }),
    ).toBeVisible();
  });

  it("resumes at the first incomplete server-backed stage after reload", async () => {
    render(<SetupCenter api={fakeApi(snapshot())} />);

    expect(
      await screen.findByRole("heading", { name: "Add a project" }),
    ).toBeVisible();
  });

  it("keeps the highest server-unlocked stage revisitable", async () => {
    render(<SetupCenter api={fakeApi(snapshot())} />);
    await screen.findByRole("heading", { name: "Add a project" });

    fireEvent.click(
      screen.getByRole("button", { name: /Step 1.*Core/i }),
    );
    expect(
      screen.getByRole("heading", {
        name: "Check the bundled core",
      }),
    ).toBeVisible();

    const projectStep = screen.getByRole("button", {
      name: /Step 3.*Project/i,
    });
    expect(projectStep).toBeEnabled();
    expect(
      projectStep.querySelector(".setup-progress-marker svg"),
    ).not.toHaveClass("lucide-check");
    fireEvent.click(projectStep);
    expect(
      screen.getByRole("heading", { name: "Add a project" }),
    ).toHaveFocus();
  });

  it("does not shrink the unlocked frontier when continuing from a revisited stage", async () => {
    render(
      <SetupCenter
        api={fakeApi(snapshot({ projects: [project] }))}
      />,
    );
    await screen.findByRole("heading", {
      name: "Review safety defaults",
    });

    fireEvent.click(
      screen.getByRole("button", { name: /Step 1.*Core/i }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Continue" }),
    );
    expect(
      screen.getByRole("heading", {
        name: "Choose intelligence",
      }),
    ).toBeVisible();

    const projectStep = screen.getByRole("button", {
      name: /Step 3.*Project/i,
    });
    expect(projectStep).toBeEnabled();
    fireEvent.click(projectStep);
    expect(
      screen.getByRole("heading", { name: "Add a project" }),
    ).toHaveFocus();
  });

  it("keeps command recovery inside Advanced diagnostics", async () => {
    const api = fakeApi(
      snapshot({
        readiness: readiness({
          ready: false,
          pass_count: 1,
          warn_count: 0,
          fail_count: 1,
          checks: [
            {
              check_id: "provider_configuration",
              title: "Provider configuration",
              status: "pass",
              detail: "Deterministic Demo is selected.",
              recovery: "Choose a live provider later.",
            },
            {
              check_id: "memory_storage",
              title: "Memory storage",
              status: "fail",
              detail: "The bundled memory directory is unavailable.",
              recovery:
                "Run `nest-agent init` after checking the storage location.",
            },
          ],
        }),
      }),
    );

    render(<SetupCenter api={api} />);

    expect(
      await screen.findByRole("heading", {
        name: "Check the bundled core",
      }),
    ).toBeVisible();
    expect(
      screen.getByText("The bundled memory directory is unavailable."),
    ).toBeVisible();
    expect(
      screen.getByRole("button", {
        name: "Review recovery for Memory storage",
      }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", {
        name: /Repair Memory storage in Settings/i,
      }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/nest-agent init/)).not.toBeVisible();

    fireEvent.click(
      screen.getByRole("button", {
        name: "Advanced diagnostics for Memory storage",
      }),
    );
    expect(screen.getByText(/nest-agent init/)).toBeVisible();
  });

  it("routes launch-controlled memory storage directly to restart recovery", async () => {
    const blocked = snapshot({
      readiness: readiness({
        ready: false,
        fail_count: 1,
        checks: [
          {
            check_id: "memory_storage",
            title: "Memory storage",
            status: "fail",
            detail: "The memory path is blocked.",
            recovery: "Choose a writable location.",
          },
        ],
      }),
    });
    const repairCore = vi.fn(async () => blocked);
    const openMemorySettings = vi.fn();
    const api = fakeApi(blocked, {
      supportsNativeWorkspacePicker: true,
      repairCore,
    });

    render(
      <SetupCenter
        api={api}
        navigation={{
          openGeneralSettings: vi.fn(),
          openProviderSettings: vi.fn(),
          openSafetySettings: vi.fn(),
          openMemorySettings,
          openMission: vi.fn(),
        }}
      />,
    );
    await screen.findByRole("heading", {
      name: "Check the bundled core",
    });
    const recovery = screen.getByRole("button", {
      name: "Review recovery for Memory storage",
    });
    expect(recovery).toHaveTextContent("Review restart recovery");
    fireEvent.click(recovery);

    expect(openMemorySettings).toHaveBeenCalledOnce();
    expect(repairCore).not.toHaveBeenCalled();
  });

  it("uses the native picker repair contract for a failed workspace check", async () => {
    const blocked = snapshot({
      readiness: readiness({
        ready: false,
        fail_count: 1,
        checks: [
          {
            check_id: "workspace",
            title: "Workspace",
            status: "fail",
            detail: "The workspace path is blocked.",
            recovery: "Choose a writable location.",
          },
        ],
      }),
    });
    const repaired = snapshot({
      readiness: readiness({
        checks: [
          {
            check_id: "workspace",
            title: "Workspace",
            status: "pass",
            detail: "The new workspace path is writable.",
            recovery: "No recovery needed.",
          },
        ],
      }),
    });
    const repairCore = vi.fn(async () => repaired);
    const api = fakeApi(blocked, {
      supportsNativeWorkspacePicker: true,
      repairCore,
    });

    render(<SetupCenter api={api} />);
    await screen.findByRole("heading", {
      name: "Check the bundled core",
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Choose a new location for Workspace",
      }),
    );

    await waitFor(() => {
      expect(repairCore).toHaveBeenCalledWith(
        "workspace",
        "runtime-revision-1",
      );
    });
    expect(
      await screen.findByText("The new workspace path is writable."),
    ).toBeVisible();
  });

  it("previews native project authority before saving", async () => {
    const createProject = vi.fn(async (_input: ProjectCreateInput) => project);
    const api = fakeApi(snapshot(), {
      supportsNativeProjectPicker: true,
      chooseProjectFolder: vi.fn(async () => ({
        status: "selected" as const,
        path: "/workspace/kestrel",
        displayLabel: "kestrel",
      })),
      createProject,
    });

    render(<SetupCenter api={api} />);
    await screen.findByRole("heading", { name: "Add a project" });
    fireEvent.click(
      screen.getByRole("button", { name: "Choose project folder" }),
    );

    expect(await screen.findByText("/workspace/kestrel")).toBeVisible();
    expect(screen.getByText(/trunk · clean/i)).toBeVisible();
    expect(screen.getByText("npm run test")).toBeVisible();
    expect(screen.getByText("npm run build")).toBeVisible();
    expect(screen.getByText(/7 currently active/i)).toBeVisible();
    expect(screen.getByText("Local models required")).toBeVisible();
    expect(screen.getByText(/registration can be archived/i)).toBeVisible();
    expect(
      screen.getByText(/No index is created during preview/i),
    ).toBeVisible();

    fireEvent.click(
      screen.getByRole("button", {
        name: "Save reviewed project",
      }),
    );
    await waitFor(() => {
      expect(createProject).toHaveBeenCalledWith({
        display_name: "kestrel",
        repository_path: "/workspace/kestrel",
        default_branch: "trunk",
        allowed_paths: ["."],
        provider_policy: {
          preset: "local_only",
          allowed_providers: ["mock"],
          allowed_models: ["mock"],
          direct_estimated_cost_usd: 0,
        },
        cost_budget: 0,
        privacy_class: "local_required",
        test_recipes: [
          { name: "tests", command: "npm run test" },
        ],
        build_recipes: [
          { name: "build", command: "npm run build" },
        ],
        capability_ceiling: firstMissionTools.map(
          (tool) => `tool:${tool}`,
        ),
      });
    });
    expect(
      screen.getByRole("heading", {
        name: "Review safety defaults",
      }),
    ).toBeVisible();
  });

  it("reviews safety without changing capability configuration", async () => {
    const api = fakeApi(snapshot());

    render(<SetupCenter api={api} />);
    await screen.findByRole("heading", { name: "Add a project" });
    fireEvent.click(
      screen.getByRole("button", { name: "Do this later" }),
    );

    expect(
      screen.getByText(/does not change any capability setting/i),
    ).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Verify and continue",
      }),
    );

    expect(
      await screen.findByRole("heading", {
        name: "Review first mission readiness",
      }),
    ).toBeVisible();
    expect(screen.getByText("A project is still required")).toBeVisible();
    expect(api.saveIntelligence).not.toHaveBeenCalled();
    expect(api.createProject).not.toHaveBeenCalled();
    expect(api.preflightFirstMission).not.toHaveBeenCalled();
  });

  it("relocks every downstream stage when refreshed server truth regresses", async () => {
    const healthy = snapshot({ projects: [project] });
    const regressed = snapshot({
      projects: [project],
      readiness: readiness({
        ready: false,
        fail_count: 1,
        checks: [
          {
            check_id: "provider_configuration",
            title: "Provider configuration",
            status: "pass",
            detail: "Demo is ready.",
            recovery: "No recovery needed.",
          },
          {
            check_id: "memory_storage",
            title: "Memory storage",
            status: "fail",
            detail: "Memory storage is no longer writable.",
            recovery: "Choose another storage location.",
          },
        ],
      }),
    });
    const load = vi
      .fn<SetupCenterApi["load"]>()
      .mockResolvedValueOnce(healthy)
      .mockResolvedValue(regressed);
    const api = fakeApi(healthy, { load });

    render(<SetupCenter api={api} />);
    await screen.findByRole("heading", {
      name: "Review safety defaults",
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Verify and continue",
      }),
    );

    expect(
      await screen.findByRole("heading", {
        name: "Check the bundled core",
      }),
    ).toBeVisible();
    const coreStep = screen.getByRole("button", {
      name: /Step 1.*Core/i,
    });
    expect(coreStep).toHaveAttribute("aria-current", "step");
    expect(
      screen.getByRole("button", { name: /Step 4.*Safety/i }),
    ).toBeDisabled();
    expect(
      screen.queryByText("First mission preflight passed"),
    ).not.toBeInTheDocument();
  });

  it("shows the real mission blocker instead of setup success", async () => {
    const initial = snapshot({ projects: [project] });
    const api = fakeApi(initial, {
      preflightFirstMission: vi.fn(async () =>
        firstMissionPreflight({
          canStart: false,
          blockers: [
            "Project provider policy requires a local provider.",
          ],
        }),
      ),
    });

    render(<SetupCenter api={api} />);
    await screen.findByRole("heading", {
      name: "Review safety defaults",
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Verify and continue",
      }),
    );

    expect(
      await screen.findByRole("heading", {
        name: "Review first mission readiness",
      }),
    ).toBeVisible();
    expect(
      screen.getByText("First mission is not runnable yet"),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Project provider policy requires a local provider.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByText("Setup review complete"),
    ).not.toBeInTheDocument();
  });

  it("relocks first mission to Safety after refreshing server truth", async () => {
    const initial = snapshot({ projects: [project] });
    const api = fakeApi(initial);

    render(<SetupCenter api={api} />);
    await screen.findByRole("heading", {
      name: "Review safety defaults",
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Verify and continue",
      }),
    );
    await screen.findByRole("heading", {
      name: "Start the first mission",
    });

    fireEvent.click(
      screen.getByRole("button", { name: /Step 1.*Core/i }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Check again" }),
    );

    await waitFor(() => {
      expect(api.load).toHaveBeenCalledTimes(3);
    });
    expect(
      screen.getByRole("button", {
        name: /Step 5.*First mission/i,
      }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /Step 4.*Safety/i }),
    ).toBeEnabled();
  });

  it("has no automated accessibility violations on the incomplete project stage", async () => {
    const { container } = render(
      <SetupCenter api={fakeApi(snapshot())} />,
    );
    await screen.findByRole("heading", { name: "Add a project" });

    const results = await axe.run(container, {
      rules: {
        "color-contrast": { enabled: false },
      },
    });
    expect(results.violations).toEqual([]);
  });
});
