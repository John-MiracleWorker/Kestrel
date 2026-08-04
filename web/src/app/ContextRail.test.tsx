import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ContextRail } from "./ContextRail";

describe("ContextRail", () => {
  afterEach(cleanup);

  it("opens as a non-modal complementary drawer and manages focus", () => {
    render(
      <ContextRail label="Mission context" defaultOpen={false}>
        <p>Route and approval evidence</p>
      </ContextRail>,
    );

    const show = screen.getByRole("button", {
      name: "Show mission context",
    });
    expect(show).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByRole("complementary", { name: "Mission context" }),
    ).not.toBeInTheDocument();

    fireEvent.click(show);
    const rail = screen.getByRole("complementary", {
      name: "Mission context",
    });
    expect(rail).toHaveFocus();
    expect(rail).toHaveTextContent("Route and approval evidence");
    expect(rail).not.toHaveAttribute("aria-modal");

    fireEvent.keyDown(rail, { key: "Escape" });
    expect(
      screen.queryByRole("complementary", { name: "Mission context" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Show mission context" }),
    ).toHaveFocus();
  });
});
