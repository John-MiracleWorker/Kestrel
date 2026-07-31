import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Button } from "./Button";

describe("Button", () => {
  afterEach(cleanup);

  it("forwards native button props and its ref", () => {
    const ref = createRef<HTMLButtonElement>();
    const onClick = vi.fn();

    render(
      <Button
        ref={ref}
        type="submit"
        name="commit"
        variant="primary"
        onClick={onClick}
      >
        Commit change
      </Button>,
    );

    const button = screen.getByRole("button", { name: "Commit change" });
    expect(button.tagName).toBe("BUTTON");
    expect(button).toHaveAttribute("type", "submit");
    expect(button).toHaveAttribute("name", "commit");
    expect(ref.current).toBe(button);
    button.click();
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("exposes and disables a pending action", () => {
    render(<Button pending>Saving settings</Button>);

    expect(screen.getByRole("button", { name: "Saving settings" })).toBeDisabled();
    expect(screen.getByRole("button")).toHaveAttribute("aria-busy", "true");
  });

  it("preserves a native aria-busy value when pending is false", () => {
    render(<Button aria-busy="true">Background refresh</Button>);

    expect(
      screen.getByRole("button", { name: "Background refresh" }),
    ).toHaveAttribute("aria-busy", "true");
  });
});
