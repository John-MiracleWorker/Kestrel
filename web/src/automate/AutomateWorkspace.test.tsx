import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AutomateWorkspace } from "./AutomateWorkspace";

describe("AutomateWorkspace", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("owns routine status and definitions loading", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = typeof input === "string" ? input : input.toString();
        requests.push(path);
        const value =
          path === "/api/routines/status"
            ? { enabled: true, loop: null }
            : path === "/api/routines"
              ? []
              : null;
        if (value === null) throw new Error(`unexpected_request:${path}`);
        return new Response(JSON.stringify(value), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );

    render(<AutomateWorkspace onAuthRequired={() => undefined} />);

    expect(
      await screen.findByRole("heading", { name: "Routine Workbench." }),
    ).toBeVisible();
    expect(requests).toEqual([
      "/api/routines/status",
      "/api/routines",
    ]);
  });
});
