import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { App } from "../App";
import { DESTINATIONS } from "../app/destinations";
import { ApprovalQueue } from "../mission/ApprovalQueue";
import { MissionControl } from "../mission/MissionControl";
import type { ProjectProfile } from "../mission/types";
import { SettingControl } from "../settings/SettingControl";
import { blockedWebSearchSetting } from "../settings/testFixtures";
import { createFixtureFetch } from "../testing/apiFixtures";
import type { ProviderModelCatalog, SetupReadinessReport } from "../types";
import { SetupCenter } from "../setup/SetupCenter";
import type {
  ProjectCreateInput,
  ProjectSetupDraft,
  SetupCenterApi,
  SetupFirstMissionPreflight,
  SetupSnapshot,
} from "../setup/types";
import {
  formatViolations,
  runWorkbenchAxe,
  seriousViolations,
} from "./axeGate";
import {
  gateApproval,
  gateProject,
  gateRun,
  gateTaskGraph,
  missionControlProps,
  stubProjectsFetch,
} from "./fixtures";

class GateEventSource {
  static instances: GateEventSource[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  private listeners = new Map<string, Array<(event: MessageEvent) => void>>();

  constructor(readonly url: string) {
    GateEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close = vi.fn();
}

function installGateEnvironment() {
  vi.stubGlobal("fetch", createFixtureFetch());
  vi.stubGlobal("EventSource", GateEventSource);
}

function restoreGateEnvironment() {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
}

async function expectNoSeriousViolations(container: Element, screenName: string) {
  const report = await runWorkbenchAxe(container);
  const serious = seriousViolations(report);
  expect(
    serious,
    `${screenName} serious/critical axe violations:\n${formatViolations(serious)}`,
  ).toEqual([]);
}

async function waitForAppHeading() {
  // Mission command and legacy chat surfaces render "Ask Kestrel" first;
  // every destination eventually renders a level-1 heading inside <main>.
  await waitFor(() => {
    const main = screen.getByRole("main");
    expect(main.querySelector("h1")).not.toBeNull();
  });
}

describe("Workbench axe gates — destinations", () => {
  beforeEach(() => {
    installGateEnvironment();
  });

  afterEach(() => {
    restoreGateEnvironment();
  });

  it.each(
    DESTINATIONS.map((destination) => ({
      id: destination.id,
      label: destination.label,
      route: `/${destination.id}/${destination.defaultSubroute}`,
    })),
  )(
    "has no serious violations at #$route ($id)",
    async ({ route, id, label }) => {
      window.history.replaceState(null, "", `/#${route}`);
      const { container } = render(<App />);
      await waitForAppHeading();
      expect(screen.getByRole("link", { name: label })).toHaveAttribute(
        "aria-current",
        "page",
      );
      await expectNoSeriousViolations(container, `destination ${id}`);
    },
  );

  it("has no serious violations in the unknown-route recovery state", async () => {
    window.history.replaceState(null, "", "/#/not-a-destination");
    const { container } = render(<App />);
    await screen.findByRole("status", { name: "Route recovery" });
    await waitForAppHeading();
    await expectNoSeriousViolations(container, "route recovery");
  });
});

describe("Workbench axe gates — mission states", () => {
  afterEach(() => {
    restoreGateEnvironment();
  });

  it("has no serious violations on the mission preflight composer", async () => {
    vi.stubGlobal("fetch", stubProjectsFetch());
    vi.stubGlobal("EventSource", GateEventSource);
    const { container } = render(<MissionControl {...missionControlProps} />);
    await screen.findByRole("heading", {
      name: "What should Kestrel accomplish?",
    });
    await expectNoSeriousViolations(container, "mission preflight");
  });

  it("has no serious violations on an active mission", async () => {
    vi.stubGlobal("fetch", stubProjectsFetch());
    vi.stubGlobal("EventSource", GateEventSource);
    const { container } = render(
      <MissionControl
        {...missionControlProps}
        runs={[gateRun]}
        activeRun={gateRun}
        taskGraph={gateTaskGraph}
      />,
    );
    await screen.findAllByText("Fix the failing authentication test");
    await expectNoSeriousViolations(container, "active mission");
  });

  it("has no serious violations on a pending approval packet", async () => {
    const { container } = render(
      <ApprovalQueue approvals={[gateApproval]} onDecision={() => undefined} />,
    );
    await screen.findByRole("heading", { name: "Exact-call approvals" });
    await expectNoSeriousViolations(container, "approval packet");
  });
});

describe("Workbench axe gates — settings and setup states", () => {
  afterEach(() => {
    restoreGateEnvironment();
  });

  it("has no serious violations on a blocked setting control", async () => {
    const { container } = render(
      <SettingControl
        setting={blockedWebSearchSetting}
        onCommitted={() => undefined}
      />,
    );
    await screen.findByText("Currently blocked");
    await expectNoSeriousViolations(container, "blocked setting control");
  });

  it("has no serious violations across the Setup Center journey", async () => {
    const api = fakeSetupApi(setupSnapshot());
    const { container } = render(<SetupCenter api={api} />);

    await screen.findByRole("heading", { name: "Add a project" });
    await expectNoSeriousViolations(container, "setup project stage");

    fireEvent.click(
      await screen.findByRole("button", { name: "Do this later" }),
    );
    await screen.findByRole("heading", { name: "Review safety defaults" });
    await expectNoSeriousViolations(container, "setup safety stage");

    fireEvent.click(screen.getByRole("button", { name: "Verify and continue" }));
    await screen.findByRole("heading", {
      name: "Review first mission readiness",
    });
    await expectNoSeriousViolations(container, "setup first-mission stage");
  });
});

/* Setup Center fixtures — replicated locally from SetupCenter.test.tsx.
 * (Importing from a *.test.tsx re-runs that module's describes here.) */

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

const setupProject: ProjectProfile = {
  ...gateProject,
  repository_path: "/workspace/kestrel",
  capability_ceiling: firstMissionTools.map((tool) => `tool:${tool}`),
  baseline_index_digest: null,
};

function setupReadiness(
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

function setupSnapshot(
  overrides: Partial<SetupSnapshot> = {},
): SetupSnapshot {
  return {
    readiness: setupReadiness(),
    catalogs: [demoCatalog],
    // Empty by default so the journey starts at the Project stage; tests
    // that need a registered project opt in via overrides.
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

function fakeSetupApi(
  initial: SetupSnapshot,
  overrides: Partial<SetupCenterApi> = {},
): SetupCenterApi {
  let current = initial;
  return {
    supportsNativeProjectPicker: false,
    load: vi.fn(async () => current),
    saveIntelligence: vi.fn(async (selection) => {
      current = setupSnapshot({
        ...current,
        readiness: setupReadiness({
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
      current = setupSnapshot({ ...current, projects: [setupProject] });
      return setupProject;
    }),
    preflightFirstMission: vi.fn(async () => firstMissionPreflight()),
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
      test_recipes: [{ name: "tests", command: "npm run test" }],
      build_recipes: [{ name: "build", command: "npm run build" }],
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
      test_recipes: [{ name: "tests", command: "npm run test" }],
      build_recipes: [{ name: "build", command: "npm run build" }],
      capability_ceiling: firstMissionTools.map((tool) => `tool:${tool}`),
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
    projectId: setupProject.project_id,
    projectRevision: setupProject.revision,
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
