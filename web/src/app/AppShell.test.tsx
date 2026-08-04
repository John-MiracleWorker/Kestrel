import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppShell, useAppShellContextRail } from "./AppShell";
import { parseAppLocation } from "./destinations";

describe("AppShell", () => {
  afterEach(() => {
    cleanup();
    setViewport(1024);
  });

  it("renders seven destination links, one main, and a current-page marker", () => {
    render(
      <AppShell
        location={parseAppLocation("#/flock/qualification")}
        onNavigate={vi.fn()}
      >
        <h1>Flock qualification</h1>
      </AppShell>,
    );

    const navigation = screen.getByRole("navigation", {
      name: "Workbench destinations",
    });
    const destinationLinks = [
      ...navigation.querySelectorAll("a[data-destination]"),
    ];
    expect(destinationLinks).toHaveLength(7);
    expect(
      destinationLinks.map((link) => link.getAttribute("aria-label")),
    ).toEqual([
      "Mission",
      "Projects",
      "Memory",
      "Flock",
      "Automate",
      "Extend",
      "Settings",
    ]);
    expect(
      destinationLinks.every(
        (link) => link.getAttribute("title") === link.getAttribute("aria-label"),
      ),
    ).toBe(true);
    expect(screen.getAllByRole("main")).toHaveLength(1);
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Flock/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByText("Current", { selector: ".sr-only" })).toBeVisible();
  });

  it("adds one named context rail only when context is supplied", () => {
    setViewport(1440);
    render(
      <AppShell
        location={parseAppLocation("#/memory/layers")}
        onNavigate={vi.fn()}
        contextRail={<p>Layer evidence</p>}
      >
        <h1>Memory</h1>
      </AppShell>,
    );

    expect(
      screen.getByRole("complementary", { name: "Memory context" }),
    ).toHaveTextContent("Layer evidence");
  });

  it("lets the active workspace register and remove contextual evidence", () => {
    setViewport(1440);

    function WorkspaceContext({ enabled }: { enabled: boolean }) {
      const { portal } = useAppShellContextRail(
        enabled ? <p>Current mission authority</p> : null,
      );
      return (
        <>
          <h1>Mission</h1>
          {portal}
        </>
      );
    }

    function Harness() {
      const [enabled, setEnabled] = useState(true);
      return (
        <AppShell
          location={parseAppLocation("#/mission/command")}
          onNavigate={vi.fn()}
        >
          <WorkspaceContext enabled={enabled} />
          <button type="button" onClick={() => setEnabled(false)}>
            Clear context
          </button>
        </AppShell>
      );
    }

    render(<Harness />);

    expect(
      screen.getByRole("complementary", { name: "Mission context" }),
    ).toHaveTextContent("Current mission authority");

    fireEvent.click(
      screen.getByRole("button", { name: "Clear context" }),
    );
    expect(
      screen.queryByRole("complementary", {
        name: "Mission context",
      }),
    ).not.toBeInTheDocument();
  });

  it("keeps navigation, main, and context in keyboard document order", () => {
    setViewport(1440);
    render(
      <AppShell
        location={parseAppLocation("#/mission/command")}
        onNavigate={vi.fn()}
        contextRail={<button type="button">Review approval</button>}
      >
        <h1>Mission</h1>
      </AppShell>,
    );

    const mission = screen.getByRole("link", { name: /Mission/ });
    const main = screen.getByRole("main");
    const context = screen.getByRole("complementary", {
      name: "Mission context",
    });
    expect(mission.tabIndex).toBe(0);
    expect(main.tabIndex).toBe(0);
    expect(context.tabIndex).toBe(0);
    expect(
      mission.compareDocumentPosition(main) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      main.compareDocumentPosition(context) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    mission.focus();
    expect(mission).toHaveFocus();
    main.focus();
    expect(main).toHaveFocus();
    context.focus();
    expect(context).toHaveFocus();
  });

  it("collapses context at a 960px desktop width without removing its toggle", () => {
    setViewport(960);
    render(
      <AppShell
        location={parseAppLocation("#/mission/command")}
        onNavigate={vi.fn()}
        contextRail={<p>Budget and permission evidence</p>}
      >
        <h1>Mission</h1>
      </AppShell>,
    );

    const show = screen.getByRole("button", {
      name: "Show mission context",
    });
    expect(show).toBeVisible();
    expect(
      screen.queryByRole("complementary", { name: "Mission context" }),
    ).not.toBeInTheDocument();

    fireEvent.click(show);
    expect(
      screen.getByRole("complementary", { name: "Mission context" }),
    ).toHaveFocus();

    setViewport(1440);
    fireEvent(window, new Event("resize"));
    expect(
      screen.getByRole("complementary", { name: "Mission context" }),
    ).toBeVisible();

    setViewport(960);
    fireEvent(window, new Event("resize"));
    expect(
      screen.queryByRole("complementary", { name: "Mission context" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Show mission context" }),
    ).toHaveFocus();
  });

  it("isolates outside navigation while the command palette is modal", () => {
    const onNavigate = vi.fn();
    render(
      <AppShell
        location={parseAppLocation("#/mission/command")}
        onNavigate={onNavigate}
      >
        <button type="button">Run mission</button>
      </AppShell>,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Open command palette" }),
    );
    const mission = screen.getByRole("link", { name: /Mission/ });
    expect(mission).toHaveAttribute("aria-disabled", "true");
    expect(mission).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("main").parentElement).toHaveAttribute("inert");
    expect(fireEvent.click(mission)).toBe(false);
    expect(onNavigate).not.toHaveBeenCalled();
  });
});

function setViewport(width: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    value: width,
  });
}
