import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { EmptyState } from "./EmptyState";
import { Notice } from "./Notice";
import { Skeleton } from "./Skeleton";

describe("Wildflower feedback primitives", () => {
  afterEach(cleanup);

  it("announces dangerous notices and keeps their title visible", () => {
    render(
      <Notice variant="danger" title="Action failed">
        The exact call was not applied.
      </Notice>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Action failedThe exact call was not applied.",
    );
  });

  it("gives empty states a real heading", () => {
    render(
      <EmptyState title="No routes yet">
        Preview a task to compare eligible workers.
      </EmptyState>,
    );

    expect(
      screen.getByRole("heading", { name: "No routes yet", level: 2 }),
    ).toBeVisible();
  });

  it("labels meaningful loading placeholders and hides decorative ones", () => {
    const { rerender } = render(<Skeleton label="Loading routes" lines={2} />);
    expect(screen.getByRole("status", { name: "Loading routes" })).toBeVisible();

    rerender(<Skeleton />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(document.querySelector(".wf-skeleton")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });
});
