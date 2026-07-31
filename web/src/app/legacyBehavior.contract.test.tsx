import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import {
  apiFixtures,
  createFixtureFetch,
  currentAppStartupRequests,
  legacyMutationContracts,
} from "../testing/apiFixtures";
import { createFakeDesktopBridge } from "../testing/fakeDesktopBridge";

const expectedFixturePaths = [
  "/api/approvals",
  "/api/capabilities",
  "/api/channels",
  "/api/learning/dashboard?since=all",
  "/api/memory/layers",
  "/api/mcp/servers",
  "/api/plugins",
  "/api/product/setup",
  "/api/projects",
  "/api/routing/status",
  "/api/runs",
  "/api/runtime/config",
  "/api/runtime/models",
  "/api/runtime/settings",
  "/api/secrets",
  "/api/sessions",
  "/api/skills",
  "/api/tools",
] as const;

describe("legacy Workbench behavior contracts", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("has a deterministic fixture for every current App startup request", () => {
    expect(Object.keys(apiFixtures).sort()).toEqual(
      [...expectedFixturePaths].sort(),
    );
  });

  it("records every behavior-preserving mutation boundary", () => {
    expect(Object.keys(legacyMutationContracts).sort()).toEqual([
      "approvalDecision",
      "browserTokenPrompt",
      "capabilityToggle",
      "extensionReview",
      "firstRunSetup",
      "memorySearch",
      "missionLaunch",
      "missionPreflight",
      "providerSave",
      "routineRun",
      "settingsSave",
      "targetSave",
    ]);
    expect(legacyMutationContracts.capabilityToggle).toMatchObject({
      method: "PUT",
      path: "/api/capabilities/:kind/:id",
      requiredBodyFields: ["enabled", "expected_revision"],
    });
    expect(legacyMutationContracts.settingsSave.requiredBodyFields).toContain(
      "expected_revision",
    );
    expect(legacyMutationContracts.missionLaunch.requiredBodyFields).toEqual(
      expect.arrayContaining(["project_revision", "mission_binding"]),
    );
    expect(legacyMutationContracts.routineRun.requiredBodyFields).toEqual([
      "expected_revision",
      "idempotency_key",
    ]);
  });

  it("fails closed on an unregistered fixture request", async () => {
    const fixtureFetch = createFixtureFetch();
    await expect(fixtureFetch("/api/not-a-real-route")).rejects.toThrow(
      "unhandled_fixture_request:GET:/api/not-a-real-route",
    );
  });

  it("serves the complete current App startup without a catch-all response", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      createFixtureFetch((request) => requests.push(request.path)),
    );

    render(<App />);
    expect(
      await screen.findByRole("heading", { name: "Ask Kestrel" }),
    ).toBeVisible();
    await waitFor(() => {
      expect(requests).toContain("/api/runtime/models?provider=mock");
    });
    expect([...new Set(requests)].sort()).toEqual(
      [...currentAppStartupRequests].sort(),
    );
  });

  it("keeps fixture responses free of raw secret values", () => {
    expect(JSON.stringify(apiFixtures)).not.toMatch(
      /super-secret|raw[_-]?secret|browser-token/i,
    );
  });

  it("provides the exact frozen eleven-method Desktop bridge", () => {
    const bridge = createFakeDesktopBridge();
    expect(Object.isFrozen(bridge)).toBe(true);
    expect(Object.keys(bridge).sort()).toEqual([
      "chooseProjectFolder",
      "chooseStorageFolder",
      "connection",
      "exportSupportBundle",
      "getAppVersion",
      "getUpdateStatus",
      "openCredentialDialog",
      "openExternalUrl",
      "performRecoveryAction",
      "subscribeLifecycle",
      "subscribeUpdateStatus",
    ]);
  });
});
