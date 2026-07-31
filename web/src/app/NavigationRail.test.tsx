import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NavigationRail } from "./NavigationRail";
import { parseAppLocation } from "./destinations";

describe("NavigationRail", () => {
  afterEach(cleanup);

  it("keeps all seven native destination links stable and named", () => {
    render(
      <NavigationRail
        location={parseAppLocation("#/flock/routing")}
        onNavigate={vi.fn()}
      />,
    );

    const navigation = screen.getByRole("navigation", {
      name: "Workbench destinations",
    });
    const links = within(navigation).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("aria-label"))).toEqual([
      "Mission",
      "Projects",
      "Memory",
      "Flock",
      "Automate",
      "Extend",
      "Settings",
    ]);
    expect(links[0]).toHaveAttribute("href", "#/mission/command");
    expect(
      screen.getByRole("link", { name: /Flock/ }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("routes through the shell owner without discarding native hrefs", () => {
    const onNavigate = vi.fn();
    render(
      <NavigationRail
        location={parseAppLocation("#/mission/command")}
        onNavigate={onNavigate}
      />,
    );

    fireEvent.click(screen.getByRole("link", { name: "Settings" }));
    expect(onNavigate).toHaveBeenCalledWith("settings");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute(
      "href",
      "#/settings/general",
    );
  });

  it("preserves native modified-click behavior for real destination links", () => {
    const onNavigate = vi.fn();
    render(
      <NavigationRail
        location={parseAppLocation("#/mission/command")}
        onNavigate={onNavigate}
      />,
    );
    const settings = screen.getByRole("link", { name: "Settings" });

    expect(fireEvent.click(settings, { metaKey: true })).toBe(true);
    expect(fireEvent.click(settings, { ctrlKey: true })).toBe(true);
    expect(onNavigate).not.toHaveBeenCalled();
  });
});
