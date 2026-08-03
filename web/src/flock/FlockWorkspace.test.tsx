import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { createFixtureFetch } from "../testing/apiFixtures";
import { FlockWorkspace } from "./FlockWorkspace";

describe("FlockWorkspace", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it("routes Qualification to the bounded qualification workspace", async () => {
    const requests: string[] = [];
    window.history.replaceState(null, "", "/#/flock/qualification");
    vi.stubGlobal(
      "fetch",
      createFixtureFetch((request) => requests.push(request.path)),
    );

    render(<App />);

    expect(
      await screen.findByRole("heading", {
        name: "Adaptive Flock qualification",
      }),
    ).toBeVisible();
    expect(screen.getByLabelText("Maximum provider spend")).toHaveValue(
      "50.00",
    );
    await waitFor(() => {
      expect(requests).toContain("/api/runtime/models?provider=mock");
    });
    expect(requests.some((path) => path.startsWith("/api/routing/"))).toBe(
      false,
    );
    expect(requests.some((path) => path.startsWith("/api/flock/"))).toBe(
      false,
    );
    expect(
      screen.queryByRole("button", { name: /activate|grant|start qualification/i }),
    ).not.toBeInTheDocument();
  });

  it("does not call discovery endpoints merely by opening the LAN workspace", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      createFixtureFetch((request) => requests.push(request.path)),
    );

    render(
      <FlockWorkspace
        subroute="lan"
        activeRunId={null}
        activeTaskId={null}
        onError={() => undefined}
        onNotice={() => undefined}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "LAN model discovery" }),
    ).toBeVisible();
    expect(screen.getByText(/no LAN scan has run/i)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Scan network" }),
    ).toBeVisible();
    await waitFor(() => {
      expect(requests).not.toContain("/api/routing/lan/interfaces");
      expect(
        requests.some((path) => path.includes("/lan/scans")),
      ).toBe(false);
    });
  });
});
