import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { StatusBadge } from "../components";
import { StatusPill } from "./StatusPill";

describe("StatusPill", () => {
  afterEach(cleanup);

  it("communicates status without color", () => {
    render(<StatusPill state="blocked">Needs approval</StatusPill>);

    expect(screen.getByText("Needs approval")).toBeVisible();
    expect(screen.getByTestId("status-icon")).toHaveAccessibleName("Blocked");
  });

  it.each([
    ["healthy", "Healthy"],
    ["waiting", "Waiting"],
    ["caution", "Caution"],
    ["inactive", "Inactive"],
  ] as const)("labels the %s status icon", (state, label) => {
    render(<StatusPill state={state}>{label} worker</StatusPill>);

    expect(screen.getByTestId("status-icon")).toHaveAccessibleName(label);
  });

  it.each(["not ready", "unhealthy", "ineligible"])(
    "never presents the negative readiness %s as healthy",
    (value) => {
      render(<StatusBadge value={value} />);

      expect(screen.getByText(value)).toBeVisible();
      expect(screen.getByTestId("status-icon")).toHaveAccessibleName("Blocked");
    },
  );
});
