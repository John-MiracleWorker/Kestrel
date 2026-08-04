import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SettingsSearch } from "./SettingsSearch";
import { blockedWebSearchSetting } from "./testFixtures";

const autonomySetting = {
  ...blockedWebSearchSetting,
  id: "general.autonomy_mode",
  key: "autonomy_mode",
  category: "General",
  type: "enum",
  blockers: [],
  configured_value: "background",
  effective_value: "background",
  allowed_values: ["background", "manual", "autonomous"],
};

describe("SettingsSearch", () => {
  afterEach(cleanup);

  it("returns settings with their owning feature surface", () => {
    const onQueryChange = vi.fn();
    const onSelect = vi.fn();
    render(
      <SettingsSearch
        query="web"
        onQueryChange={onQueryChange}
        results={[blockedWebSearchSetting]}
        onSelect={onSelect}
      />,
    );

    expect(
      screen.getByRole("button", { name: /tools\.web_search\.enabled/ }),
    ).toBeVisible();
    expect(screen.getAllByText("Safety and permissions")).not.toHaveLength(0);

    fireEvent.click(
      screen.getByRole("button", { name: /tools\.web_search\.enabled/ }),
    );
    expect(onSelect).toHaveBeenCalledWith("tools.web_search.enabled");
  });

  it("matches queries against the owning category as well as the setting", () => {
    render(
      <SettingsSearch
        query="general"
        onQueryChange={vi.fn()}
        results={[autonomySetting]}
        onSelect={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("button", { name: /general\.autonomy_mode/ }),
    ).toBeVisible();
  });

  it("reports honestly when nothing matches", () => {
    render(
      <SettingsSearch
        query="does-not-exist"
        onQueryChange={vi.fn()}
        results={[]}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByText(/No settings match/)).toBeVisible();
  });
});
