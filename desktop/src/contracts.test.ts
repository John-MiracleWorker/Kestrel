import { describe, expect, it } from "vitest";
import {
  desktopConnectionSchema,
  desktopLifecycleStateSchema,
  desktopRecoveryReasonSchema
} from "./contracts";

describe("desktopConnectionSchema", () => {
  it("rejects a token-bearing renderer payload", () => {
    expect(() =>
      desktopConnectionSchema.parse({
        state: "ready",
        baseUrl: "http://127.0.0.1:43123",
        profileId: "default",
        sidecarVersion: "0.5.0",
        recovery: null,
        apiToken: "must-never-cross"
      })
    ).toThrow();
  });

  it("represents every supervisor transition without exposing authority", () => {
    expect(
      [
        "verifying",
        "starting",
        "ready",
        "stopping",
        "restarting",
        "recovery"
      ].map((state) => desktopLifecycleStateSchema.parse(state))
    ).toEqual([
      "verifying",
      "starting",
      "ready",
      "stopping",
      "restarting",
      "recovery"
    ]);
    expect(
      desktopRecoveryReasonSchema.parse("reconciliation_required")
    ).toBe("reconciliation_required");
  });
});
