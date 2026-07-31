import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommandBar } from "./CommandBar";

describe("CommandBar", () => {
  afterEach(cleanup);

  it("opens from the platform shortcut and navigates only to destinations", () => {
    const onNavigate = vi.fn();
    render(<CommandBar onNavigate={onNavigate} />);

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const dialog = screen.getByRole("dialog", {
      name: "Open a Kestrel destination",
    });
    const search = within(dialog).getByRole("searchbox", {
      name: "Search destinations",
    });
    expect(search).toHaveFocus();

    fireEvent.change(search, { target: { value: "settings" } });
    const choices = within(dialog).getAllByRole("button");
    expect(choices.map((choice) => choice.getAttribute("aria-label"))).toEqual([
      "Settings",
    ]);

    fireEvent.click(choices[0]);
    expect(onNavigate).toHaveBeenCalledWith("settings");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("returns focus to its trigger when Escape closes the palette", () => {
    render(<CommandBar onNavigate={vi.fn()} />);
    const trigger = screen.getByRole("button", {
      name: "Open command palette",
    });

    fireEvent.click(trigger);
    fireEvent.keyDown(
      screen.getByRole("dialog", {
        name: "Open a Kestrel destination",
      }),
      { key: "Escape" },
    );

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("returns shortcut focus to the element active before opening", () => {
    render(
      <>
        <input aria-label="Mission objective" />
        <CommandBar onNavigate={vi.fn()} />
      </>,
    );
    const objective = screen.getByRole("textbox", {
      name: "Mission objective",
    });
    objective.focus();

    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const dialog = screen.getByRole("dialog", {
      name: "Open a Kestrel destination",
    });
    expect(dialog.parentElement?.parentElement).toBe(document.body);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(
      within(dialog).getByRole("searchbox", {
        name: "Search destinations",
      }),
    ).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(objective).toHaveFocus();
  });

  it("keeps Tab focus inside the open command palette", () => {
    render(<CommandBar onNavigate={vi.fn()} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Open command palette" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Open a Kestrel destination",
    });
    const search = within(dialog).getByRole("searchbox", {
      name: "Search destinations",
    });
    const choices = within(dialog).getAllByRole("button");
    const lastChoice = choices.at(-1);
    expect(lastChoice).toBeDefined();

    lastChoice?.focus();
    fireEvent.keyDown(lastChoice!, { key: "Tab" });
    expect(search).toHaveFocus();

    search.focus();
    fireEvent.keyDown(search, { key: "Tab", shiftKey: true });
    expect(lastChoice).toHaveFocus();
  });

  it("has no automated accessibility violations while open", async () => {
    render(<CommandBar onNavigate={vi.fn()} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Open command palette" }),
    );

    expect(
      screen
        .getByRole("dialog", { name: "Open a Kestrel destination" })
        .closest(".workbench-command-bar"),
    ).toBeNull();
    const report = await axe.run(document.body);
    expect(report.violations).toEqual([]);
  });

  it("searches projected settings and reports their owning feature surface", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input) === "/api/settings") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              schema: "kestrel.effective_settings.v1",
              revision: "rev-1",
              categories: ["Safety and permissions"],
              items: [
                {
                  id: "tools.web_search.enabled",
                  key: "allow_web",
                  category: "Safety and permissions",
                  type: "boolean",
                  configured_value: true,
                  effective_value: false,
                  blockers: ["capability:network_disabled"],
                  authority_impact: "grants_authority",
                  privacy_impact: "network_egress",
                  applies: "new_runs",
                  revision: "rev-1",
                  provenance: "runtime",
                  undo_available: true,
                  allowed_values: null,
                  allowed_range: null,
                  restart_required: false,
                  writable: true,
                  requires_approval: true,
                },
              ],
              items_by_id: {},
              counts: { total: 1, blocked: 1, restart_required: 0 },
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ detail: "not_found" }), { status: 404 }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CommandBar onNavigate={vi.fn()} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Open command palette" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Open a Kestrel destination",
    });
    fireEvent.change(
      within(dialog).getByRole("searchbox", {
        name: "Search destinations",
      }),
      { target: { value: "web search" } },
    );

    const hit = await within(dialog).findByRole("button", {
      name: /tools\.web_search\.enabled/,
    });
    expect(hit).toHaveTextContent("Safety and permissions");
    expect(within(dialog).queryByRole("status")).not.toBeInTheDocument();
  });
});
