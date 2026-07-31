import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectsWorkspace } from "./ProjectsWorkspace";

describe("ProjectsWorkspace", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("owns the Mission project surface without a second shell", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = typeof input === "string" ? input : input.toString();
        if (path !== "/api/projects") {
          throw new Error(`unexpected_request:${path}`);
        }
        return new Response(JSON.stringify({ items: [], count: 0 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );

    render(
      <ProjectsWorkspace
        runs={[]}
        activeRun={null}
        taskGraph={null}
        approvals={[]}
        events={[]}
        onLaunch={async () => undefined}
        onOpenRun={() => undefined}
        onOpenHistory={() => undefined}
        onOpenAdvanced={() => undefined}
        onOpenDiagnostics={() => undefined}
        onPrepareTool={() => undefined}
        onAuthRequired={() => undefined}
      />,
    );

    expect(
      await screen.findByRole("heading", {
        name: "What should Kestrel accomplish?",
      }),
    ).toBeVisible();
    expect(screen.queryByRole("main")).not.toBeInTheDocument();
  });
});
