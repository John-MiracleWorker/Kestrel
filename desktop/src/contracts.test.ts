import { describe, expect, it } from "vitest";
import { desktopConnectionSchema } from "./contracts";

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
});
