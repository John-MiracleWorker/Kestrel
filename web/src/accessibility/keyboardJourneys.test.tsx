import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
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
import { createFixtureFetch } from "../testing/apiFixtures";
import type { ProviderModelCatalog, SetupReadinessReport } from "../types";
import type { ProjectProfile } from "../mission/types";
import { SetupCenter } from "../setup/SetupCenter";
import type {
  ProjectCreateInput,
  ProjectSetupDraft,
  SetupCenterApi,
  SetupFirstMissionPreflight,
  SetupSnapshot,
} from "../setup/types";
import { gateProject } from "./fixtures";

class JourneyEventSource {
  onmessage: ((event: MessageEvent) => void) | null = null;
  private listeners = new Map<string, Array<(event: MessageEvent) => void>>();

  constructor(readonly url: string) {}

  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close = vi.fn();
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

/**
 * jsdom does not implement sequential-focus Tab navigation, so journeys are
 * asserted as DOM-order contracts (focusable candidates in document order
 * must include the expected sequence) plus direct focus()/toHaveFocus()
 * checks at each step. This is a DOM-order gate, not browser fidelity.
 */
function focusableIn(container: ParentNode): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)].filter(
    (element) => !element.hasAttribute("inert"),
  );
}

describe("Setup Center keyboard journey", () => {
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("walks Core → Project → Safety → First mission with heading focus restoration", async () => {
    const api = fakeSetupApi(setupSnapshot());
    const { container } = render(<SetupCenter api={api} />);

    // Stage buttons are stepwise keyboard targets; the current stage carries
    // aria-current="step".
    const projectStageHeading = await screen.findByRole("heading", {
      name: "Add a project",
    });
    expect(projectStageHeading).toBeVisible();
    const currentStep = container.querySelector('[aria-current="step"]');
    expect(currentStep).not.toBeNull();
    expect(currentStep).toHaveAccessibleName(/Step 3.*Project/i);

    // Focusable candidates must include the unlocked progress steps before
    // the stage actions (DOM order == tab order contract). Upcoming steps
    // are disabled — they are not keyboard targets until unlocked.
    const candidates = focusableIn(container);
    const stepButtons = candidates.filter((element) =>
      /Step \d/.test(element.textContent ?? ""),
    );
    expect(stepButtons.length).toBeGreaterThanOrEqual(3);
    const skipIndex = candidates.findIndex(
      (element) => element.textContent === "Do this later",
    );
    expect(skipIndex).toBeGreaterThan(-1);
    expect(
      candidates.findIndex((element) =>
        /Step 3/.test(element.textContent ?? ""),
      ),
    ).toBeLessThan(skipIndex);

    // Skipping the project advances to Safety and restores focus on the new
    // stage heading — the load-bearing focus-restoration assertion.
    fireEvent.click(screen.getByRole("button", { name: "Do this later" }));
    expect(
      screen.getByRole("heading", { name: "Review safety defaults" }),
    ).toHaveFocus();
    expect(
      screen.getByRole("button", { name: /Step 4.*Safety/i }),
    ).toHaveAttribute("aria-current", "step");

    fireEvent.click(screen.getByRole("button", { name: "Verify and continue" }));
    const readinessHeading = await screen.findByRole("heading", {
      name: "Review first mission readiness",
    });
    expect(readinessHeading).toHaveFocus();
    expect(
      screen.getByRole("button", { name: /Step 5.*First mission/i }),
    ).toHaveAttribute("aria-current", "step");

    // First-mission readiness with a registered project reaches the start
    // stage; the primary action remains keyboard reachable.
    const openMission = await screen.findByRole("button", {
      name: /Open Mission Command|Review in Mission Command/i,
    });
    openMission.focus();
    expect(openMission).toHaveFocus();
  });

  it("keeps revisited stage headings focusable when stepping backward", async () => {
    const api = fakeSetupApi(setupSnapshot());
    render(<SetupCenter api={api} />);

    await screen.findByRole("heading", { name: "Add a project" });
    fireEvent.click(screen.getByRole("button", { name: "Do this later" }));
    expect(
      screen.getByRole("heading", { name: "Review safety defaults" }),
    ).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: /Step 1.*Core/i }));
    expect(
      screen.getByRole("heading", { name: "Check the bundled core" }),
    ).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: /Step 3.*Project/i }));
    expect(
      screen.getByRole("heading", { name: "Add a project" }),
    ).toHaveFocus();
  });
});

describe("First-mission keyboard journey (App-level)", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/#/mission/command");
    vi.stubGlobal("fetch", createFixtureFetch());
    vi.stubGlobal("EventSource", JourneyEventSource);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it("keeps the seven destinations in document order and marks the current page", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "What should Kestrel accomplish?" });

    const navigation = screen.getByRole("navigation", {
      name: "Workbench destinations",
    });
    const links = [
      ...navigation.querySelectorAll<HTMLAnchorElement>("a[data-destination]"),
    ];
    expect(links).toHaveLength(7);
    expect(links.every((link) => link.tabIndex === 0)).toBe(true);
    expect(
      screen.getByRole("link", { name: "Mission" }),
    ).toHaveAttribute("aria-current", "page");

    // Keyboard document order: navigation → main → (no context rail yet).
    const main = screen.getByRole("main");
    expect(
      navigation.compareDocumentPosition(main) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // Each destination link is focusable and activates navigation.
    for (const link of links) {
      (link as HTMLElement).focus();
      expect(link).toHaveFocus();
    }
  });

  it("moves from Mission to Settings and back with focus restoration on the main region", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "What should Kestrel accomplish?" });

    const settingsLink = screen.getByRole("link", { name: "Settings" });
    settingsLink.focus();
    expect(settingsLink).toHaveFocus();
    fireEvent.click(settingsLink);

    await screen.findByRole("heading", { name: "Settings." });
    expect(
      screen.getByRole("link", { name: "Settings" }),
    ).toHaveAttribute("aria-current", "page");

    const main = screen.getByRole("main");
    main.focus();
    expect(main).toHaveFocus();

    const missionLink = screen.getByRole("link", { name: "Mission" });
    missionLink.focus();
    fireEvent.click(missionLink);
    await screen.findByRole("heading", { name: "What should Kestrel accomplish?" });
    expect(
      screen.getByRole("link", { name: "Mission" }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("isolates outside navigation while the command palette is modal and restores it after close", async () => {
    render(<App />);
    await screen.findByRole("heading", { name: "What should Kestrel accomplish?" });

    const navigation = screen.getByRole("navigation", {
      name: "Workbench destinations",
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Open command palette" }),
    );

    const palette = await screen.findByRole("dialog");
    const mission = screen.getByRole("link", { name: "Mission" });
    expect(mission).toHaveAttribute("aria-disabled", "true");
    expect(mission).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("main").parentElement).toHaveAttribute("inert");

    const paletteFocusables = focusableIn(palette);
    expect(paletteFocusables.length).toBeGreaterThan(0);
    paletteFocusables[0].focus();
    expect(paletteFocusables[0]).toHaveFocus();

    fireEvent.keyDown(palette, { key: "Escape" });
    await vi.waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("main").parentElement).not.toHaveAttribute("inert");
    const links = [
      ...navigation.querySelectorAll<HTMLAnchorElement>("a[data-destination]"),
    ];
    expect(links.every((link) => link.tabIndex === 0)).toBe(true);
  });
});

/* Setup Center fixtures — replicated locally (importing from a *.test.tsx
 * re-runs that module's describes in every importer). */

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
