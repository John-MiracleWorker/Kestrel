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
import { createFixtureFetch } from "../testing/apiFixtures";

describe("Settings workspace ownership", () => {
  const requests: string[] = [];

  beforeEach(() => {
    requests.length = 0;
    localStorage.clear();
    window.history.replaceState(null, "", "/#/settings");
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
    localStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  it("owns settings inventory and editable runtime draft state", async () => {
    render(<App />);

    const workspace = await screen.findByRole("region", {
      name: "Settings",
    });
    expect(workspace).toContainElement(
      screen.getByRole("heading", { name: "Settings." }),
    );
    await waitFor(() => {
      expect(requests).toContain("/api/channels");
      expect(requests).toContain("/api/secrets");
      expect(requests).toContain("/api/tools");
      expect(requests).toContain("/api/capabilities");
    });
    expect(requests).not.toContain("/api/plugins");
    expect(requests).not.toContain("/api/mcp/servers");
    expect(requests).not.toContain("/api/skills");

    const temperature = screen.getByLabelText("Temperature");
    fireEvent.change(temperature, { target: { value: "0.7" } });
    expect(temperature).toHaveValue(0.7);

    const inventoryReads = requests.filter(
      (path) => path === "/api/channels",
    ).length;
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => {
      expect(
        requests.filter((path) => path === "/api/channels").length,
      ).toBeGreaterThan(inventoryReads);
    });
    expect(requests).not.toContain("/api/plugins");
    expect(requests).not.toContain("/api/mcp/servers");
    expect(requests).not.toContain("/api/skills");
  });

  it("keeps Setup Center permanently routed and avoids general settings inventory", async () => {
    window.history.replaceState(null, "", "/#/settings/setup");

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "Setup Center." }),
    ).toBeVisible();
    expect(
      await screen.findByRole("heading", {
        name: "Review safety defaults",
      }),
    ).toBeVisible();
    await waitFor(() => {
      expect(requests).toContain("/api/product/setup");
      expect(requests).toContain("/api/runtime/models");
      expect(requests).toContain("/api/projects");
      expect(requests).toContain("/api/secrets");
      expect(requests).toContain("/api/runtime/settings");
    });
    expect(requests).not.toContain("/api/channels");
    expect(requests).not.toContain("/api/tools");
    expect(requests).not.toContain("/api/capabilities");
    expect(window.location.hash).toBe("#/settings/setup");
  });
});
