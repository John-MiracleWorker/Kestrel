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
import type { ComponentProps, FormEvent } from "react";
import { App } from "../App";
import { createFixtureFetch } from "../testing/apiFixtures";
import type { PluginReviewReport } from "../types";
import { PluginsPanel } from "./PluginsPanel";

describe("Extend workspace ownership", () => {
  const requests: string[] = [];

  beforeEach(() => {
    requests.length = 0;
    window.history.replaceState(null, "", "/#/mission/command");
    vi.stubGlobal(
      "fetch",
      createFixtureFetch((request) => {
        requests.push(request.path);
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it("does not load extension inventory for Mission and loads it when Extend becomes active", async () => {
    render(<App />);

    expect(
      await screen.findByRole("heading", {
        name: "What should Kestrel accomplish?",
      }),
    ).toBeVisible();
    await waitFor(() => {
      expect(requests).toContain("/api/runtime/models?provider=mock");
    });
    expect(requests).not.toContain("/api/plugins");
    expect(requests).not.toContain("/api/mcp/servers");

    fireEvent.click(
      screen.getByRole("link", { name: "Extend" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Advanced." }),
    ).toBeVisible();
    await waitFor(() => {
      expect(requests).toContain("/api/plugins");
      expect(requests).toContain("/api/mcp/servers");
    });
  });

  it("owns the operator landmark and extension filter state", async () => {
    window.history.replaceState(null, "", "/#/extend");
    render(<App />);

    const workspace = await screen.findByRole("region", {
      name: "Advanced Operator Console",
    });
    expect(workspace).toContainElement(
      screen.getByRole("heading", { name: "Advanced." }),
    );

    const filter = await screen.findByLabelText("Filter tools");
    fireEvent.change(filter, { target: { value: "memory" } });
    expect(filter).toHaveValue("memory");
  });
});

function reviewFixture(blockers: string[]): PluginReviewReport {
  return {
    source_url: "owner/repo",
    source_ref: null,
    commit_sha: "abcdef1234567890",
    manifest: { id: "reviewed" },
    capabilities: [],
    risk_report: { risk: "medium", enable_blockers: blockers },
    dependency_review: { declared: {} },
    isolation_review: { mode: "container", required: true, available: false },
    compatibility_review: { status: "compatible" },
    provenance_review: { status: "verified" },
    enable_blockers: blockers,
    warnings: [],
    unsupported_features: []
  };
}

function pluginsPanelProps(
  overrides: Partial<ComponentProps<typeof PluginsPanel>> = {}
): ComponentProps<typeof PluginsPanel> {
  return {
    plugins: [],
    pluginSource: "owner/repo",
    pluginRef: "",
    pluginEnable: false,
    pluginResult: null,
    pluginReview: null,
    pluginUpdateReviews: {},
    reviewedCurrentPlugin: false,
    pluginEnableBlockers: [],
    onPluginSourceChange: () => undefined,
    onPluginRefChange: () => undefined,
    onPluginEnableChange: () => undefined,
    onReview: (event: FormEvent) => event.preventDefault(),
    onInstall: () => undefined,
    onPluginAction: () => undefined,
    ...overrides
  };
}

describe("PluginsPanel", () => {
  it("keeps plugin review separate from authority enablement", async () => {
    const onReview = vi.fn((event: FormEvent) => event.preventDefault());
    const onInstall = vi.fn();

    render(
      <PluginsPanel
        {...pluginsPanelProps({
          pluginReview: reviewFixture(["plugin_isolation_unavailable"]),
          reviewedCurrentPlugin: true,
          pluginEnableBlockers: ["plugin_isolation_unavailable"],
          onReview,
          onInstall
        })}
      />
    );

    expect(screen.queryByText(/exactly once/i)).not.toBeInTheDocument();

    const reviewButton = screen.getByRole("button", { name: "Review" });
    const enableAfterInstall = screen.getByRole("checkbox", {
      name: /enable after install/i
    });
    const installButton = screen.getByRole("button", { name: "Install" });

    expect(enableAfterInstall).toBeDisabled();
    expect(installButton).toBeEnabled();

    fireEvent.click(reviewButton);
    expect(onReview).toHaveBeenCalledTimes(1);
    expect(onInstall).not.toHaveBeenCalled();
    expect(enableAfterInstall).toBeDisabled();

    expect(screen.getByText(/Review: reviewed/)).toBeInTheDocument();
    expect(screen.getByText(/container required unavailable/)).toBeInTheDocument();
  });
});
