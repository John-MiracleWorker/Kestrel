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
