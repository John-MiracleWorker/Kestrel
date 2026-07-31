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
});
