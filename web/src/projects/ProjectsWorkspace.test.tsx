import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectsWorkspace } from "./ProjectsWorkspace";
import {
  installFakeDesktopBridge,
  installFakeDesktopRuntimeMarker,
  removeFakeDesktopEnvironment,
} from "../testing/fakeDesktopBridge";

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const projectFixture = {
  project_id: "project_demo",
  display_name: "Demo Repository",
  repository_path: "/workspace/repo",
  remote: null,
  default_branch: "main",
  allowed_paths: ["/workspace/repo", "/workspace/repo-shared"],
  provider_policy: { privacy: "local_only" },
  cost_budget: 5,
  privacy_class: "local_required",
  test_recipes: [
    { name: "pytest", command: "pytest -q", working_directory: null },
  ],
  build_recipes: [
    { name: "build", command: "npm run build", working_directory: null },
  ],
  capability_ceiling: ["tool:file.read", "tool:file.write"],
  baseline_index_digest: "abc123",
  archived_at: null,
  revision: 3,
  created_at: "2026-07-30T10:00:00Z",
  updated_at: "2026-07-30T12:00:00Z",
};

const setupDraftFixture = {
  schema: "kestrel.project_setup_draft.v1",
  inspection: {
    canonical_path: "/workspace/repo",
    git: { branch: "main", state: "clean", summary: "clean working tree" },
    index: {
      status: "not_created",
      detail: "No repository index exists yet.",
    },
    test_recipes: [
      { name: "pytest", command: "pytest -q", working_directory: null },
    ],
    build_recipes: [],
    recipe_warnings: [],
  },
  create_input: {
    display_name: "repo",
    repository_path: "/workspace/repo",
    default_branch: "main",
    allowed_paths: ["/workspace/repo"],
    provider_policy: { privacy: "local_only" },
    cost_budget: null,
    privacy_class: "local_required",
    test_recipes: [
      { name: "pytest", command: "pytest -q", working_directory: null },
    ],
    build_recipes: [],
    capability_ceiling: ["tool:file.read"],
  },
  first_mission: {
    template_id: "explain_repository",
    estimated_provider_calls: 1,
    can_start: true,
    required_tools: ["file.read"],
    missing_tools: [],
    blockers: [],
  },
};

function missionProps() {
  return {
    runs: [],
    activeRun: null,
    taskGraph: null,
    approvals: [],
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
}

function stubProjectsFetch(
  overrides: Record<string, { body: unknown; status?: number }> = {},
) {
  const requests: string[] = [];
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === "string" ? input : input.toString();
      // Record METHOD:path so list GETs are distinguishable from save POSTs
      // (both hit /api/projects; the desktop transport makes them absolute).
      requests.push(`${init?.method ?? "GET"}:${path}`);
      const override = overrides[path];
    if (override) {
      return jsonResponse(override.body, override.status ?? 200);
    }
    // Desktop runtime transport resolves relative paths against the marker
    // base URL, so match by suffix rather than exact string equality.
    if (path.endsWith("/api/projects")) {
      return jsonResponse({ items: [projectFixture], count: 1 });
    }
    if (path.endsWith("/api/projects/project_demo/index")) {
      return jsonResponse({
        schema: "kestrel.project_index_status.v1",
        project_id: "project_demo",
        project_revision: 3,
        status: "ready",
        freshness: "current",
        aggregate_digest: "abc123",
        indexed_at: "2026-07-30T11:00:00Z",
        detail: "Repository index matches the current project snapshot.",
      });
    }
    if (path.endsWith("/api/projects/setup-draft")) {
      return jsonResponse(setupDraftFixture);
    }
    throw new Error(`unexpected_request:${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { requests, fetchMock };
}

describe("ProjectsWorkspace", () => {
  beforeEach(() => {
    removeFakeDesktopEnvironment();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    removeFakeDesktopEnvironment();
  });

  it("keeps Mission command reachable from the projects surface", async () => {
    stubProjectsFetch();
    render(<ProjectsWorkspace {...missionProps()} />);
    expect(
      await screen.findByRole("heading", {
        name: "What should Kestrel accomplish?",
      }),
    ).toBeVisible();
  });

  it("lists project profiles with authority facts from the server", async () => {
    stubProjectsFetch();
    render(<ProjectsWorkspace {...missionProps()} />);

    const region = await screen.findByRole("region", {
      name: "Project profiles",
    });
    expect(
      await within(region).findByRole(
        "row",
        { name: /Demo Repository/i },
        { timeout: 3000 },
      ),
    ).toHaveTextContent("local_required");
    expect(within(region).getByText(/rev 3/)).toBeVisible();
    // Renderer never claims health the server did not provide.
    expect(within(region).queryByText(/healthy/i)).not.toBeInTheDocument();
  });

  it("shows server-provided index freshness for the selected project", async () => {
    stubProjectsFetch();
    render(<ProjectsWorkspace {...missionProps()} />);

    const region = await screen.findByRole("region", {
      name: "Index freshness",
    });
    // "current" appears in both the freshness StatusBadge and the
    // server-provided detail copy — assert both, not a singleton.
    expect(await within(region).findAllByText(/current/i)).toHaveLength(2);
    expect(
      within(region).getByText(
        /Repository index matches the current project snapshot/i,
      ),
    ).toBeVisible();
  });

  it("uses a native picker and previews project authority before save", async () => {
    const { requests } = stubProjectsFetch();
    installFakeDesktopBridge({
      chooseProjectFolder: async () => ({
        status: "selected",
        path: "/workspace/repo",
      }),
    });
    // Bridge without the runtime marker makes runtimeTransport() throw
    // desktop_runtime_marker_unavailable; the picker flow needs both.
    installFakeDesktopRuntimeMarker();
    render(<ProjectsWorkspace {...missionProps()} />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Add project" }),
    );

    // The chosen path appears in the project list and possibly elsewhere;
    // the assertion that matters is the authority preview region (rendered
    // as a plain section, so scope via its heading's closest section).
    const previewHeading = await screen.findByRole("heading", {
      name: "Project authority preview",
    });
    const preview = previewHeading.closest("section");
    expect(preview).not.toBeNull();
    // The path appears twice in the preview: the inspected Repository fact
    // and the allowed-path ceiling entry.
    expect(
      within(preview as HTMLElement).getAllByText("/workspace/repo"),
    ).toHaveLength(2);
    expect(
      within(preview as HTMLElement).getByText(/allowed path ceiling/i),
    ).toBeVisible();
    // At least one list GET and exactly one inspection POST — and crucially
    // no save POST until the owner confirms the preview.
    const listGets = requests.filter(
      (entry) =>
        entry.startsWith("GET:") && entry.endsWith("/api/projects"),
    );
    expect(listGets.length).toBeGreaterThanOrEqual(1);
    const inspectionPosts = requests.filter(
      (entry) =>
        entry.startsWith("POST:") &&
        entry.endsWith("/api/projects/setup-draft"),
    );
    expect(inspectionPosts).toHaveLength(1);
    const savePosts = requests.filter(
      (entry) =>
        entry.startsWith("POST:") && entry.endsWith("/api/projects"),
    );
    expect(savePosts).toHaveLength(0);
  });

  it("keeps every move-storage action disabled and linked to the packaging plan", async () => {
    stubProjectsFetch();
    render(<ProjectsWorkspace {...missionProps()} />);

    const moveButton = await screen.findByRole("button", {
      name: /move storage/i,
    });
    expect(moveButton).toBeDisabled();
    expect(moveButton).toHaveAttribute(
      "aria-describedby",
      expect.stringContaining("move-storage"),
    );
    // The plan copy appears in the explanatory paragraph and in the
    // aria-describedby target for the disabled button — assert both.
    expect(screen.getAllByText(/packaging\/recovery plan/i)).toHaveLength(2);
  });

  it("surfaces server truth when project reads fail", async () => {
    stubProjectsFetch({
      "/api/projects": { body: { detail: "boom" }, status: 500 },
    });
    render(<ProjectsWorkspace {...missionProps()} />);

    expect(
      await screen.findByRole("region", { name: "Project profiles" }),
    ).toHaveTextContent(/unavailable|failed|error/i);
  });
});
