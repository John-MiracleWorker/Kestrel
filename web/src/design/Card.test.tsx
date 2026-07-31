import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Button } from "./Button";
import { Card } from "./Card";

describe("Card", () => {
  afterEach(cleanup);

  it("names its region from the visible heading", () => {
    render(
      <Card
        title="Route evidence"
        actions={<Button variant="quiet">Refresh</Button>}
      >
        Provenance digest
      </Card>,
    );

    const card = screen.getByRole("region", { name: "Route evidence" });
    expect(card).toContainElement(
      screen.getByRole("heading", { name: "Route evidence", level: 2 }),
    );
    expect(card).toHaveTextContent("Provenance digest");
    expect(screen.getByRole("button", { name: "Refresh" })).toBeVisible();
  });
});
