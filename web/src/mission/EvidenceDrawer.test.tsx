import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { EvidenceDrawer } from "./EvidenceDrawer";

describe("EvidenceDrawer", () => {
  afterEach(cleanup);

  it("keeps raw records behind an owner-opened disclosure", () => {
    render(
      <EvidenceDrawer
        title="Mission evidence"
        records={[
          {
            label: "Launch binding",
            value: {
              binding_digest: "a".repeat(64),
              project_revision: 4,
            },
          },
        ]}
      />,
    );

    expect(screen.getByText("Mission evidence")).toBeVisible();
    expect(screen.getByText(/binding_digest/)).not.toBeVisible();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Launch binding evidence",
      }),
    );
    expect(screen.getByText(/binding_digest/)).toBeVisible();
    expect(screen.getByText(/project_revision/)).toBeVisible();
  });
});
