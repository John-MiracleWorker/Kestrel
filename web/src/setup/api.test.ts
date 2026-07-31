import { afterEach, describe, expect, it, vi } from "vitest";
import {
  installFakeDesktopBridge,
  installFakeDesktopRuntimeMarker,
  removeFakeDesktopEnvironment,
} from "../testing/fakeDesktopBridge";
import type { SetupReadinessReport } from "../types";
import {
  createSetupCenterApi,
  loadSetupSnapshot,
} from "./api";

const readiness: SetupReadinessReport = {
  schema: "kestrel.setup_readiness.v1",
  ready: true,
  experience_mode: "demo",
  pass_count: 1,
  warn_count: 0,
  fail_count: 0,
  checks: [
    {
      check_id: "provider_configuration",
      title: "Provider configuration",
      status: "pass",
      detail: "Demo is selected.",
      recovery: "Choose a live model later.",
    },
  ],
  next_action: "Demo is ready.",
};

const catalog = {
  provider: "mock",
  models: ["mock"],
  fallback_models: ["mock"],
  source: "static",
  ok: true,
  fetchable: false,
  error: null,
  base_url_configured: false,
  api_key_env: null,
  api_key_configured: false,
  fetched_at: "2026-07-31T00:00:00Z",
};

const project = {
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
  capability_ceiling: [],
  baseline_index_digest: null,
  archived_at: null,
  revision: 1,
  created_at: "2026-07-31T00:00:00Z",
  updated_at: "2026-07-31T00:00:00Z",
};

const projectDraft = {
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
      detail: "Build the index explicitly after registration.",
    },
    test_recipes: [
      { name: "tests", command: "npm run test" },
    ],
    build_recipes: [],
    recipe_warnings: [],
  },
  create_input: {
    display_name: "kestrel",
    repository_path: "/workspace/kestrel",
    default_branch: "trunk",
    allowed_paths: ["."],
    provider_policy: {
      preset: "approved_cloud",
      allowed_providers: ["openai"],
      allowed_models: ["gpt-5.5"],
      direct_estimated_cost_usd: 0.2,
    },
    cost_budget: 1,
    privacy_class: "approved_cloud",
    test_recipes: [
      { name: "tests", command: "npm run test" },
    ],
    build_recipes: [],
    capability_ceiling: ["tool:file.read"],
  },
  first_mission: {
    template_id: "explain_repository",
    estimated_provider_calls: 3,
    can_start: true,
    required_tools: ["file.read"],
    missing_tools: [],
    blockers: [],
  },
};

describe("Setup Center API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    removeFakeDesktopEnvironment();
  });

  it("loads each server-backed setup source exactly once", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      fixtureFetch((path) => requests.push(path)),
    );

    const snapshot = await loadSetupSnapshot();

    expect(snapshot.readiness).toMatchObject({
      schema: "kestrel.setup_readiness.v1",
      experience_mode: "demo",
    });
    expect(snapshot.catalogs.map((item) => item.provider)).toEqual([
      "mock",
    ]);
    expect(snapshot.projects).toEqual([]);
    expect(snapshot.secrets).toEqual([]);
    expect(snapshot.runtime).toEqual({
      expectedRevision: "runtime-revision-1",
      provider: "mock",
      model: "mock",
      baseUrl: null,
      apiKeyEnv: null,
      workspace: "/workspace/kestrel",
      memoryDir: "/workspace/.nest/memory",
    });
    expect(requests).toEqual([
      "/api/product/setup",
      "/api/runtime/models",
      "/api/projects",
      "/api/secrets",
      "/api/runtime/settings",
    ]);
  });

  it("requests and parses the server-inspected project draft", async () => {
    const requests: Array<{
      path: string;
      method: string;
      body: unknown;
    }> = [];
    vi.stubGlobal(
      "fetch",
      fixtureFetch((path, init) => {
        requests.push({
          path,
          method: init?.method ?? "GET",
          body:
            typeof init?.body === "string"
              ? JSON.parse(init.body)
              : null,
        });
      }),
    );
    const api = createSetupCenterApi();

    const draft = await api.inspectProject({
      repositoryPath: "/workspace/kestrel",
      directEstimatedCostUsd: 0.2,
      costBudget: 1,
    });

    expect(requests[0]).toEqual({
      path: "/api/projects/setup-draft",
      method: "POST",
      body: {
        repository_path: "/workspace/kestrel",
        direct_estimated_cost_usd: 0.2,
        cost_budget: 1,
      },
    });
    expect(draft.inspection.git.branch).toBe("trunk");
    expect(draft.create_input.privacy_class).toBe("approved_cloud");
    expect(draft.first_mission.can_start).toBe(true);
  });

  it("runs the exact read-only first mission preflight", async () => {
    const requests: Array<{ path: string; body: unknown }> = [];
    vi.stubGlobal(
      "fetch",
      fixtureFetch((path, init) => {
        requests.push({
          path,
          body:
            typeof init?.body === "string"
              ? JSON.parse(init.body)
              : null,
        });
      }),
    );
    const api = createSetupCenterApi();

    const result = await api.preflightFirstMission("project/kestrel");

    expect(requests[0]).toMatchObject({
      path:
        "/api/projects/project%2Fkestrel/mission/preflight",
      body: {
        template_id: "explain_repository",
      },
    });
    expect(result).toMatchObject({
      projectId: "project_kestrel",
      projectRevision: 1,
      canStart: true,
    });
  });

  it("saves intelligence through the revisioned runtime contract", async () => {
    const requests: Array<{
      path: string;
      method: string;
      body: unknown;
    }> = [];
    vi.stubGlobal(
      "fetch",
      fixtureFetch((path, init) => {
        requests.push({
          path,
          method: init?.method ?? "GET",
          body:
            typeof init?.body === "string"
              ? JSON.parse(init.body)
              : null,
        });
      }),
    );
    const api = createSetupCenterApi();

    await api.saveIntelligence({
      expectedRevision: "runtime-revision-1",
      provider: "mock",
      model: "mock",
      baseUrl: null,
      apiKeyEnv: null,
    });

    expect(requests[0]).toEqual({
      path: "/api/runtime/settings",
      method: "PUT",
      body: {
        expected_revision: "runtime-revision-1",
        provider: "mock",
        model: "mock",
        base_url: null,
        api_key_env: null,
      },
    });
  });

  it("posts the reviewed empty project capability ceiling unchanged", async () => {
    const requests: Array<{ path: string; body: unknown }> = [];
    vi.stubGlobal(
      "fetch",
      fixtureFetch((path, init) => {
        requests.push({
          path,
          body:
            typeof init?.body === "string"
              ? JSON.parse(init.body)
              : null,
        });
      }),
    );
    const api = createSetupCenterApi();

    await api.createProject({
      display_name: "Kestrel",
      repository_path: "/workspace/kestrel",
      default_branch: "main",
      allowed_paths: ["."],
      provider_policy: { preset: "local_only" },
      cost_budget: 0,
      privacy_class: "local_required",
      test_recipes: [],
      build_recipes: [],
      capability_ceiling: [],
    });

    expect(requests[0]).toMatchObject({
      path: "/api/projects",
      body: {
        provider_policy: { preset: "local_only" },
        capability_ceiling: [],
      },
    });
  });

  it("rejects an untrusted relative folder response in the renderer", async () => {
    installFakeDesktopBridge({
      chooseProjectFolder: async () => ({
        status: "selected",
        path: "relative/project",
        displayLabel: "project",
      }),
    });
    installFakeDesktopRuntimeMarker();
    const api = createSetupCenterApi();

    expect(api.supportsNativeProjectPicker).toBe(true);
    await expect(api.chooseProjectFolder()).rejects.toThrow(
      "desktop_folder_path_invalid",
    );
  });

  it("rejects absolute dot-segment folder injection in the renderer", async () => {
    installFakeDesktopBridge({
      chooseProjectFolder: async () => ({
        status: "selected",
        path: "/private/allowed/../outside",
        displayLabel: "outside",
      }),
    });
    const api = createSetupCenterApi();

    await expect(api.chooseProjectFolder()).rejects.toThrow(
      "desktop_folder_path_invalid",
    );
  });

  it("refuses a memory-storage repair that Desktop launch authority would discard", async () => {
    const requests: Array<{
      path: string;
      method: string;
      body: unknown;
    }> = [];
    const chooseStorageFolder = vi.fn(async () => ({
      status: "selected",
      path: "/private/kestrel-storage",
      displayLabel: "kestrel-storage",
    }));
    installFakeDesktopBridge({
      chooseStorageFolder,
    });
    installFakeDesktopRuntimeMarker();
    vi.stubGlobal(
      "fetch",
      fixtureFetch((path, init) => {
        requests.push({
          path,
          method: init?.method ?? "GET",
          body:
            typeof init?.body === "string"
              ? JSON.parse(init.body)
              : null,
        });
      }),
    );
    const api = createSetupCenterApi();

    await expect(
      api.repairCore("memory_storage", "runtime-revision-1"),
    ).rejects.toThrow("core_check_has_no_bounded_gui_repair");

    expect(chooseStorageFolder).not.toHaveBeenCalled();
    expect(requests).toEqual([]);
  });

  it("applies a native workspace repair through revisioned runtime settings", async () => {
    const requests: Array<{
      path: string;
      method: string;
      body: unknown;
    }> = [];
    installFakeDesktopBridge({
      chooseProjectFolder: async () => ({
        status: "selected",
        path: "/private/kestrel-workspace",
        displayLabel: "kestrel-workspace",
      }),
    });
    installFakeDesktopRuntimeMarker();
    vi.stubGlobal(
      "fetch",
      fixtureFetch((path, init) => {
        requests.push({
          path,
          method: init?.method ?? "GET",
          body:
            typeof init?.body === "string"
              ? JSON.parse(init.body)
              : null,
        });
      }),
    );
    const api = createSetupCenterApi();

    const next = await api.repairCore(
      "workspace",
      "runtime-revision-1",
    );

    expect(api.supportsNativeWorkspacePicker).toBe(true);
    expect(requests[0]).toEqual({
      path: "/api/runtime/settings",
      method: "PUT",
      body: {
        expected_revision: "runtime-revision-1",
        workspace: "/private/kestrel-workspace",
      },
    });
    expect(next.runtime.workspace).toBe(
      "/private/kestrel-workspace",
    );
  });

  it("fails closed when project count evidence disagrees with the list", async () => {
    vi.stubGlobal(
      "fetch",
      fixtureFetch(undefined, {
        projects: { items: [], count: 1 },
      }),
    );

    await expect(loadSetupSnapshot()).rejects.toThrow(
      "project_count_mismatch",
    );
  });
});

function fixtureFetch(
  onRequest?: (path: string, init?: RequestInit) => void,
  overrides: {
    projects?: unknown;
  } = {},
): typeof fetch {
  let runtimeMemoryDir = "/workspace/.nest/memory";
  let runtimeWorkspace = "/workspace/kestrel";
  return async (input, init) => {
    const path =
      typeof input === "string"
        ? input.startsWith("http://") ||
          input.startsWith("https://")
          ? `${new URL(input).pathname}${new URL(input).search}`
          : input
        : input instanceof URL
          ? `${input.pathname}${input.search}`
          : `${new URL(input.url).pathname}${new URL(input.url).search}`;
    onRequest?.(path, init);
    if (path === "/api/runtime/settings" && init?.method === "PUT") {
      const body =
        typeof init.body === "string"
          ? (JSON.parse(init.body) as Record<string, unknown>)
          : {};
      if (typeof body.memory_dir === "string") {
        runtimeMemoryDir = body.memory_dir;
      }
      if (typeof body.workspace === "string") {
        runtimeWorkspace = body.workspace;
      }
      return response({
        settings: {
          provider: "mock",
          model: "mock",
          revision: "runtime-revision-2",
        },
      });
    }
    if (path === "/api/projects" && init?.method === "POST") {
      return response(project, 201);
    }
    if (
      path === "/api/projects/setup-draft" &&
      init?.method === "POST"
    ) {
      return response(projectDraft);
    }
    if (
      path.endsWith("/mission/preflight") &&
      init?.method === "POST"
    ) {
      return response({
        project_id: "project_kestrel",
        project_revision: 1,
        can_start: true,
        blockers: [],
        warnings: ["Index missing."],
        checks: [
          {
            check_id: "route",
            title: "Route",
            status: "warn",
            detail: "Demo route.",
          },
        ],
      });
    }
    const payloads: Record<string, unknown> = {
      "/api/product/setup": readiness,
      "/api/runtime/models": { providers: [catalog] },
      "/api/projects":
        overrides.projects ?? { items: [], count: 0 },
      "/api/secrets": [],
      "/api/runtime/settings": {
        settings: {
          provider: "mock",
          model: "mock",
          base_url: null,
          api_key_env: null,
          workspace: runtimeWorkspace,
          memory_dir: runtimeMemoryDir,
          revision: "runtime-revision-1",
        },
      },
    };
    if (!(path in payloads)) {
      throw new Error(`unexpected_request:${path}`);
    }
    return response(payloads[path]);
  };
}

function response(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
