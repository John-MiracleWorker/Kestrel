import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingControl } from "./SettingControl";
import { blockedWebSearchSetting, jsonResponse } from "./testFixtures";
import type { ProjectedSetting } from "./types";

const newerSettingProjection: ProjectedSetting = {
  ...blockedWebSearchSetting,
  configured_value: false,
  effective_value: false,
  blockers: [],
  revision: "rev-2",
};

describe("SettingControl", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("shows configured on but effective blocked", () => {
    render(
      <SettingControl
        setting={blockedWebSearchSetting}
        onCommitted={vi.fn()}
      />,
    );

    expect(screen.getByRole("switch")).toBeChecked();
    expect(screen.getByText("Currently blocked")).toBeVisible();
    expect(screen.getByText("Network capability is disabled")).toBeVisible();
  });

  it("recovers from a revision conflict with server truth", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "PUT" && path.startsWith("/api/settings/")) {
        return Promise.resolve(
          jsonResponse(
            {
              detail: {
                error: "setting_revision_conflict",
                current: newerSettingProjection,
              },
            },
            409,
          ),
        );
      }
      return Promise.resolve(jsonResponse({ detail: "not_found" }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SettingControl
        setting={blockedWebSearchSetting}
        onCommitted={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("switch"));

    expect(
      await screen.findByText("Changed elsewhere; review the current value"),
    ).toBeVisible();
    expect(screen.getByRole("switch")).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("commits a boolean change only after the server returns the projection", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "PUT" && path.startsWith("/api/settings/")) {
        return Promise.resolve(
          jsonResponse({
            schema: "kestrel.effective_settings_mutation.v1",
            setting: {
              ...blockedWebSearchSetting,
              configured_value: false,
              revision: "rev-2",
            },
            revision: "rev-2",
            store_revision: "rev-2",
            undo_available: true,
            undo: {
              available: true,
              setting_id: "tools.web_search.enabled",
              key: "allow_web",
            },
            revoked_approvals: 0,
            authority_changes: [],
          }),
        );
      }
      return Promise.resolve(jsonResponse({ detail: "not_found" }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    const onCommitted = vi.fn();

    render(
      <SettingControl
        setting={blockedWebSearchSetting}
        onCommitted={onCommitted}
      />,
    );

    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() =>
      expect(screen.getByRole("switch")).toHaveAttribute(
        "aria-checked",
        "false",
      ),
    );
    expect(onCommitted).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      value: false,
      expected_revision: "rev-1",
    });
  });

  it("shows a truthful blocked state after the server commits on top of a disabled capability", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "PUT" && path.startsWith("/api/settings/")) {
        return Promise.resolve(
          jsonResponse({
            schema: "kestrel.effective_settings_mutation.v1",
            setting: {
              ...blockedWebSearchSetting,
              configured_value: false,
              effective_value: false,
              blockers: ["capability:network_disabled"],
              revision: "rev-2",
            },
            revision: "rev-2",
            store_revision: "rev-2",
            undo_available: true,
            undo: {
              available: true,
              setting_id: "tools.web_search.enabled",
              key: "allow_web",
            },
            revoked_approvals: 0,
            authority_changes: [],
          }),
        );
      }
      return Promise.resolve(jsonResponse({ detail: "not_found" }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SettingControl
        setting={blockedWebSearchSetting}
        onCommitted={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("switch"));

    // The server kept the capability blocker; the control must still say
    // blocked rather than claiming the value took effect.
    await waitFor(() =>
      expect(screen.getByRole("switch")).toHaveAttribute(
        "aria-checked",
        "false",
      ),
    );
    expect(screen.getByText("Currently blocked")).toBeVisible();
    expect(screen.queryByText(/effective on/)).not.toBeInTheDocument();
  });
});
