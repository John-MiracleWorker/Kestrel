import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./AppShell";
import { parseAppLocation } from "./destinations";

describe("AppShell", () => {
  afterEach(cleanup);

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
      screen.getByRole("complementary", { name: "Context" }),
    ).toHaveTextContent("Layer evidence");
  });
});
