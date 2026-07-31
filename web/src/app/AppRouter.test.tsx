import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { useEffect } from "react";
import { AppRouter, legacySectionForLocation } from "./AppRouter";

describe("AppRouter", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    cleanup();
    window.history.replaceState(null, "", "/");
  });

  it("renders a nested destination and navigates through the shared parser", () => {
    render(
      <AppRouter
        initialHash="#/flock/qualification"
        renderContent={(location) => (
          <h1>
            {location.destination}/{location.subroute}
          </h1>
        )}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "flock/qualification" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: /Settings/ }));
    expect(
      screen.getByRole("heading", { name: "settings/general" }),
    ).toBeInTheDocument();
  });

  it("recovers an unknown route visibly without dropping evidence query", () => {
    render(
      <AppRouter
        initialHash="#/lost?run_id=run_1"
        renderContent={(location) => (
          <output>{location.query.run_id}</output>
        )}
      />,
    );

    expect(
      screen.getByRole("status", { name: "Route recovery" }),
    ).toHaveTextContent("Unknown destination");
    expect(screen.getByText("run_1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Mission/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("replaces unknown browser hashes with a trusted Mission route while retaining recovery state", () => {
    window.history.replaceState(
      null,
      "",
      "/#/lost?run_id=run_1",
    );

    render(
      <AppRouter
        renderContent={(location) => (
          <output>
            {location.recoveryReason}:{location.query.run_id}
          </output>
        )}
      />,
    );

    expect(window.location.hash).toBe(
      "#/mission/command?run_id=run_1",
    );
    expect(screen.getByText("unknown_route:run_1")).toBeInTheDocument();
  });

  it("upgrades legacy browser hashes to the stable route contract", () => {
    window.history.replaceState(null, "", "/#routing");

    render(
      <AppRouter
        renderContent={(location) => (
          <output>
            {location.destination}/{location.subroute}
          </output>
        )}
      />,
    );

    expect(window.location.hash).toBe("#/flock/routing");
    expect(screen.getByText("flock/routing")).toBeInTheDocument();
  });

  it("canonicalizes the frame before descendant effects can use the Desktop bridge", () => {
    window.history.replaceState(null, "", "/#routing");
    const observedHash = vi.fn();

    function BridgeProbe() {
      useEffect(() => {
        observedHash(window.location.hash);
      }, []);
      return <output>ready</output>;
    }

    render(
      <AppRouter renderContent={() => <BridgeProbe />} />,
    );

    expect(observedHash).toHaveBeenCalledWith("#/flock/routing");
  });

  it("adapts stable destinations onto the current Workbench sections", () => {
    expect(
      legacySectionForLocation({
        destination: "mission",
        subroute: "history",
        query: {},
      }),
    ).toBe("chat");
    expect(
      legacySectionForLocation({
        destination: "flock",
        subroute: "qualification",
        query: {},
      }),
    ).toBe("routing");
    expect(
      legacySectionForLocation({
        destination: "automate",
        subroute: "routines",
        query: {},
      }),
    ).toBe("routines");
  });
});
