import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsIndex } from "./SettingsIndex";
import { jsonResponse, settingsProjectionFixture } from "./testFixtures";

describe("SettingsIndex", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("loads the server projection and renders controls with owning surfaces", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input) === "/api/settings") {
        return Promise.resolve(jsonResponse(settingsProjectionFixture));
      }
      return Promise.resolve(jsonResponse({ detail: "not_found" }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsIndex />);

    expect(
      await screen.findByRole("switch", {
        name: /tools\.web_search\.enabled/,
      }),
    ).toBeChecked();
    expect(screen.getAllByText("Currently blocked")).not.toHaveLength(0);
    expect(screen.getAllByText("Safety and permissions")).not.toHaveLength(0);
    expect(screen.getAllByText("Models and providers")).not.toHaveLength(0);
  });

  it("filters settings through search, including category matches", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input) === "/api/settings") {
        return Promise.resolve(jsonResponse(settingsProjectionFixture));
      }
      return Promise.resolve(jsonResponse({ detail: "not_found" }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsIndex />);
    await screen.findByRole("switch", {
      name: /tools\.web_search\.enabled/,
    });

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "models" },
    });

    await waitFor(() =>
      expect(
        screen.queryByRole("switch", {
          name: /tools\.web_search\.enabled/,
        }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByLabelText(/models\.temperature/)).toBeInTheDocument();
  });

  it("surfaces a load error without fabricating setting state", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(jsonResponse({ detail: "boom" }, 500)),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsIndex />);

    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });
});
