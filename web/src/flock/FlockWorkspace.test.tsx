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

  it("routes Qualification to a truthful unavailable surface", async () => {
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
    expect(
      screen.getByText(/no corpus run, routing authority, scoped grant/i),
    ).toBeVisible();
    await waitFor(() => {
      expect(requests).toContain("/api/runtime/models?provider=mock");
    });
    expect(requests.some((path) => path.startsWith("/api/routing/"))).toBe(
      false,
    );
    expect(
      screen.queryByRole("button", { name: /qualify|activate|grant/i }),
    ).not.toBeInTheDocument();
  });

  it("does not imply that LAN discovery has scanned or trusted a model", () => {
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
      screen.queryByRole("button", { name: /scan|trust|enable/i }),
    ).not.toBeInTheDocument();
  });
});
