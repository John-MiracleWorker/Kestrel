import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Disclosure } from "./Disclosure";

describe("Disclosure", () => {
  afterEach(cleanup);

  it("keeps disclosure state available to assistive technology", () => {
    const ref = createRef<HTMLButtonElement>();
    render(
      <Disclosure ref={ref} title="Evidence">
        digest
      </Disclosure>,
    );
    const button = screen.getByRole("button", { name: "Evidence" });
    const content = screen.getByText("digest");

    expect(ref.current).toBe(button);
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(content).not.toBeVisible();
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(content).toBeVisible();
  });

  it("reports controlled changes without mutating the controlled state", () => {
    const onOpenChange = vi.fn();
    const { rerender } = render(
      <Disclosure
        title="Route evidence"
        open={false}
        onOpenChange={onOpenChange}
      >
        digest
      </Disclosure>,
    );
    const button = screen.getByRole("button", { name: "Route evidence" });

    fireEvent.click(button);
    expect(onOpenChange).toHaveBeenLastCalledWith(true);
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("digest")).not.toBeVisible();

    rerender(
      <Disclosure
        title="Route evidence"
        open
        onOpenChange={onOpenChange}
      >
        digest
      </Disclosure>,
    );
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("digest")).toBeVisible();

    fireEvent.click(button);
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
  });
});
