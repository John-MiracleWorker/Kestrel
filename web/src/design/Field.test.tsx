import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Field } from "./Field";

describe("Field", () => {
  afterEach(cleanup);

  it("binds its label and hint to the native control", () => {
    render(
      <Field label="Maximum workers" hint="One to eight workers.">
        <input type="number" min={1} max={8} />
      </Field>,
    );

    const input = screen.getByRole("spinbutton", { name: "Maximum workers" });
    expect(input).toHaveAccessibleDescription("One to eight workers.");
  });

  it("announces validation without replacing native control props", () => {
    render(
      <Field
        label="Provider URL"
        hint="Private addresses only."
        error="Enter a valid URL."
      >
        <input name="provider_url" required />
      </Field>,
    );

    const input = screen.getByRole("textbox", { name: "Provider URL" });
    expect(input).toHaveAttribute("name", "provider_url");
    expect(input).toBeRequired();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription(
      "Private addresses only. Enter a valid URL.",
    );
  });
});
